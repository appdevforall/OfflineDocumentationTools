#!/usr/bin/env python3
"""Flattens the pebble-renderer templates into standalone templates for documentation.db.

The renderer in `pebble-renderer/` and the reader that serves documentation.db run Pebble in two
different environments, and the database's is the narrower one:

  * Templates are stored one per row in `Templates`, with no loader that can resolve
    `{% extends "base" %}` or `{% import "macros" %}` by name. Every template already in the
    database (page.peb, nav.peb, layout.pebble) is self-contained, so that is the contract.
  * Only Pebble's built-in filters are available. The renderer's `href` and `doc` filters are Java
    classes that ship with it and are not there.

Rather than maintain a second, divergent copy of the templates by hand, this generates them:

  * `{% extends %}` is resolved by substituting the child's `{% block %}` bodies into the parent.
  * `{% import %}` is resolved by appending the imported macro definitions.
  * `| href` is dropped and `| doc` becomes `| raw`. Both are safe because sync_javadoc_json_to_db.py
    rewrites the JSON's `.json` links to `.html` before insertion, which is the only thing those
    filters did beyond marking documentation HTML as trusted.

Run it whenever the pebble-renderer templates change.
"""

from __future__ import annotations

import argparse
import re
import sys
from pathlib import Path

BLOCK_RE = re.compile(r"\{%-?\s*block\s+(\w+)\s*-?%\}(.*?)\{%-?\s*endblock\s*-?%\}", re.DOTALL)
EXTENDS_RE = re.compile(r"\{%-?\s*extends\s+\"([^\"]+)\"\s*-?%\}")
IMPORT_RE = re.compile(r"\{%-?\s*import\s+\"([^\"]+)\"\s*-?%\}")
MACRO_RE = re.compile(r"\{%-?\s*macro\s+\w+.*?\{%-?\s*endmacro\s*-?%\}", re.DOTALL)

# page kind (the JSON's `page` field) -> source template
PAGES = {
    "class": "class",
    "package": "package-summary",
    "module": "module-summary",
    "overview": "overview",
    "all-classes": "all-classes",
    "all-packages": "all-packages",
    "deprecated-list": "deprecated-list",
    "constant-values": "constant-values",
    "index": "index-page",
}


def load(directory: Path, name: str) -> str:
    return (directory / f"{name}.peb").read_text(encoding="utf-8")


def flatten(directory: Path, name: str) -> str:
    source = load(directory, name)

    imported_macros: list[str] = []
    for imported in IMPORT_RE.findall(source):
        imported_macros.extend(MACRO_RE.findall(load(directory, imported)))
    source = IMPORT_RE.sub("", source)

    extends = EXTENDS_RE.search(source)
    if extends:
        parent = load(directory, extends.group(1))
        blocks = {n: b for n, b in BLOCK_RE.findall(source)}
        # The parent's own block bodies are the defaults for blocks the child doesn't override.
        parent = BLOCK_RE.sub(lambda m: blocks.get(m.group(1), m.group(2)), parent)
        # Anything outside a block in a child template is discarded by Pebble, and macros the
        # child defines itself must survive, so they are carried over explicitly.
        own_macros = MACRO_RE.findall(EXTENDS_RE.sub("", source))
        source = parent + "\n" + "\n".join(own_macros)

    source = "\n".join([source, *imported_macros])

    # The two renderer-only filters. `href` did nothing but swap the extension, which the sync
    # script now does to the data itself; `doc` additionally marked the value as trusted HTML,
    # which is Pebble's built-in `raw`.
    source = re.sub(r"\|\s*href\b", "", source)
    source = re.sub(r"\|\s*doc\b", "| raw", source)

    if re.search(r"\{%-?\s*(extends|import|include)\b", source):
        raise SystemExit(f"{name}: template still references another template after flattening")
    if re.search(r"\|\s*(href|doc)\b", source):
        raise SystemExit(f"{name}: template still uses a renderer-only filter after flattening")
    return source


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--templates", type=Path,
                        default=Path(__file__).resolve().parents[2] / "Dokka-plugin-kdoc2json/pebble-renderer/src/main/resources/templates",
                        help="the pebble-renderer template directory to flatten")
    parser.add_argument("--out", type=Path, default=Path(__file__).resolve().parent / "db-templates",
                        help="where to write the flattened templates")
    args = parser.parse_args()

    if not args.templates.is_dir():
        print(f"Error: {args.templates} is not a directory", file=sys.stderr)
        return 2

    args.out.mkdir(parents=True, exist_ok=True)
    for kind, name in sorted(PAGES.items()):
        flattened = flatten(args.templates, name)
        target = args.out / f"javadoc-{kind}.peb"
        target.write_text(flattened, encoding="utf-8")
        print(f"  {target.name:32s} {len(flattened):6d} bytes   (page kind '{kind}')")
    print(f"Wrote {len(PAGES)} template(s) to {args.out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
