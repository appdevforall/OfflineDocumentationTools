#!/usr/bin/env python3
"""Loads the Android JSON documentation tree into a documentation.db.

The database already carries the Android reference as 12,106 rows of scraped HTML, each with the
site's whole stylesheet inlined into it and no structure a caller can address. This replaces those
rows with the JSON that `android_docs_to_json.py` produces, plus a Pebble template per page kind,
so the server renders a page from its fields the way it already does for the Kotlin docs: a row's
`templateId` names a template, and the row's JSON becomes that template's context.

What goes in:

  Templates          one self-contained template per page kind, each being a page template with
                     the shared macros appended -- the only shape a Templates row can hold
  assets/            the one stylesheet all 12,906 pages link to, instead of a copy in each
  Content            one row per page, at the path the scraped HTML row used, so every link that
                     already pointed at it still resolves

The source database is never written to. Everything happens on a copy, which is the artifact.

    ./load_android_json_db.py <json-dir> --source ~/Desktop/documentation.db \
        --out ~/Desktop/documentation_androidjson.db
"""

from __future__ import annotations

import argparse
import json
import os
import posixpath
import re
import shutil
import sqlite3
import sys
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

# The row-writing machinery lives with the Kotlin pipeline, which is where it was written and
# where the reasoning is documented at length: the exact chunk boundary WebServer.kt reassembles
# on, the AddBook/DeleteBook triggers that make a base-row DELETE unsafe, and the rule that an
# existing CompressionDictionary is never retrained. Reusing it is the point -- a second
# implementation of any of that would be a second chance to get it wrong.
_KOTLIN_PIPELINE = (Path(__file__).resolve().parents[2]
                    / "ProcessKotlinDocs" / "ProcessKotlinWebsiteJSON")
sys.path.insert(0, str(_KOTLIN_PIPELINE))

from populate_db import (  # noqa: E402  (the path has to be set up first)
    DictionaryCompressor, fragment_chain, get_content_type, get_id, insert_chunked_content,
    load_dictionary, upsert_template,
)
from migrate_content_to_dictionary_brotli import write_item  # noqa: E402

# Where the Android documentation lives in the served namespace. The scraped HTML rows are already
# under this prefix, and keeping it means every link elsewhere in the database still lands.
DB_PREFIX = "a/"
# Served as text/html because that is what the response is: the server renders the JSON through
# the row's template. The Kotlin pages the database already holds are stored the same way.
CONTENT_TYPE = "text/html"
STYLESHEET_PATH = "assets/android-reference.css"
# `page` field -> (Templates.name, template file). The stored name carries the `android-` prefix
# because Templates.name is unique across every documentation set in the database.
TEMPLATES = {
    "android-class": ("android-class.peb", "class"),
    "android-package": ("android-package.peb", "package"),
    "android-index": ("android-index.peb", "index"),
}
TEMPLATE_DIR = Path(__file__).resolve().parent / "renderer/src/main/resources/templates"
STYLESHEET_SOURCE = Path(__file__).resolve().parent / "renderer/src/main/resources/static/stylesheet.css"

_HREF = re.compile(r'href="([^"]*)"')


# --------------------------------------------------------------------------------------------
# Paths and links
# --------------------------------------------------------------------------------------------

def db_path_for(relative: str) -> str:
    """The served path for a JSON file's tree-relative path."""
    return DB_PREFIX + relative[: -len(".json")] + ".html"


def existing_android_paths(conn) -> dict:
    """Case-folded served path -> the path this database actually uses.

    The scrape is read off a case-insensitive filesystem, where `android.os.strictmode` (a
    package) and `android.os.StrictMode` (a class) cannot both have their own directory: the
    package's 25 pages sit under `StrictMode/` on disk. The database was built somewhere that
    could tell them apart and has them under `strictmode/`, which is also what every link in the
    corpus says. So the database's spelling wins -- for the row paths and for the links -- or
    those 25 pages would be written beside the rows they are meant to replace and linked to at a
    path that does not exist.
    """
    rows = conn.execute(
        "SELECT path FROM Content WHERE path LIKE ? OR path LIKE ?",
        (f"{DB_PREFIX}android/%", f"{DB_PREFIX}androidx/%"),
    ).fetchall()
    return {path.casefold(): path for (path,) in rows}


