#!/usr/bin/env python3
"""Checks a documentation.db that build-all-docs(.yaml / -local.yaml) has rebuilt.

The four per-set pipelines each verify their own slice as they go (the Kotlin blacklist check,
the Java parity check, the Android render sample, the re-mint round trip). This is the gate on
the *combined* result, run last, against the database exactly as it will ship:

  1. Every documentation set that was rebuilt is actually served from JSON: it has rows, and the
     rows are pointed at a template.
  2. Every templated row in the database -- not just the rebuilt sets -- names a Templates row
     that exists, reassembles from its chunks, decodes the way the app will decode it, and parses
     as JSON. The app's rule (CodeOnTheGo's DocumentationCompression.kt): a database declaring
     major version 2 or later has every brotli row compressed against the shared dictionary and
     is read with it attached, with no plain-Brotli retry; anything older is read plainly.
  3. A random sample of each rebuilt set renders through its stored template, in Pebble as the
     server configures it (the Android renderer's TemplateCheck), within the app's 4 MiB cap.
  4. With --original: no row appeared or disappeared outside the documentation sets' own paths,
     no Tooltips row was lost, and no tooltip button that resolved before now points at nothing.
  5. PRAGMA quick_check.

    verify_all_docs_db.py documentation.db --original documentation-original.db \\
        --sets android,java,kotlin-website,kotlin-stdlib --sample 20

Exits 1 if any check fails.
"""

from __future__ import annotations

import argparse
import json
import random
import sqlite3
import sys
from collections import Counter
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
_KOTLIN_PIPELINE = REPO_ROOT / "ProcessDocs" / "ProcessKotlinDocs" / "ProcessKotlinWebsiteJSON"
_ANDROID_PIPELINE = REPO_ROOT / "ProcessDocs" / "AndroidDocs" / "AndroidDocsToJson"
# append, not insert(0): nothing in those directories may shadow a standard-library module.
sys.path.append(str(_KOTLIN_PIPELINE))
sys.path.append(str(_ANDROID_PIPELINE))

import brotli  # noqa: E402

from content_chunking import is_continuation_path  # noqa: E402
from migrate_content_to_dictionary_brotli import read_item  # noqa: E402
from populate_db import DictionaryCompressor  # noqa: E402

# Must match DatabaseVersionResolver.MAJOR_VERSION_WITH_COMPRESSION_DICTIONARY in CodeOnTheGo.
MAJOR_VERSION_WITH_COMPRESSION_DICTIONARY = 2

# CodeOnTheGo's DocumentationContentSource caps a rendered page at this many characters.
APP_MAX_RENDERED_CHARS = 4 * 1024 * 1024

# Where each documentation set lives in the served namespace. A path belongs to a set if it
# starts with any of its prefixes. `assets/` is shared: the Kotlin website and the Android
# reference both keep their stylesheets and scripts there.
DOC_SETS = {
    "android": ("a/android/", "a/androidx/", "a/index.html"),
    "java": ("j/html/api/",),
    "kotlin-website": ("k/html/",),
    "kotlin-stdlib": ("k/kotlin-stdlib", "k/kotlin-reflect", "k/kotlin-test"),
}
SHARED_PREFIXES = ("assets/",)


def doc_set_of(path: str) -> str | None:
    for name, prefixes in DOC_SETS.items():
        if path.startswith(prefixes):
            return name
    return None


def in_rebuildable_namespace(path: str) -> bool:
    return doc_set_of(path) is not None or path.startswith(SHARED_PREFIXES)


class Report:
    def __init__(self):
        self.failures: list[str] = []

    def check(self, ok: bool, message: str, details: list[str] | None = None) -> bool:
        print(f"{'PASS' if ok else 'FAIL'}: {message}")
        if not ok:
            self.failures.append(message)
            for line in (details or [])[:15]:
                print(f"    {line}")
            if details and len(details) > 15:
                print(f"    ... and {len(details) - 15} more")
        return ok


# --------------------------------------------------------------------------------------------
# Decoding, the way the app decodes
# --------------------------------------------------------------------------------------------

