#!/usr/bin/env python3
"""
Builds sidebar navigation data/HTML from a JetBrains Writerside .tree file
(e.g. kotlin-web-site/docs/kr.tree), for use with templates/nav.peb.

Usage:
    python3 build_nav.py <docs-root> <docs-json-dir> <output-dir> [--tree-file kr.tree]

<docs-root>      is the checkout of kotlin-web-site/docs (contains kr.tree).
<docs-json-dir>  is the output of md_to_json.py, used to resolve each
                  <toc-element topic="foo.md"/> to the page's "id" (its path
                  relative to docs-root, e.g. "topics/ksp/ksp-overview") and,
                  when a <toc-element> has no toc-title, its rendered "title".
<output-dir>     receives nav.json (the tree, for feeding into nav.peb) and
                  nav.html (a pre-rendered static copy of what nav.peb outputs).

Writerside lets a <toc-element> both link to its own page (topic="...") and
contain nested <toc-element> children (sub-pages) at once, and topic
references are bare filenames resolved by uniqueness across the whole
topics/ subtree, not by path - this script relies on that same uniqueness.

A topic="foo.md" that doesn't resolve to any converted page is treated as
manually deleted (rather than e.g. a typo) and dropped from the nav: if the
toc-element has no children either, build_node() returns None for it and it's
filtered out of its parent's children entirely; if it still has children
(sub-topics that do still exist), the element is kept as a link-less category
header for them instead of losing those children too. This only applies to
topic="*.md" references - kr.tree itself is never modified, so a purged
topic's <toc-element> (and any of its still-live children) stays in the tree
forever; this is what re-derives "not actually present" from the docs-json
output on every run instead.

If <docs-json-dir>/theme.json (written by md_to_json.py from its own config
argument) has a "menu-no-link-color", each node that doesn't actually lead to
a generated page - a pure category header with no topic="...", a
topic="foo.topic" that isn't a converted .md page (e.g. home.topic,
api-references.topic), or a topic="foo.md" whose page was deleted but which
still has live children - gets a "noLinkColor" of that value baked directly
into its entry in nav.json (null otherwise), which nav.peb turns into an
inline style. A toc-element using kr.tree's other, unrelated href="https://..."
attribute (e.g. "Test library (kotlin.test)" under API reference) is not
treated any differently here - it has no topic="...", so it's already a
no-link category header like any other; this script does not read that
external href at all, so no link is added for it.
"""
import argparse
import html
import json
import re
import sys
import xml.etree.ElementTree as ET
from pathlib import Path

HUMANIZE_RE = re.compile(r"[-_]+")


def humanize(stem: str) -> str:
    return HUMANIZE_RE.sub(" ", stem).strip().title()


def load_page_index(docs_json_dir: Path) -> tuple[dict, dict]:
    """Returns (stem -> id, id -> title) built from every generated page JSON."""
    stem_to_id = {}
    id_to_title = {}
    # sorted(), and first-wins below, so a stem that exists in more than one
    # place resolves the same way on every machine. An unsorted rglob left it to
    # filesystem order, which made nav output non-reproducible - and disagreed
    # with md_to_json.build_topic_index, which is first-sorted-wins with a
    # warning and is what the pages themselves were converted against.
    for json_path in sorted(docs_json_dir.rglob("*.json")):
        page = json.loads(json_path.read_text(encoding="utf-8"))
        # Not every *.json under here is a page. main() writes nav.json - a
        # top-level array - into output_dir, and the documented invocation
        # passes output_dir as the scan directory too, so a second run reads
        # its own previous output and used to die on `list.get`. Anything that
        # isn't a page object is simply not a page.
        if not isinstance(page, dict):
            continue
        page_id = page.get("id")
        if not page_id:
            continue
        stem = Path(page_id).name
        if stem in stem_to_id:
            print(f"warning: ambiguous page stem {stem!r}: keeping {stem_to_id[stem]}, "
                  f"ignoring {page_id}", file=sys.stderr)
            continue
        stem_to_id[stem] = page_id
        id_to_title[page_id] = page.get("title")
    return stem_to_id, id_to_title


