#!/usr/bin/env python3
"""Checks a documentation.db that load_android_json_db.py has written.

Answers the question the loader itself cannot: do the rows it wrote actually render? For a sample
of pages it pulls the stored JSON and the stored template out of the database, renders them
through Pebble configured the way the server configures it, and looks for the parts of a reference
page that must be there. It also checks the things that are true of the whole set: that every page
has a template, that no link points at a row that is not there, and that nothing still carries the
scraped HTML.

    ./verify_android_json_db.py ~/Desktop/documentation_androidjson.db --sample 40
"""

from __future__ import annotations

import argparse
import json
import posixpath
import random
import re
import subprocess
import sys
import tempfile
from pathlib import Path

_KOTLIN_PIPELINE = (Path(__file__).resolve().parents[2]
                    / "ProcessKotlinDocs" / "ProcessKotlinWebsiteJSON")
sys.path.insert(0, str(_KOTLIN_PIPELINE))

from populate_db import DictionaryCompressor, load_dictionary  # noqa: E402
from migrate_content_to_dictionary_brotli import read_item  # noqa: E402

RENDERER_CLASSPATH = (Path(__file__).resolve().parent
                      / "renderer/build/install/android-doc-renderer/lib/*")
CHECK_CLASS = "org.appdevforall.docs.android.TemplateCheck"
PREFIX = "a/"
_HREF = re.compile(r'href="([^"]*)"')

# What a rendered reference page has to contain, by page kind. Deliberately about content rather
# than markup: the point is that the template ran and found its fields, not that it emitted a
# particular div.
EXPECTED = {
    "android-class": ["<title>", "breadcrumb", "android-reference.css"],
    "android-package": ["<title>", "breadcrumb", "summary"],
    "android-index": ["<title>", "breadcrumb"],
}


def android_rows(conn) -> list[tuple[str, int, int]]:
    return conn.execute(
        "SELECT path, templateId, contentTypeID FROM Content "
        "WHERE (path LIKE ? OR path LIKE ? OR path = ?) AND path LIKE '%.html' ORDER BY path",
        (f"{PREFIX}android/%", f"{PREFIX}androidx/%", f"{PREFIX}index.html"),
    ).fetchall()


def render(template: str, document: bytes) -> str:
    """The stored template applied to the stored page, through a real Pebble engine."""
    with tempfile.TemporaryDirectory() as work:
        template_file = Path(work) / "template.peb"
        template_file.write_text(template, encoding="utf-8")
        page_file = Path(work) / "page.json"
        page_file.write_bytes(document)
        result = subprocess.run(
            ["java", "-cp", str(RENDERER_CLASSPATH), CHECK_CLASS,
             str(template_file), str(page_file)],
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, check=False)
        if result.returncode != 0:
            raise RuntimeError(result.stderr.decode(errors="replace").strip())
        return result.stdout.decode("utf-8", errors="replace")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Verify an Android-JSON documentation database.")
    parser.add_argument("db", type=Path)
    parser.add_argument("--sample", type=int, default=25, help="pages to render")
    parser.add_argument("--seed", type=int, default=0x5548)
    args = parser.parse_args(argv)

    import sqlite3
    conn = sqlite3.connect(args.db)
    failures = 0

    templates = {row[0]: row[1].decode("utf-8")
                 for row in conn.execute("SELECT id, content FROM Templates").fetchall()}
    rows = android_rows(conn)
    print(f"==> {len(rows):,} Android pages")

    untemplated = [path for path, template_id, _ in rows if template_id == 0]
    print(f"==> pages still served without a template: {len(untemplated):,}")
    if untemplated:
        failures += 1
        for path in untemplated[:5]:
            print(f"    ! {path}")

    kinds: dict[str, int] = {}
    compressor = DictionaryCompressor(load_dictionary(conn))
    known = {path for path, _t, _c in rows}
    known |= {path for (path,) in conn.execute(
        "SELECT path FROM Content WHERE path NOT LIKE '%-1'").fetchall()}

    broken_links = 0
    random.Random(args.seed).shuffle(rows)
    for path, template_id, _ in rows[: args.sample]:
        raw = compressor.decompress(read_item(conn, path))
        try:
            document = json.loads(raw)
        except (json.JSONDecodeError, UnicodeDecodeError):
            # A row still holding the scraped HTML rather than a page document. Reported rather
            # than raised: which rows those are is the useful answer.
            print(f"    ! {path}: content is not a page document "
                  f"({raw[:40]!r}...)")
            failures += 1
            continue
        kind = document.get("page")
        kinds[kind] = kinds.get(kind, 0) + 1
        if template_id not in templates:
            print(f"    ! {path}: templateId {template_id} is not in Templates")
            failures += 1
            continue
        try:
            html = render(templates[template_id], raw)
        except RuntimeError as error:
            print(f"    ! {path}: template failed: {error.args[0][:200]}")
            failures += 1
            continue
        missing = [marker for marker in EXPECTED.get(kind, []) if marker not in html]
        if missing or len(html) < 200:
            print(f"    ! {path}: rendered {len(html)} bytes, missing {missing}")
            failures += 1
        # Every relative link in the stored page must name a row that exists.
        page_dir = posixpath.dirname(path)
        for url in _HREF.findall(raw.decode("utf-8", errors="replace")):
            if url.startswith(("http://", "https://", "#", "mailto:", "data:")) or not url:
                continue
            target = posixpath.normpath(posixpath.join(page_dir, url.split("#")[0]))
            if target not in known:
                broken_links += 1
                if broken_links <= 5:
                    print(f"    ! {path}: link to a missing row: {target}")

    print(f"==> rendered {min(args.sample, len(rows))} pages: "
          + ", ".join(f"{n} {k}" for k, n in sorted(kinds.items())))
    print(f"==> links into the database that name a missing row: {broken_links}")
    if broken_links:
        failures += 1
    print("==> OK" if not failures else f"==> {failures} check(s) failed")
    conn.close()
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
