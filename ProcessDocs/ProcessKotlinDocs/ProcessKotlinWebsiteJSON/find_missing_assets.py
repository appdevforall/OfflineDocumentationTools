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

from md_to_json import Converter, build_image_index, build_topic_index, load_variables, make_markdown_it

INCLUDE_RE = re.compile(r'<include\b[^>]*\bfrom\s*=\s*"([^"]+)"')
FENCE_RE = re.compile(r"```.*?```", re.S)


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
            text_no_fences = FENCE_RE.sub("", md_path.read_text(encoding="utf-8"))
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
