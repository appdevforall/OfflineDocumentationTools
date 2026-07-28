#!/usr/bin/env python3
"""Checks for TEST_PLAN.md §7 (LinkPostProcessor cross-module resolution).

Run via tests/test_link_postprocessor.sh. Takes the rendered JSON output
directory and the path to that run's captured Gradle log as args.

Not covered here (need a real multi-module or multiplatform build, deferred to
TEST_PLAN.md §8's kotlin-stdlib stress test per the user's decision when step 3
started):
- "Relative path depth" for a genuinely cross-module-resolved DRI: verified
  empirically that in this single-module fixture, LinkPostProcessor's pass-2
  replace step resolves exactly 0 links ("Successfully resolved 0 cross-module
  links!" in the build log) -- every "unresolved:" marker here is either
  resolved directly by locationProvider at write time, or is a synthetic
  accessor DRI with no page at all, so it's patched straight to "#".
- "Last-writer-wins" for expect/actual: needs two files where the same DRI
  legitimately resolves in both.
"""
import os
import re
import sys

sys.path.insert(0, os.path.dirname(__file__))
from checklib import Checker, load


def main():
    output_dir = sys.argv[1]
    gradle_log_path = sys.argv[2]
    c = Checker()

    # --- Two-pass index + replace: no lingering "unresolved:" markers ---
    offenders = []
    for dirpath, _, files in os.walk(output_dir):
        for f in files:
            if f.endswith(".json"):
                full = os.path.join(dirpath, f)
                with open(full, "r", encoding="utf-8", errors="replace") as fh:
                    if "unresolved:" in fh.read():
                        offenders.append(os.path.relpath(full, output_dir))
    c.check(
        not offenders,
        "no .json file on disk contains the literal string 'unresolved:'",
        f"found in: {offenders[:5]}",
    )

    # --- Genuinely unresolvable DRI: patched to "#", and named in the log ---
    description = load(os.path.join(output_dir, "com.example.testlib", "-meta", "description.json"))
    getter = description.get("getter") or {}
    c.check(
        getter.get("url") == "#" and "getDescription" in getter.get("dri", ""),
        'a synthetic accessor DRI with no page of its own (Meta.getDescription) is patched to "#"',
        f"got getter={getter}",
    )

    with open(gradle_log_path, "r", encoding="utf-8", errors="replace") as f:
        log_text = f.read()
    match = re.search(r'Failed to resolve (\d+) DRIs \(patched to "#"\):([^\n]*)', log_text)
    found = c.check(match is not None, "the build log contains a 'Failed to resolve N DRIs' warning")
    if found:
        count = int(match.group(1))
        listed = match.group(2)
        c.check(count > 0, "the warning reports a positive count of unresolved DRIs", f"got {count}")
        c.check(
            "com.example.testlib/Meta/getDescription" in listed,
            "the warning names the specific DRI that ended up patched to \"#\"",
        )

    return c.summarize(os.path.basename(__file__))


if __name__ == "__main__":
    sys.exit(main())
