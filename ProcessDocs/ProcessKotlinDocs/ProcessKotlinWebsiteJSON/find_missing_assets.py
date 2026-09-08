#!/usr/bin/env python3
"""
Documents every reference in the docs source (kotlin-web-site/docs) that
points at an asset - another topic page, an image, or an <include> target -
which doesn't actually exist, so broken source content can be found and
fixed without having to read every generated page.

Usage:
    python3 find_missing_assets.py <docs-root> [report-path] [--topics-subdir topics]
        [--images-subdir images] [--allow-failures]

Reuses md_to_json.py's own link/image resolution (build_topic_index,
build_image_index, Converter) instead of re-parsing links with regexes, so
this reports exactly what would end up broken in the rendered site - e.g. a
"foo.md" written inside a fenced code sample (showing readers what Writerside
markup looks like) is correctly ignored, since it's never tokenized as a
real link in the first place.

<include from="..." element-id="...">  targets are a separate check: those
aren't links/images so md_to_json.py's Converter never resolves them, but a
missing include is still a broken asset reference worth surfacing. This is
checked with a small standalone regex scan (fenced code blocks stripped
first) against every filename that exists anywhere under <topics-subdir>/,
regardless of extension (include targets can be ".md" or ".topic").
"""
import argparse
import re
import sys
from collections import defaultdict
from pathlib import Path

from md_to_json import (
    Converter,
    FENCE_LINE_RE,
    build_image_index,
    build_topic_index,
    fenced_spans,
    load_variables,
    make_markdown_it,
)

INCLUDE_RE = re.compile(r'<include\b[^>]*\bfrom\s*=\s*"([^"]+)"')


def is_unterminated(span_text: str) -> bool:
    """True when a fenced span never got its closing fence.

    fenced_spans runs such a span to the end of the document, which is what
    CommonMark does - but for a scan whose whole job is finding missing
    references, it means one stray ``` line turns the check off for
    everything after it. Counting fence lines inside the span separates the
    two: a closed block has an opener and a closer, an unclosed one has only
    the opener. FENCE_LINE_RE is fenced_spans' own regex, so this cannot
    drift from the rule that produced the span.

    Deliberately one-sided. A span holding a fence line that did not close it
    - a ``` sample inside a ~~~ block, say - counts two and stays quiet even
    if it really is unterminated, so this under-warns rather than crying wolf
    on the common case of a file that simply ends with a closed code block.
    It gates a warning, not the stripping, so a miss costs a message."""
    return sum(1 for line in span_text.splitlines() if FENCE_LINE_RE.match(line.lstrip())) < 2


def outside_fences(text: str, source: str = None) -> str:
    """`text` with every fenced code block removed, so the <include> scan below
    doesn't report a sample as a broken reference.

    md_to_json.fenced_spans is the shared fence rule, rather than a second
    pattern here. The one this replaced was `re.compile("```.*?```", re.S)`,
    which saw only backtick fences: a `<include from="...">` shown inside a
    ~~~-fenced sample was scanned as if it were real, and warned about a file
    that was never meant to exist. It also paired fences by "next three
    backticks", so a longer ```` fence wrapping a ``` sample closed early and
    exposed the rest of the block. fenced_spans handles both, and is already
    what extract_title trusts to stay out of code samples.

    `source` names the file in the warning an unterminated fence earns. That
    warning is the point: the old regex needed a *closing* fence to match
    anything, so an unpaired one left the rest of the file scannable, where
    this correctly treats it as one long code block and stops checking. That
    is a false negative in a report whose value is catching what's missing,
    so it has to be said out loud rather than inferred from a short report."""
    spans = fenced_spans(text)
    if not spans:
        return text
    parts = []
    pos = 0
    for start, end in spans:
        if source and is_unterminated(text[start:end]):
            print(f"warning: {source} has an unterminated code fence; everything after it is being read as "
                  "code, so any <include> below it is not being checked", file=sys.stderr)
        parts.append(text[pos:start])
        pos = end
    parts.append(text[pos:])
    return "".join(parts)


def find_include_warnings(topics_dir: Path) -> tuple:
    """Returns (warnings, failed_count). A per-file read/scan failure here
    used to raise uncaught, killing the whole process (and bypassing
    --allow-failures) even when the main conversion loop's own try/except
    would have just counted it as one more scan failure - same shape as the
    bug that loop was fixed for, just in this separate scan."""
    all_filenames = {p.name for p in topics_dir.rglob("*") if p.is_file()}
    warnings = []
    failed = 0
    for md_path in sorted(topics_dir.rglob("*.md")):
        source_rel = str(md_path.relative_to(topics_dir.parent))
        try:
            text_no_fences = outside_fences(md_path.read_text(encoding="utf-8"), source_rel)
        except Exception as exc:  # noqa: BLE001 - surface which file broke, keep scanning the rest
            print(f"error scanning {md_path} for <include> targets: {exc}", file=sys.stderr)
            failed += 1
            continue
        for target in INCLUDE_RE.findall(text_no_fences):
            if target not in all_filenames:
                warnings.append({"kind": "include", "source": source_rel, "reference": target})
    return warnings, failed


