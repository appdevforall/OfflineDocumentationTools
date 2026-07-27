#!/usr/bin/env python3
"""
Verifies that every object listed in a kdoc-to-json package index.json actually
corresponds to a real documented page: the URL it points to must resolve to an
existing file, and that file's own top-level "dri" must match the index entry's
"dri" exactly.

This matters because Dokka can group multiple declarations (e.g. several overloads
of the same function name for different receiver types) onto a single PageNode /
output page. JsonRenderer only serializes documentables.first() for such a page, so
every other declaration that shares that page is listed in the package index but
has no actual documented content of its own.
"""
import json
import os
import sys
import argparse


def resolve_target_path(package_dir, url):
    """Resolve an index entry's url to the local .json file it should point to."""
    if not url:
        return None
    if url.startswith("http://") or url.startswith("https://"):
        return None  # external link, not a local file
    if url.startswith("unresolved:") or url == "#":
        return None

    path = url
    if path.endswith(".html"):
        path = path[:-5] + ".json"
    elif not path.endswith(".json"):
        path = path + ".json"

    return os.path.normpath(os.path.join(package_dir, path))


def main():
    parser = argparse.ArgumentParser(
        description="Verify every object in a kdoc-to-json package index.json has a corresponding documented page."
    )
    parser.add_argument("index_json", help="Path to a package's index.json, or the package directory containing one")
    args = parser.parse_args()

    index_path = args.index_json
    if os.path.isdir(index_path):
        index_path = os.path.join(index_path, "index.json")

    if not os.path.isfile(index_path):
        print(f"Error: '{index_path}' not found.", file=sys.stderr)
        sys.exit(2)

    package_dir = os.path.dirname(os.path.abspath(index_path))

    with open(index_path, "r", encoding="utf-8") as f:
        index_data = json.load(f)

    member_sections = ["functions", "properties", "classlikes", "typeAliases"]
    entries = []
    for section in member_sections:
        for entry in index_data.get(section) or []:
            entries.append((section, entry))

    print(f"Package: {index_data.get('name', '?')}")
    print(f"Index file: {index_path}")
    print(f"Total member entries listed: {len(entries)}")
    print()

    file_cache = {}
    missing = []

    for section, entry in entries:
        name = entry.get("name", "?")
        dri = entry.get("dri")
        url = entry.get("url")

        target_path = resolve_target_path(package_dir, url)
        if target_path is None:
            missing.append((section, name, dri, url, "unresolvable URL"))
            continue

        if target_path not in file_cache:
            if not os.path.isfile(target_path):
                file_cache[target_path] = None
            else:
                try:
                    with open(target_path, "r", encoding="utf-8") as f:
                        file_cache[target_path] = json.load(f)
                except Exception:
                    file_cache[target_path] = None

        target_data = file_cache[target_path]
        if target_data is None:
            missing.append((section, name, dri, url, "target file missing or unreadable"))
        elif target_data.get("dri") != dri:
            missing.append((section, name, dri, url, "target file exists but does not document this DRI"))

    print(f"Checked {len(entries)} entries against {len(file_cache)} unique target files.")
    print()

    if missing:
        print(f"FAILED: {len(missing)} object(s) in the index have no corresponding documented element:")
        for section, name, dri, url, reason in missing:
            print(f"  [{section}] {name}  (dri={dri}, url={url}) -- {reason}")
    else:
        print("SUCCESS: every object in the index corresponds to a documented element.")

    sys.exit(1 if missing else 0)


if __name__ == "__main__":
    main()
