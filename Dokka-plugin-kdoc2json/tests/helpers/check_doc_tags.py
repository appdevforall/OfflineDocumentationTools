#!/usr/bin/env python3
"""Checks for TEST_PLAN.md §4 (Documentation tag & text extraction:
mapDocNodes, extractText).

Run via tests/test_doc_tags.sh, which regenerates examples/example-data-processor
first. Takes the rendered JSON output directory as argv[1].

Not covered here: §4's last row ("Per-source-set documentation") needs an
expect/actual pair across two source sets, which TEST_PLAN.md §1 deferred
(would require converting example-data-processor to Kotlin Multiplatform).
"""
import os
import sys

sys.path.insert(0, os.path.dirname(__file__))
from checklib import Checker, load


def main():
    output_dir = sys.argv[1]
    c = Checker()

    safe_divide = load(os.path.join(output_dir, "com.example.testlib", "safe-divide.json"))
    tags = safe_divide["documentation"][":/main"]
    description = next(t["text"] for t in tags if t["type"] == "Description")

    # --- HTML escaping ---
    c.check("&lt; 2" in description, "raw '<' in KDoc prose is escaped to &lt;")
    c.check("A &amp; B" in description, "raw '&' in KDoc prose is escaped to &amp;")
    c.check("1 < 2" not in description, "the raw unescaped '<' sequence does not leak into the output")

    # --- Nested inline markup ---
    c.check("<b>bold</b>" in description, "**bold** maps to <b>bold</b>")
    c.check("<i>italic</i>" in description, "*italic* maps to <i>italic</i>")
    c.check("<code>1 &lt; 2</code>" in description, "inline `code` maps to <code>, escaped and un-flattened")

    # --- Block-level tags ---
    c.check(
        "<pre><code>val result = safeDivide(10, 2)</code></pre>" in description,
        "fenced code block round-trips to <pre><code>",
    )
    c.check("<blockquote>" in description, "blockquote markup round-trips to <blockquote>")
    c.check("<ul><li>" in description, "bulleted list round-trips to <ul><li>")

    # --- @see / @throws / custom named tags ---
    throws = next(t for t in tags if t["type"] == "Throws")
    c.check(throws.get("name") == "ArithmeticException", "@throws captures the exception name via TagWrapperDto.name")
    c.check(
        "href=" in throws.get("text", ""),
        "a [Foo]-style link inside an @throws body resolves to a real url via resolveUrl",
    )

    see = next(t for t in tags if t["type"] == "See")
    c.check(see.get("name") == "CustomException", "@see captures the referenced name via TagWrapperDto.name")
    # NOTE: `@see [CustomException]` does not currently produce a resolved link/url anywhere on
    # this tag. Unlike @throws (whose link comes from an inline [Foo] reference inside its text),
    # Dokka's `See` DocTag carries its resolved target on a dedicated `address: DRI?` field, and
    # TagWrapperDto has no `url` property for mapDocNodes to put it in. Not asserted as a bug here
    # since fixing it needs a DTO/schema change (new TagWrapperDto.url field), not a test.

    # --- @sample ---
    samples = [t for t in tags if t["type"] == "Sample"]
    c.check(len(samples) == 2, "both @sample tags on safeDivide are present", f"got {len(samples)}")
    sample_by_name = {s["name"]: s["text"] for s in samples}
    c.check(
        sample_by_name.get("com.example.testlib.safeDivideSample") == "val result = safeDivide(10, 2)\nprintln(result)",
        "first @sample pulls safeDivideSample's own source",
        f"got {sample_by_name.get('com.example.testlib.safeDivideSample')!r}",
    )
    c.check(
        sample_by_name.get("com.example.testlib.safeDivideSampleAlternate") == "val result = safeDivide(7, 3)\nprintln(result)",
        "second @sample pulls safeDivideSampleAlternate's own source (not a repeat of the first)",
        f"got {sample_by_name.get('com.example.testlib.safeDivideSampleAlternate')!r}",
    )

    return c.summarize(os.path.basename(__file__))


if __name__ == "__main__":
    sys.exit(main())
