#!/usr/bin/env python3
"""Compares a Javadoc-mode JSON tree against the official javadoc HTML it is meant to mirror.

Reports, at each level of the api/ tree, what is in one side and not the other:

  modules   -- directories with a module-summary page
  packages  -- directories with a package-summary page
  types     -- class/interface/enum/record/annotation pages
  members   -- the anchors on each type page (fields, constructors, methods)

Member anchors are the sharpest check of the four: javadoc's anchor encodes a member's name and
its *erased* parameter types, so a matching anchor set means the two sides agree on the members,
their signatures, and their overload resolution -- not merely on the page count.

Exit status is 0 when every level matches, 1 otherwise.
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path

# javadoc's own non-API pages, which have no JSON counterpart by design.
NON_TYPE_PAGES = {
    "package-summary", "package-tree", "package-use", "module-summary", "module-graph",
    "allclasses-index", "allpackages-index", "constant-values", "deprecated-list",
    "help-doc", "index", "overview-tree", "serialized-form", "system-properties",
    "search", "new-list", "preview-list",
}
SKIP_DIRS = {"index-files", "class-use", "doc-files", "legal", "resources", "specs"}

MEMBER_ANCHOR = re.compile(r'<section class="detail" id="([^"]+)"')
HTML_ENTITY = re.compile(r"&(?:lt|gt|amp|quot|#(\d+));")


def unescape(text: str) -> str:
    return (text.replace("&lt;", "<").replace("&gt;", ">")
                .replace("&quot;", '"').replace("&amp;", "&"))


def html_modules(api: Path) -> set[str]:
    return {p.parent.name for p in api.glob("*/module-summary.html")}


def json_modules(out: Path) -> set[str]:
    return {p.parent.name for p in out.glob("*/module-summary.json")}


def _relative_package(page: Path, root: Path) -> str | None:
    rel = page.parent.relative_to(root)
    parts = rel.parts
    if not parts:
        return None
    # Drop the leading module directory when the tree is modular.
    return ".".join(parts[1:]) if len(parts) > 1 else ".".join(parts)


def html_packages(api: Path, modular: bool) -> set[str]:
    result = set()
    for page in api.rglob("package-summary.html"):
        if any(part in SKIP_DIRS for part in page.parts):
            continue
        parts = page.parent.relative_to(api).parts
        result.add(".".join(parts[1:] if modular else parts))
    return result


def json_packages(out: Path, modular: bool) -> set[str]:
    result = set()
    for page in out.rglob("package-summary.json"):
        parts = page.parent.relative_to(out).parts
        result.add(".".join(parts[1:] if modular else parts))
    return result


def html_types(api: Path, modular: bool) -> set[str]:
    result = set()
    for page in api.rglob("*.html"):
        if any(part in SKIP_DIRS for part in page.parts):
            continue
        if page.stem in NON_TYPE_PAGES:
            continue
        parts = page.parent.relative_to(api).parts
        package = ".".join(parts[1:] if modular else parts)
        if not package:
            continue
        result.add(f"{package}.{page.stem}")
    return result


def json_types(out: Path, modular: bool) -> set[str]:
    result = set()
    for page in out.rglob("*.json"):
        if any(part in SKIP_DIRS for part in page.parts):
            continue
        if page.stem in NON_TYPE_PAGES:
            continue
        parts = page.parent.relative_to(out).parts
        package = ".".join(parts[1:] if modular else parts)
        if not package:
            continue
        result.add(f"{package}.{page.stem}")
    return result


def html_member_anchors(page: Path) -> set[str]:
    text = page.read_text(encoding="utf-8", errors="replace")
    return {unescape(a) for a in MEMBER_ANCHOR.findall(text)}


def json_member_anchors(page: Path) -> set[str]:
    data = json.loads(page.read_text(encoding="utf-8"))
    anchors = set()
    for key in ("fields", "enumConstants", "constructors", "methods", "annotationElements"):
        for member in data.get(key) or []:
            anchors.add(member["anchor"])
    return anchors


def report(label: str, expected: set[str], actual: set[str], limit: int) -> bool:
    missing = sorted(expected - actual)
    extra = sorted(actual - expected)
    status = "OK " if not missing and not extra else "DIFF"
    print(f"[{status}] {label}: {len(actual)}/{len(expected)} "
          f"(missing {len(missing)}, extra {len(extra)})")
    for name in missing[:limit]:
        print(f"         missing: {name}")
    if len(missing) > limit:
        print(f"         ... and {len(missing) - limit} more missing")
    for name in extra[:limit]:
        print(f"         extra:   {name}")
    if len(extra) > limit:
        print(f"         ... and {len(extra) - limit} more extra")
    return not missing and not extra


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("json_dir", type=Path, help="the generated javadoc-mode JSON tree")
    parser.add_argument("html_dir", type=Path, help="the official javadoc api/ directory")
    parser.add_argument("--limit", type=int, default=10, help="how many names to list per difference")
    parser.add_argument("--members", action="store_true",
                        help="also compare the member anchors of every type (slower)")
    args = parser.parse_args()

    for path in (args.json_dir, args.html_dir):
        if not path.is_dir():
            print(f"Error: {path} is not a directory", file=sys.stderr)
            return 2

    ok = True
    expected_modules = html_modules(args.html_dir)
    actual_modules = json_modules(args.json_dir)
    modular = bool(expected_modules)
    ok &= report("modules", expected_modules, actual_modules, args.limit)
    ok &= report("packages",
                 html_packages(args.html_dir, modular),
                 json_packages(args.json_dir, modular), args.limit)

    expected_types = html_types(args.html_dir, modular)
    actual_types = json_types(args.json_dir, modular)
    ok &= report("types", expected_types, actual_types, args.limit)

    if args.members:
        shared = sorted(expected_types & actual_types)
        html_index = {}
        for page in args.html_dir.rglob("*.html"):
            if page.stem in NON_TYPE_PAGES or any(p in SKIP_DIRS for p in page.parts):
                continue
            parts = page.parent.relative_to(args.html_dir).parts
            package = ".".join(parts[1:] if modular else parts)
            if package:
                html_index[f"{package}.{page.stem}"] = page
        json_index = {}
        for page in args.json_dir.rglob("*.json"):
            if page.stem in NON_TYPE_PAGES or any(p in SKIP_DIRS for p in page.parts):
                continue
            parts = page.parent.relative_to(args.json_dir).parts
            package = ".".join(parts[1:] if modular else parts)
            if package:
                json_index[f"{package}.{page.stem}"] = page

        total = matched = 0
        differing: list[tuple[str, int, int]] = []
        for name in shared:
            try:
                expected = html_member_anchors(html_index[name])
                actual = json_member_anchors(json_index[name])
            except Exception as exc:  # a malformed page shouldn't abort the whole comparison
                print(f"         error reading {name}: {exc}")
                continue
            total += 1
            if expected == actual:
                matched += 1
            else:
                differing.append((name, len(expected - actual), len(actual - expected)))

        print(f"[{'OK ' if matched == total else 'DIFF'}] member anchors: "
              f"{matched}/{total} types match exactly")
        for name, missing, extra in differing[:args.limit]:
            print(f"         {name}: missing {missing}, extra {extra}")
        if len(differing) > args.limit:
            print(f"         ... and {len(differing) - args.limit} more types differ")
        ok &= matched == total

    print()
    print("MATCH" if ok else "DIFFERENCES FOUND")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
