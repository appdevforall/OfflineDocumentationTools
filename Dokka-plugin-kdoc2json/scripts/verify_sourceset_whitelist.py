#!/usr/bin/env python3
"""
Verifies that every output JSON file in a kdoc-to-json rendered documentation folder
complies with a given sourceSet whitelist: any file whose top-level "sourceSets" list
does not intersect the whitelist is reported as a violation. Such a file should have
been omitted by the JsonOutputPlugin's `sourceSetWhitelist` config option, so finding
one here means the plugin was run with a different (or no) whitelist than expected.

Files with an empty or missing top-level "sourceSets" (e.g. all-types.json, the
multimodule root index.json, or a page whose "sourceSets" field was itself stripped by
`omitFields`) are synthetic/aggregate outputs that aren't tied to a single source set,
and are skipped rather than flagged.
"""
import json
import os
import sys
import argparse


def find_json_files(root_dir):
    for dirpath, _, filenames in os.walk(root_dir):
        for filename in filenames:
            if filename.endswith(".json"):
                yield os.path.join(dirpath, filename)


def check_file(path, whitelist):
    """Returns (status, sourceSets, reason). status is one of 'ok', 'violation', 'skipped', 'unreadable'."""
    try:
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
    except (json.JSONDecodeError, OSError) as e:
        return "unreadable", None, str(e)

    if not isinstance(data, dict):
        return "skipped", None, "root is not a JSON object"

    source_sets = data.get("sourceSets")
    if not source_sets:
        return "skipped", source_sets, "no top-level sourceSets field (synthetic/aggregate output)"

    if any(ss in whitelist for ss in source_sets):
        return "ok", source_sets, None

    return "violation", source_sets, None


def write_log(output_file, lines):
    if not output_file:
        return
    try:
        with open(output_file, "w", encoding="utf-8") as f:
            for line in lines:
                f.write(line + "\n")
        print(f"📄 Results logged to: {output_file}")
    except OSError as e:
        print(f"⚠️  Failed to write to output file {output_file}: {e}")


def main():
    parser = argparse.ArgumentParser(
        description="Verify every JSON file in a kdoc-to-json output directory complies with a sourceSet whitelist."
    )
    parser.add_argument("docs_dir", help="Path to the rendered JSON documentation directory to scan recursively")
    parser.add_argument(
        "whitelist",
        nargs="+",
        help="One or more allowed source set names, matching the values that appear in the output "
             "\"sourceSets\" field (e.g. jvm js). Comma-separated values in a single argument are also "
             "accepted (e.g. 'jvm,js').",
    )
    parser.add_argument("--output-file", "-o", help="Path to a file where violations will be logged.", default=None)
    parser.add_argument("--verbose", "-v", action="store_true", help="Print the result for every file checked, not just violations.")
    args = parser.parse_args()

    if not os.path.isdir(args.docs_dir):
        print(f"Error: '{args.docs_dir}' is not a directory.", file=sys.stderr)
        sys.exit(2)

    whitelist = set()
    for entry in args.whitelist:
        whitelist.update(part.strip() for part in entry.split(",") if part.strip())

    if not whitelist:
        print("Error: whitelist must contain at least one source set name.", file=sys.stderr)
        sys.exit(2)

    print(f"Scanning: {args.docs_dir}")
    print(f"Whitelist: {sorted(whitelist)}")
    print()

    violations = []
    unreadable = []
    checked = 0
    skipped = 0

    for path in sorted(find_json_files(args.docs_dir)):
        rel_path = os.path.relpath(path, args.docs_dir)
        status, source_sets, reason = check_file(path, whitelist)

        if status == "unreadable":
            unreadable.append((rel_path, reason))
            print(f"  [UNREADABLE] {rel_path} -- {reason}")
        elif status == "skipped":
            skipped += 1
            if args.verbose:
                print(f"  [SKIPPED] {rel_path} -- {reason}")
        else:
            checked += 1
            if status == "violation":
                violations.append((rel_path, source_sets))
                print(f"  [VIOLATION] {rel_path} -- sourceSets={source_sets} not in whitelist")
            elif args.verbose:
                print(f"  [OK] {rel_path} -- sourceSets={source_sets}")

    print()
    print(f"Checked {checked} file(s) with a sourceSets field ({skipped} skipped, {len(unreadable)} unreadable).")

    write_log(
        args.output_file,
        [f"{rel_path}\tsourceSets={source_sets}" for rel_path, source_sets in violations]
        + [f"{rel_path}\tUNREADABLE: {reason}" for rel_path, reason in unreadable],
    )

    if violations:
        print(f"❌ FAILED: {len(violations)} file(s) violate the sourceSet whitelist.")
    if unreadable:
        print(f"⚠️  {len(unreadable)} file(s) could not be read/parsed as JSON.")
    if not violations and not unreadable:
        print("✅ SUCCESS: every file with a sourceSets field complies with the whitelist.")

    sys.exit(1 if (violations or unreadable) else 0)


if __name__ == "__main__":
    main()