def canonical(path: str, known: dict) -> str:
    """`path` as this database spells it, when it holds the same page under a different case."""
    return known.get(path.casefold(), path)


def rewrite_link(url: str, page_dir: str, known: dict) -> str:
    """One link, from the JSON tree into the served namespace.

    Two changes, and only for a link that stays inside the corpus: the extension the extractor
    wrote for its own tree, and the case the database spells the target with. The link is resolved
    against the page's directory to canonicalise it and then made relative again, because that is
    the form the extractor emits and the form a browser resolves the same way.
    """
    if not url or url.startswith(("http://", "https://", "#", "mailto:", "data:")):
        return url
    path, sep, fragment = url.partition("#")
    fragment = sep + fragment
    if path.endswith(".json"):
        path = path[: -len(".json")] + ".html"
    if not path or path.startswith("/"):
        return path + fragment
    absolute = posixpath.normpath(posixpath.join(page_dir, path))
    fixed = canonical(absolute, known)
    return posixpath.relpath(fixed, page_dir) + fragment


def rewrite_document(node, page_dir: str, known: dict):
    """Every link in a page document, rewritten.

    HtmlLinks.java walks the document the same way for the standalone renderer, but the two are
    not interchangeable: `rewrite_link` below also canonicalises each path's case against what
    this database holds, which the renderer has no need of. See that class's own header.
    """
    if isinstance(node, dict):
        result = {}
        for key, value in node.items():
            if key == "url" and isinstance(value, str):
                result[key] = rewrite_link(value, page_dir, known)
            else:
                result[key] = rewrite_document(value, page_dir, known)
        return result
    if isinstance(node, list):
        return [rewrite_document(item, page_dir, known) for item in node]
    if isinstance(node, str) and 'href="' in node:
        return _HREF.sub(
            lambda m: 'href="' + rewrite_link(m.group(1), page_dir, known) + '"', node)
    return node


def served_links(document: dict, db_path: str) -> dict:
    """The three links a template needs that are not documentation: the stylesheet, the root index,
    and this page's own package summary. Absolute, because the server resolves a leading slash
    against Content.path."""
    links = {"stylesheetUrl": "/" + STYLESHEET_PATH,
             "indexUrl": "/" + DB_PREFIX + "index.html"}
    package = document.get("packageName")
    if package and document.get("page") == "android-class":
        links["packageUrl"] = "/" + posixpath.join(posixpath.dirname(db_path),
                                                   "package-summary.html")
    return links


# --------------------------------------------------------------------------------------------
# Loading
# --------------------------------------------------------------------------------------------

def assemble_template(name: str) -> str:
    """A page template with the shared macros appended, as a Templates row has to hold it.

    The same assembly the standalone renderer does. Macros are visible only inside the file that
    defines them and a Templates row is one self-contained template, so the two halves are joined
    rather than imported.
    """
    page = (TEMPLATE_DIR / f"{name}.peb").read_text(encoding="utf-8")
    macros = (TEMPLATE_DIR / "_macros.peb").read_text(encoding="utf-8")
    for directive in ("{% extends", "{% import"):
        if directive in page or directive in macros:
            raise RuntimeError(f"{name}.peb is not self-contained: it uses {directive!r}")
    return page + "\n" + macros