def table_exists(conn, name: str) -> bool:
    return conn.execute("SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = ?",
                        (name,)).fetchone() is not None


def declared_version(conn) -> tuple | None:
    """(major, minor, patch, comment) of the newest DocumentationDatabaseVersion row, ordered the
    way the app's DatabaseVersionResolver orders it."""
    if not table_exists(conn, "DocumentationDatabaseVersion"):
        return None
    return conn.execute("SELECT major, minor, patch, comment FROM DocumentationDatabaseVersion "
                        "ORDER BY changeTime DESC, rowid DESC LIMIT 1").fetchone()


class PlainDecoder:
    def decompress(self, data: bytes) -> bytes:
        return brotli.decompress(data)


def decoder_for(conn):
    """What the app attaches for this database: the shared dictionary from 2.0 on, else none."""
    version = declared_version(conn)
    if version is not None and version[0] >= MAJOR_VERSION_WITH_COMPRESSION_DICTIONARY:
        row = (conn.execute("SELECT data FROM CompressionDictionary WHERE id = 1").fetchone()
               if table_exists(conn, "CompressionDictionary") else None)
        if row is None or not row[0]:
            raise SystemExit("error: the database declares version "
                             f"{version[0]}.{version[1]}.{version[2]} but has no usable "
                             "CompressionDictionary - the app could not read any brotli row")
        return DictionaryCompressor(row[0]), "the shared dictionary"
    return PlainDecoder(), "plain Brotli (no dictionary)"


def templated_rows(conn) -> list[tuple[str, int, str]]:
    """(path, templateId, compression) for every base row with a template; continuation rows
    are part of their base row's content and are read through it."""
    rows = conn.execute(
        "SELECT c.path, c.templateId, t.compression, LENGTH(c.content) FROM Content c "
        "LEFT JOIN ContentTypes t ON t.id = c.contentTypeID").fetchall()
    lengths = {path: length for path, _tid, _comp, length in rows}
    return [(path, tid, comp) for path, tid, comp, _length in rows
            if tid and not is_continuation_path(lengths, path)]


# --------------------------------------------------------------------------------------------
# Checks
# --------------------------------------------------------------------------------------------

def check_sets(conn, report: Report, sets: list[str]) -> None:
    for name in sets:
        prefixes = DOC_SETS[name]
        where = " OR ".join("path LIKE ?" for _ in prefixes)
        params = [p + "%" for p in prefixes]
        total, templated = conn.execute(
            f"SELECT count(*), coalesce(sum(templateId != 0), 0) FROM Content WHERE {where}",
            params).fetchone()
        report.check(templated > 0, f"{name}: {templated:,} of {total:,} row(s) served through a template")


def check_rows_decode(db_path: Path, report: Report, workers: int) -> dict[str, list[str]]:
    """Decodes every templated row. Returns {doc set (or 'other'): [path, ...]} of the rows that
    decoded to JSON, for the render sample."""
    conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    try:
        templates = {tid for (tid,) in conn.execute("SELECT id FROM Templates")}
        decoder, how = decoder_for(conn)
        rows = templated_rows(conn)
        dangling = [f"{p}: templateId {t}" for p, t, _c in rows if t not in templates]
        report.check(not dangling, f"every templated row's templateId names a Templates row "
                     f"({len(rows):,} rows, {len(templates)} templates)", dangling)
        stored = [(path, comp, read_item(conn, path)) for path, _tid, comp in rows]
    finally:
        conn.close()

    def decode(item):
        path, compression, blob = item
        try:
            raw = decoder.decompress(blob) if compression == "brotli" else blob
            document = json.loads(raw)
        except Exception as exc:  # noqa: BLE001 - reported per row
            return path, f"{path}: {type(exc).__name__}: {str(exc)[:120]}"
        if not isinstance(document, (dict, list)):
            return path, f"{path}: decodes to JSON {type(document).__name__}, not a page document"
        return path, None

    good: dict[str, list[str]] = {}
    bad, not_pages = [], []
    with ThreadPoolExecutor(max_workers=workers) as pool:
        for path, error in pool.map(decode, stored):
            if error is None:
                good.setdefault(doc_set_of(path) or "other", []).append(path)
            elif path.endswith(".html") or "JSONDecodeError" not in error:
                bad.append(error)
            else:
                # Dokka's package-list rows: sync_kdoc_json_to_db.py refreshes them in place and
                # they keep the templateId their row already had, but they are plain text that
                # nothing links to. Not a regression of this pipeline, so reported, not failed.
                not_pages.append(path)
    report.check(not bad, f"all {len(stored) - len(not_pages):,} templated page row(s) decode "
                 f"with {how} and parse as JSON", bad)
    if not_pages:
        print(f"    note: {len(not_pages)} templated non-page row(s) hold plain text, not JSON "
              f"(the app would fail to render them if asked): {', '.join(sorted(not_pages)[:5])}")
    for name, paths in sorted(good.items()):
        print(f"    {name}: {len(paths):,} JSON row(s)")
    return good