def group_by_reference(warnings: list) -> dict:
    grouped = defaultdict(set)
    for w in warnings:
        grouped[w["reference"]].add(w["source"])
    return {ref: sorted(sources) for ref, sources in sorted(grouped.items())}


def render_section(title: str, grouped: dict) -> str:
    lines = [f"## {title} ({len(grouped)})", ""]
    if not grouped:
        lines.append("None.")
    for ref, sources in grouped.items():
        lines.append(f"- `{ref}`")
        for src in sources:
            lines.append(f"  - {src}")
    lines.append("")
    return "\n".join(lines)


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("docs_root", type=Path, help="Path to kotlin-web-site/docs")
    parser.add_argument("report_path", type=Path, nargs="?", default=Path("missing-assets-report.md"),
                         help="Where to write the Markdown report (default: ./missing-assets-report.md)")
    parser.add_argument("--topics-subdir", default="topics", help="Subdirectory of docs_root holding .md files")
    parser.add_argument("--images-subdir", default="images", help="Subdirectory of docs_root holding image files")
    parser.add_argument("--allow-failures", action="store_true",
                         help="Exit 0 even if some files failed to scan (default: exit 1 if any did, so this "
                              "pre-flight gate can't report a clean run over a corpus it couldn't actually read)")
    args = parser.parse_args()

    docs_root: Path = args.docs_root
    topics_dir = docs_root / args.topics_subdir
    images_dir = docs_root / args.images_subdir
    if not topics_dir.is_dir():
        print(f"error: {topics_dir} is not a directory", file=sys.stderr)
        sys.exit(1)

    variables = load_variables(docs_root)
    topic_index = build_topic_index(topics_dir)
    image_index, image_collisions = build_image_index(images_dir)
    md = make_markdown_it()
    converter = Converter(md, variables, topic_index, image_index)

    md_files = sorted(topics_dir.rglob("*.md"))
    failed = 0
    for md_path in md_files:
        rel = md_path.relative_to(topics_dir)
        page_id = str(rel.with_suffix(""))
        source_rel = str(Path(args.topics_subdir) / rel)
        try:
            converter.convert_file(md_path, page_id, source_rel)
        except Exception as exc:  # noqa: BLE001 - surface which file broke, keep auditing the rest
            print(f"error scanning {md_path}: {exc}", file=sys.stderr)
            failed += 1

    link_warnings = [w for w in converter.warnings if w["kind"] == "link"]
    image_warnings = [w for w in converter.warnings if w["kind"] == "image"]
    include_warnings, include_scan_failed = find_include_warnings(topics_dir)
    failed += include_scan_failed

    links = group_by_reference(link_warnings)
    images = group_by_reference(image_warnings)
    includes = group_by_reference(include_warnings)
    collisions = {name: rels for name, rels in image_collisions}

    report = [
        "# Missing Assets Report",
        "",
        f"Scanned {len(md_files)} Markdown files under `{topics_dir}`.",
        "",
        "## Summary",
        "",
        f"- {len(links)} unresolved cross-page link target(s)",
        f"- {len(images)} missing image(s)",
        f"- {len(collisions)} ambiguous image filename(s) (exist in more than one place under images/)",
        f"- {len(includes)} unresolved `<include>` target(s)",
        f"- {failed} file(s) failed to scan" + (" - **this report is incomplete**" if failed else ""),
        "",
        render_section("Unresolved cross-page links", links),
        render_section("Missing images", images),
        render_section("Unresolved <include> targets", includes),
        f"## Ambiguous image filenames ({len(collisions)})",
        "",
    ]
    if not collisions:
        report.append("None.")
    for name, rels in collisions.items():
        report.append(f"- `{name}`")
        report.append(f"  - used: images/{rels[0]}")
        for rel in rels[1:]:
            report.append(f"  - ignored: images/{rel}")
    report.append("")

    args.report_path.write_text("\n".join(report), encoding="utf-8")
    print(f"Wrote {args.report_path} "
          f"({len(links)} links, {len(images)} images, {len(collisions)} ambiguous, {len(includes)} includes)")
    if failed and not args.allow_failures:
        print(f"error: {failed} file(s) failed to scan (pass --allow-failures to tolerate this)", file=sys.stderr)
        sys.exit(1)


if __name__ == "__main__":
    main()