def build_node(el: ET.Element, stem_to_id: dict, id_to_title: dict, warnings: list, no_link_color: str,
               id_prefix: str = "") -> dict | None:
    """Returns None when this element's own topic="*.md" no longer resolves
    to a converted page (deleted) and it has no children left worth keeping -
    callers must filter these out of whatever list they collect build_node()
    results into (both build_node() itself, for its own children, and the
    top-level toc-element loop in main())."""
    topic = el.get("topic")
    toc_title = el.get("toc-title")
    hidden = el.get("hidden") == "true"

    children = [
        build_node(c, stem_to_id, id_to_title, warnings, no_link_color, id_prefix)
        for c in el.findall("toc-element")
    ]
    children = [c for c in children if c is not None]

    page_id = None
    title = toc_title
    no_link = True
    deleted = False
    if topic:
        stem = Path(topic).stem
        page_id = stem_to_id.get(stem)
        if page_id is None:
            if topic.endswith(".md"):
                # Was a real page at some point, isn't anymore - the .md was
                # manually deleted. Drop this node; if children still resolve
                # to real pages, keep it around as a no-link category header
                # for them rather than losing those children too.
                deleted = True
                if children:
                    warnings.append(f"topic={topic!r} has no converted page (deleted); "
                                     f"keeping as a header for its {len(children)} remaining child page(s)")
                else:
                    warnings.append(f"topic={topic!r} has no converted page (deleted); dropping from nav")
                    return None
            else:
                # Not a converted .md page (e.g. home.topic, api-references.topic):
                # still rendered as a link (for consistency, and it's still the
                # best URL guess) but it doesn't lead anywhere real, so it's
                # colored the same as a no-topic-at-all category header. Prefixed
                # the same way as a resolved stem_to_id hit would be (e.g.
                # populate_db.py passes id_prefix="k/html/" since stem_to_id
                # there already maps every real page to "k/html/<stem>"), so
                # this fallback id follows the same URL convention as everything
                # else on the page rather than silently reverting to a bare one.
                page_id = f"{id_prefix}{stem}"
                warnings.append(f"no converted page for topic={topic!r}; using id={page_id!r}")
        else:
            no_link = False
        if not title:
            title = (id_to_title.get(page_id) if page_id else None) or humanize(stem)
    elif not title:
        title = "Untitled"

    return {
        "title": title,
        "id": None if deleted else page_id,
        "hidden": hidden,
        "noLinkColor": no_link_color if (no_link or deleted) else None,
        "children": children,
    }


def render_node(node: dict, indent: int = 0) -> str:
    # Kept byte-identical to templates/nav.peb's renderNavNode macro, since
    # RenderDocs.java re-renders nav.peb over nav.json for the live site and
    # this function only produces a standalone preview copy of the same HTML.
    #
    # That equivalence is why every interpolation is escaped: Pebble autoescapes
    # by default, so nav.peb's output already is. Interpolating raw here meant a
    # toc-title containing a double quote (or "&", or "<") closed the aria-label
    # attribute early and produced different - and malformed - HTML than the
    # template it claims to match.
    title = html.escape(node["title"] or "", quote=True)
    node_id = html.escape(node["id"] or "", quote=True)
    classes = "nav-item" + (" nav-hidden" if node["hidden"] else "")
    has_children = bool(node["children"])
    style = f' style="color: {html.escape(str(node["noLinkColor"]), quote=True)};"' if node["noLinkColor"] else ""
    if node["id"]:
        label = f'<a class="nav-link"{style} href="/{node_id}.html" data-nav-id="{node_id}">{title}</a>'
    else:
        label = f'<span class="nav-group-title"{style}>{title}</span>'
    toggle = (
        f'<button type="button" class="nav-toggle-group" aria-expanded="false" '
        f'aria-label="Toggle {title} section"></button>'
        if has_children else '<span class="nav-spacer" aria-hidden="true"></span>'
    )
    parts = [f'<li class="{classes}">', '<div class="nav-row">', toggle, label, "</div>"]
    if has_children:
        parts.append('<ul class="nav-tree nav-subtree">')
        for child in node["children"]:
            parts.append(render_node(child))
        parts.append("</ul>")
    parts.append("</li>")
    return "".join(parts)


def render_html(tree: list) -> str:
    body = "".join(render_node(node) for node in tree)
    return (
        '<nav class="docs-nav" aria-label="Documentation">'
        f'<ul class="nav-tree">{body}</ul>'
        "</nav>"
    )


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("docs_root", type=Path, help="Path to kotlin-web-site/docs")
    parser.add_argument("docs_json_dir", type=Path, help="Path to md_to_json.py output directory")
    parser.add_argument("output_dir", type=Path, help="Directory to write nav.json and nav.html into")
    parser.add_argument("--tree-file", default="kr.tree", help="Filename of the Writerside tree file under docs_root")
    args = parser.parse_args()

    tree_path = args.docs_root / args.tree_file
    if not tree_path.is_file():
        print(f"error: {tree_path} does not exist", file=sys.stderr)
        sys.exit(1)

    stem_to_id, id_to_title = load_page_index(args.docs_json_dir)

    theme_path = args.docs_json_dir / "theme.json"
    no_link_color = None
    if theme_path.is_file():
        no_link_color = json.loads(theme_path.read_text(encoding="utf-8")).get("menu-no-link-color")
    else:
        print(f"warning: {theme_path} not found (run md_to_json.py first); "
              f"no-link menu items won't be colored", file=sys.stderr)

    root = ET.parse(tree_path).getroot()
    warnings = []
    nav_tree = [build_node(el, stem_to_id, id_to_title, warnings, no_link_color) for el in root.findall("toc-element")]
    nav_tree = [node for node in nav_tree if node is not None]

    for w in warnings:
        print(f"warning: {w}", file=sys.stderr)

    args.output_dir.mkdir(parents=True, exist_ok=True)
    (args.output_dir / "nav.json").write_text(
        json.dumps(nav_tree, separators=(",", ":"), ensure_ascii=False), encoding="utf-8"
    )
    (args.output_dir / "nav.html").write_text(render_html(nav_tree), encoding="utf-8")

    print(f"Wrote nav.json and nav.html ({len(warnings)} warning(s)) into {args.output_dir}")


if __name__ == "__main__":
    main()