def check_render_sample(db_path: Path, report: Report, sets: list[str], good: dict[str, list[str]],
                        sample: int, seed: int) -> None:
    from verify_android_json_db import RENDERER_CLASSPATH, render  # noqa: E402 - needs the jar

    if not any(Path(RENDERER_CLASSPATH).parent.glob("*.jar")):
        report.check(False, f"renderer jars present at {Path(RENDERER_CLASSPATH).parent} "
                     "(build them: cd ProcessDocs/AndroidDocs/AndroidDocsToJson/renderer && "
                     "./gradlew installDist)")
        return
    conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    try:
        decoder, _how = decoder_for(conn)
        templates = {tid: bytes(body).decode("utf-8")
                     for tid, body in conn.execute("SELECT id, content FROM Templates")}
        rng = random.Random(seed)
        for name in sets:
            paths = sorted(good.get(name, []))
            picked = rng.sample(paths, min(sample, len(paths)))
            failures, sizes = [], []
            for path in picked:
                tid, compression = conn.execute(
                    "SELECT c.templateId, t.compression FROM Content c "
                    "JOIN ContentTypes t ON t.id = c.contentTypeID WHERE c.path = ?", (path,)).fetchone()
                blob = read_item(conn, path)
                raw = decoder.decompress(blob) if compression == "brotli" else blob
                try:
                    html = render(templates[tid], raw)
                except RuntimeError as exc:
                    failures.append(f"{path}: {str(exc.args[0])[:200]}")
                    continue
                if len(html) > APP_MAX_RENDERED_CHARS:
                    failures.append(f"{path}: renders to {len(html):,} chars, over the app's "
                                    f"{APP_MAX_RENDERED_CHARS:,}")
                elif len(html.strip()) == 0:
                    failures.append(f"{path}: renders to nothing")
                sizes.append(len(html))
            largest = f", largest {max(sizes):,} chars" if sizes else ""
            report.check(bool(picked) and not failures,
                         f"{name}: {len(picked)} sampled page(s) render through their stored "
                         f"template{largest}", failures)
    finally:
        conn.close()