def store_page(conn, path: str, language_id: int, content_type_id: int, template_id: int,
               data: bytes, chunked: list) -> bool:
    """Writes one page's bytes, replacing what is there or adding it. Returns whether it replaced.

    The two halves come from the Kotlin pipeline and each does one of the jobs: `write_item`
    UPDATEs a base row that exists and reconciles its continuation rows, and
    `insert_chunked_content` INSERTs one that does not. Which to call has to be decided here,
    because `write_item`'s UPDATE on an absent path succeeds while writing nothing -- it would
    lose the page silently.

    `write_item` rewrites only the bytes, which is right for what it was written for:
    recompressing a row whose type and template are staying as they are. Here they are not. A
    scraped page is `templateId` 0 and becomes a templated row, so the columns that say how to
    serve it are set afterwards -- on the base row and on any continuation row, found by exact
    path rather than by a LIKE that a `_` in a path would widen.
    """
    existed = conn.execute("SELECT 1 FROM Content WHERE path = ?", (path,)).fetchone() is not None
    if existed:
        write_item(conn, path, language_id, content_type_id, template_id, data)
        for row_path in [path] + [fragment for _n, fragment in fragment_chain(conn, path)]:
            conn.execute("UPDATE Content SET languageID = ?, contentTypeID = ?, templateId = ? "
                         "WHERE path = ?", (language_id, content_type_id, template_id, row_path))
    else:
        insert_chunked_content(conn, path, language_id, content_type_id, template_id, data, chunked)
    return existed