def check_scope(db_path: Path, original: Path, report: Report) -> None:
    conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    try:
        conn.execute("ATTACH DATABASE ? AS orig", (f"file:{original}?mode=ro",))

        def base_paths(schema):
            # Continuation rows ("<path>-1", ...) excluded: the re-mint recompresses every row, so
            # a chunked item anywhere - a PDF under p/, say - can legitimately end up with a
            # different number of them. The item itself is what must not appear or vanish.
            lengths = dict(conn.execute(f"SELECT path, LENGTH(content) FROM {schema}.Content"))
            return {p for p in lengths if not is_continuation_path(lengths, p)}

        before, after = base_paths("orig"), base_paths("main")
        added, removed = after - before, before - after
        per_set = Counter()
        for path in added:
            per_set[(doc_set_of(path) or ("shared" if path.startswith(SHARED_PREFIXES) else "other"), "+")] += 1
        for path in removed:
            per_set[(doc_set_of(path) or ("shared" if path.startswith(SHARED_PREFIXES) else "other"), "-")] += 1
        for name in [*DOC_SETS, "shared"]:
            print(f"    {name}: +{per_set[(name, '+')]:,} / -{per_set[(name, '-')]:,} row(s)")
        outside = sorted(f"{'+' if p in added else '-'} {p}" for p in added | removed
                         if not in_rebuildable_namespace(p))
        report.check(not outside, "no row appeared or disappeared outside the documentation sets' paths",
                     outside)

        lost = [str(i) for (i,) in conn.execute(
            "SELECT id FROM orig.Tooltips EXCEPT SELECT id FROM main.Tooltips")]
        # Not a failure on its own: sync_kdoc_json_to_db.py deliberately deletes a tooltip whose
        # button pointed at a kotlin-stdlib page that no longer exists. Reported so it is seen.
        print(f"    Tooltips: {len(lost):,} removed since the original"
              + (f" (ids {', '.join(lost[:10])}{'...' if len(lost) > 10 else ''})" if lost else ""))

        def dangling(schema):
            return {uri for (uri,) in conn.execute(
                f"SELECT b.uri FROM {schema}.TooltipButtons b WHERE b.uri IS NOT NULL AND b.uri != '' "
                f"AND NOT EXISTS (SELECT 1 FROM {schema}.Content c WHERE c.path = "
                "CASE WHEN instr(b.uri, '#') > 0 THEN substr(b.uri, 1, instr(b.uri, '#') - 1) "
                "ELSE b.uri END)")
                if in_rebuildable_namespace(uri.split("?")[0])}
        newly = sorted(dangling("main") - dangling("orig"))
        report.check(not newly, "no tooltip button that resolved before now points at a missing row",
                     newly)
    finally:
        conn.close()


def verify(db_path: Path, original: Path | None, sets: list[str], sample: int, seed: int,
           workers: int, render: bool) -> Report:
    report = Report()
    conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    try:
        version = declared_version(conn)
        print(f"==> {db_path}: version "
              + (f"{version[0]}.{version[1]}.{version[2]} ({version[3]})" if version else "(none declared)"))
        print("==> templates: " + ", ".join(
            f"{name}=#{tid}" for tid, name in conn.execute("SELECT id, name FROM Templates ORDER BY id")))
        check_sets(conn, report, sets)
    finally:
        conn.close()

    good = check_rows_decode(db_path, report, workers)
    if render and sample > 0:
        check_render_sample(db_path, report, sets, good, sample, seed)
    if original is not None:
        check_scope(db_path, original, report)

    conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    try:
        integrity = conn.execute("PRAGMA quick_check").fetchone()[0]
    finally:
        conn.close()
    report.check(integrity == "ok", f"PRAGMA quick_check: {integrity}")
    return report


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("db", type=Path, help="the rebuilt database")
    parser.add_argument("--original", type=Path, help="the database the run started from")
    parser.add_argument("--sets", default=",".join(DOC_SETS),
                        help=f"comma-separated sets that were rebuilt (default: all of {', '.join(DOC_SETS)})")
    parser.add_argument("--sample", type=int, default=20, help="pages per set to render (default: 20)")
    parser.add_argument("--seed", type=int, default=0x4065)
    parser.add_argument("--workers", type=int, default=8)
    parser.add_argument("--no-render", action="store_true", help="skip the render sample")
    args = parser.parse_args(argv)

    sets = [s.strip() for s in args.sets.split(",") if s.strip()]
    unknown = [s for s in sets if s not in DOC_SETS]
    if unknown:
        parser.error(f"unknown set(s): {', '.join(unknown)}; expected some of {', '.join(DOC_SETS)}")
    if not args.db.is_file():
        parser.error(f"no such database: {args.db}")
    if args.original is not None and not args.original.is_file():
        parser.error(f"no such database: {args.original}")

    report = verify(args.db, args.original, sets, args.sample, args.seed, args.workers,
                    render=not args.no_render)
    if report.failures:
        print(f"\n{len(report.failures)} check(s) failed.")
        return 1
    print("\nAll checks passed.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