def prepare(job: tuple, known: dict, compressor: DictionaryCompressor) -> tuple:
    """Reads one page, rewrites its links, and compresses it. Returns (db_path, kind, bytes).

    Split out so the compressing -- a brotli q11 subprocess per page, which is where the time
    goes -- can run on a pool while the writing stays on the one connection.
    """
    source, relative = job
    document = json.loads(Path(source).read_text(encoding="utf-8"))
    db_path = canonical(db_path_for(relative), known)
    page_dir = posixpath.dirname(db_path)
    document = rewrite_document(document, page_dir, known)
    document.update(served_links(document, db_path))
    payload = json.dumps(document, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    return db_path, document.get("page"), compressor.compress(payload)


def load(json_dir: Path, conn, workers: int, limit: int | None) -> dict:
    stats = {"updated": 0, "inserted": 0, "skipped": 0, "stored": 0}
    language_id = get_id(conn, "Languages", "en-US")
    content_type_id, compress = get_content_type(conn, CONTENT_TYPE)
    if not compress:
        raise RuntimeError(f"ContentTypes says {CONTENT_TYPE} is uncompressed; expected brotli")
    compressor = DictionaryCompressor(load_dictionary(conn))

    template_ids = {kind: upsert_template(conn, stored, assemble_template(source))
                    for kind, (stored, source) in TEMPLATES.items()}
    print("==> templates: " + ", ".join(f"{stored}=#{template_ids[kind]}"
                                        for kind, (stored, _) in TEMPLATES.items()))

    stylesheet = STYLESHEET_SOURCE.read_bytes()
    css_type_id, css_compress = get_content_type(conn, "text/css")
    store_page(conn, STYLESHEET_PATH, language_id, css_type_id, 0,
               compressor.compress(stylesheet) if css_compress else stylesheet, [])
    print(f"==> stylesheet: {STYLESHEET_PATH} ({len(stylesheet):,} bytes)")

    known = existing_android_paths(conn)
    jobs = sorted((str(p), p.relative_to(json_dir).as_posix())
                  for p in json_dir.rglob("*.json"))
    if limit:
        jobs = jobs[:limit]
    print(f"==> {len(jobs):,} pages")

    chunked: list = []
    start = time.time()
    with ThreadPoolExecutor(max_workers=workers) as pool:
        for done, (db_path, kind, payload) in enumerate(
                pool.map(lambda job: prepare(job, known, compressor), jobs), 1):
            template_id = template_ids.get(kind)
            if template_id is None:
                print(f"  ! {db_path}: no template for page kind {kind!r}", file=sys.stderr)
                stats["skipped"] += 1
                continue
            existed = store_page(conn, db_path, language_id, content_type_id, template_id,
                                 payload, chunked)
            stats["updated" if existed else "inserted"] += 1
            stats["stored"] += len(payload)
            if done % 2000 == 0:
                conn.commit()
                print(f"    {done:,}/{len(jobs):,}  ({time.time() - start:.0f}s)")
    conn.commit()
    if chunked:
        print(f"==> {len(chunked)} page(s) needed continuation rows: "
              + ", ".join(f"{p} ({n} chunks)" for p, _size, n in chunked[:5]))
    return stats


# Every table this script reads or writes. Checked up front (see check_schema) because two of
# them are only touched by record_version, at the very end: a database missing them would
# otherwise take the whole load - 90 seconds locally, a quarter of an hour in CI - before saying
# so. The same fail-fast the media optimizer does for its own content types.
REQUIRED_TABLES = ("Content", "Languages", "ContentTypes", "Templates", "CompressionDictionary",
                   "DocumentationDatabaseVersion", "LastChange")


def check_schema(conn) -> list[str]:
    """The tables in REQUIRED_TABLES this database does not have, in the order listed."""
    present = {name for (name,) in conn.execute(
        "SELECT name FROM sqlite_master WHERE type = 'table'")}
    return [name for name in REQUIRED_TABLES if name not in present]


def record_version(conn, stats: dict) -> None:
    """A row saying what changed, as the schema's own comment asks for. Minor, not major: nothing
    about the schema or the server's contract changes, a documentation set is re-expressed."""
    row = conn.execute(
        "SELECT major, minor, patch FROM DocumentationDatabaseVersion "
        "ORDER BY changeTime DESC, rowid DESC LIMIT 1").fetchone()
    major, minor, _ = row if row else (2, 0, 0)
    conn.execute(
        "INSERT INTO DocumentationDatabaseVersion (major, minor, patch, who, comment) "
        "VALUES (?, ?, ?, ?, ?)",
        (major, minor + 1, 0, "ADFA-5548",
         f"Serve the Android reference from JSON through Pebble templates "
         f"({stats['updated']:,} rows replaced, {stats['inserted']:,} added)"),
    )
    conn.execute("INSERT INTO LastChange (documentationSet, who) VALUES (?, ?)",
                 ("android", "ADFA-5548"))


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Load the Android JSON documentation tree into a documentation.db.")
    parser.add_argument("json_dir", type=Path, help="output of android_docs_to_json.py")
    parser.add_argument("--source", required=True, type=Path,
                        help="database to start from; never written to")
    parser.add_argument("--out", required=True, type=Path, help="artifact to write")
    parser.add_argument("--workers", type=int, default=os.cpu_count() or 4)
    parser.add_argument("--limit", type=int, help="stop after this many pages (for a smoke run)")
    parser.add_argument("--force", action="store_true", help="overwrite an existing --out")
    args = parser.parse_args(argv)

    if not args.json_dir.is_dir():
        parser.error(f"not a directory: {args.json_dir}")
    if not args.source.is_file():
        parser.error(f"no such database: {args.source}")
    if args.out.exists() and not args.force:
        parser.error(f"{args.out} exists; pass --force to replace it")
    if args.out.resolve() == args.source.resolve():
        parser.error("--out must not be --source: the source database is never written to")

    source_conn = sqlite3.connect(f"file:{args.source}?mode=ro", uri=True)
    try:
        missing = check_schema(source_conn)
    finally:
        source_conn.close()
    if missing:
        parser.error(f"{args.source} is missing table(s) this loader writes: "
                     + ", ".join(missing))

    print(f"==> copying {args.source} -> {args.out}")
    shutil.copy2(args.source, args.out)

    conn = sqlite3.connect(args.out)
    try:
        conn.execute("PRAGMA foreign_keys = ON")
        stats = load(args.json_dir, conn, args.workers, args.limit)
        record_version(conn, stats)
        conn.commit()
        print("==> VACUUM")
        conn.execute("VACUUM")
    finally:
        conn.close()

    print(f"==> {stats['updated']:,} rows replaced, {stats['inserted']:,} added, "
          f"{stats['skipped']:,} skipped")
    print(f"==> {stats['stored'] / 1e6:.0f} MB of compressed page content")
    print(f"==> {args.out} is {args.out.stat().st_size / 1e6:.0f} MB")
    return 1 if stats["skipped"] else 0


if __name__ == "__main__":
    sys.exit(main())
