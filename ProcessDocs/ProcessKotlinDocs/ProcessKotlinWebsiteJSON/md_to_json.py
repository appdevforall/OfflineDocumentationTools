#!/usr/bin/env python3
"""
Converts JetBrains Writerside-flavored Markdown (as used by kotlin-web-site/docs)
into a simple JSON block schema suitable for a templating engine.

Usage:
    python3 md_to_json.py <docs-root> <output-dir> <config> [--topics-subdir topics]
        [--images-subdir images] [--allow-failures]

<docs-root> is the checkout of kotlin-web-site/docs (contains v.list, topics/, ...).
One JSON file is written per input .md file, mirroring its relative path under
<topics-subdir>.

<config> is a JSON file with:
  {"broken-ext-link-color": "#cc0000", "menu-no-link-color": "#999999"}
"broken-ext-link-color" colors <a> tags in the rendered content that are
either off-site (any http(s)/mailto: link) or a same-tree ".md" reference
that doesn't resolve to a real page. "menu-no-link-color" isn't used here -
it's carried through to <output-dir>/theme.json for build_nav.py (which
builds the sidebar from kr.tree, a separate input this script doesn't read)
to pick up.

Output schema (one object per page):
{
  "id": "enum-classes",
  "sourceFile": "topics/enum-classes.md",
  "title": "Enum classes",
  "blocks": [ <block>, ... ]
}

Block shapes:
  {"type": "heading", "level": 2, "id": "anonymous-classes", "html": "..."}
    - "attrs" is present only if the heading's source line had a trailing
      `{...}` attribute group (e.g. `## Title {id="custom-anchor"}`); an
      explicit "id" in it overrides the auto-generated slug.
  {"type": "paragraph", "html": "..."}
  {"type": "code", "lang": "kotlin", "code": "...", "attrs": {"kotlin-runnable": "true"}}
  {"type": "blockquote", "attrs": {"style": "note"}, "blocks": [...]}
  {"type": "list", "ordered": false, "items": [{"blocks": [...]}]}
  {"type": "table", "headers": ["a", "b"], "rows": [["1", "2"]]}
  {"type": "hr"}
  {"type": "tabs", "attrs": {"group": "build-system"},
   "tabs": [{"title": "Gradle", "attrs": {"group-key": "gradle"}, "blocks": [...]}]}
  {"type": "html", "html": "<raw passthrough for anything unrecognized>"}

There is no standalone "image" block type - CommonMark only ever produces
"image" as an inline token nested inside a paragraph/heading/etc., so a
`![alt](foo.png)` in the source always ends up as an <img> inside that
block's own "html" string (via render_inline), never as a top-level block
of its own.

Cross-page links (`[text](other-page.md#anchor)`) and images (`![alt](foo.png)`)
use Writerside's bare-filename convention - the referenced file is looked up
by name anywhere under <topics-subdir>/ (links) or images/ (images), the same
way kr.tree's topic="..." references are resolved. Rendered HTML rewrites
these to root-relative URLs that only resolve once this JSON has been passed
through templates/page.peb and rendered by RenderDocs: links become
"/<page-id>.html#anchor", and images become "/images/<path-within-images>".
The images/ directory itself is copied to <output-dir>/images/ so the
resolved paths have something to point at.

Known limitations (fine for a first pass, worth revisiting before production use):
  - <tabs>/<tab> grouping and attribute-line merging (the "{...}" line after a
    fence/blockquote) only happen at the top level of a page and inside list
    items/blockquotes/table cells one level deep; deeply nested tabs-in-tabs
    are not handled.
  - <note>/<tip>/<warning> admonitions are recognized as block-level tags. The
    inline, single-line form seen inside HTML tables (e.g. roadmap.md) is passed
    through as raw "html" blocks instead of being unpacked, since those pages
    are basically hand-written HTML tables rather than prose.
  - <include> elements are passed through as raw "html" blocks; resolving them
    to the referenced snippet is not implemented.
  - %variables% (defined in v.list) are substituted textually in rendered HTML
    and code, using simple %name% -> value replacement.
  - A handful of images/ filenames collide across subdirectories (leftover
    duplicates in the source tree); resolution keeps the first match in sorted
    order and prints a warning rather than guessing which one is "correct".
  - A heading's id is slugify()'d from its text unless the source overrides
    it with a trailing "{id=...}". A hand-written #anchor that was authored
    against Writerside's own anchor algorithm rather than either of those
    could still resolve to the right page but land on no anchor, if the two
    algorithms diverge on a case not yet seen in the corpus (inline code or
    punctuation in the heading, duplicate heading text) - not currently
    detected.
"""
import argparse
import json
import os
import re
import shutil
import sys
import xml.etree.ElementTree as ET
from pathlib import Path

from markdown_it import MarkdownIt
from markdown_it.token import Token

TITLE_RE = re.compile(r"^\[//\]:\s*#\s*\(title:\s*(.*?)\)\s*$", re.MULTILINE)
ATTR_LINE_RE = re.compile(r"^\{(.*)\}$")
TRAILING_ATTR_GROUPS_RE = re.compile(r"\s*((?:\{[^{}]*\})+)\s*$")

# No leading "^": matched via .match(content, pos) below, which already
# anchors at pos - unlike search(), match() never scans forward - but "^"
# itself always means the absolute start of the *string*, not of pos, so
# keeping it here would silently stop the pos-advancing loop after the
# first brace group on every second-and-later iteration.
IMAGE_ATTR_GROUP_RE = re.compile(r"\{([^{}]*)\}")
ATTR_PAIR_RE = re.compile(r'([\w-]+)\s*=\s*(?:"([^"]*)"|(\S+))')
VAR_RE = re.compile(r"%([\w.-]+)%")
MD_LINK_RE = re.compile(r"^([\w.-]+)\.md(#.*)?$")
EXTERNAL_HREF_RE = re.compile(r"^(?:[a-zA-Z][a-zA-Z0-9+.-]*:)?//|^mailto:", re.I)
LINK_TAG_RE = re.compile(r'<a\b[^>]*\bhref="([^"]*)"[^>]*>')
STYLE_ATTR_RE = re.compile(r'\bstyle="([^"]*)"')
IMG_TAG_RE = re.compile(r"<img\b[^>]*>", re.I)
IMG_SRC_RE = re.compile(r'src="([^"]*)"')
COLOR_RE = re.compile(r"^#[0-9a-fA-F]{3,8}$|^[a-zA-Z]+$")

# TAG_RE is built from this set (rather than hardcoding the same five names
# twice) so the two can't silently diverge. The (?![\w-]) after the
# alternation is load-bearing: without it, "tab" matches as a prefix of
# "table", consuming "<table>" as tag "tab" with attrs "le".
#
# Group 3 (attrs) is non-greedy and group 4 (self-closing "/") is anchored
# right before the final ">" - with a single greedy "([^>]*)/?>$" instead,
# the attrs group swallows a self-closing tag's trailing "/" before the
# optional "/?" ever gets a chance to match it, so "<tab .../>" came out
# indistinguishable from "<tab ...>": an opener with no matching closer,
# silently nesting the rest of the page inside it.
CONTAINER_TAGS = {"tabs", "tab", "note", "tip", "warning"}
TAG_RE = re.compile(r"^<(/?)(" + "|".join(CONTAINER_TAGS) + r")(?![\w-])([^>]*?)\s*(/?)>$", re.I)


def build_topic_index(topics_dir: Path) -> dict:
    """Bare filename stem (e.g. "enum-classes") -> page id (e.g. "kotlin-tour/enum-classes").

    Mirrors build_image_index's collision handling: a stem that exists more
    than once under topics_dir keeps its first (sorted) page id and prints a
    warning naming the others, rather than resolving to whichever sorts
    first with no diagnostic at all."""
    index = {}
    candidates = {}
    for md_path in sorted(topics_dir.rglob("*.md")):
        page_id = md_path.relative_to(topics_dir).with_suffix("").as_posix()
        candidates.setdefault(md_path.stem, []).append(page_id)
        index.setdefault(md_path.stem, page_id)
    for stem, ids in sorted(candidates.items()):
        if len(ids) > 1:
            print(f"warning: ambiguous topic filename {stem!r}: "
                  f"using {ids[0]}.md, ignoring {', '.join(i + '.md' for i in ids[1:])}", file=sys.stderr)
    return index


def build_image_index(images_dir: Path):
    """Bare filename (e.g. "mascot-main.png") -> path relative to images_dir.

    Returns (index, collisions), where collisions is a list of
    (filename, [candidate relative paths]) for filenames that exist in more
    than one place under images_dir - index keeps the first (sorted) one."""
    index = {}
    candidates = {}
    if not images_dir.is_dir():
        return index, []
    for img_path in sorted(images_dir.rglob("*")):
        if not img_path.is_file():
            continue
        rel = img_path.relative_to(images_dir).as_posix()
        candidates.setdefault(img_path.name, []).append(rel)
        index.setdefault(img_path.name, rel)
    collisions = [(name, rels) for name, rels in sorted(candidates.items()) if len(rels) > 1]
    for name, rels in collisions:
        print(f"warning: ambiguous image filename {name!r}: "
              f"using images/{rels[0]}, ignoring {', '.join('images/' + r for r in rels[1:])}", file=sys.stderr)
    return index, collisions


def load_variables(docs_root: Path) -> dict:
    v_list = docs_root / "v.list"
    if not v_list.exists():
        return {}
    tree = ET.parse(v_list)
    return {el.get("name"): el.get("value") for el in tree.getroot().findall("var")}


def substitute_vars(text: str, variables: dict) -> str:
    if not text:
        return text
    return VAR_RE.sub(lambda m: variables.get(m.group(1), m.group(0)), text)


def parse_attrs(attr_str: str) -> dict:
    """name="quoted value" or name=bare -> {"name": "quoted value"/"bare"},
    decided per pair. findall() coerces a non-participating group to "" (not
    None), and exactly one of quoted/bare participates per match, so the
    other is always "" - `quoted or bare` picks whichever one actually
    matched, including a genuinely empty quoted value ("" or "" -> "")."""
    attrs = {}
    for name, quoted, bare in ATTR_PAIR_RE.findall(attr_str or ""):
        attrs[name] = quoted or bare
    return attrs


def extract_title(raw_text: str):
    m = TITLE_RE.search(raw_text)
    if not m:
        return None, raw_text
    title = m.group(1).strip()
    remaining = raw_text[: m.start()] + raw_text[m.end():]
    return title, remaining


def slugify(text: str) -> str:
    slug = re.sub(r"[^\w\s-]", "", text.lower()).strip()
    return re.sub(r"[\s_]+", "-", slug)


class Node:
    """Generic open/close tree built from markdown-it's flat token stream."""

    __slots__ = ("token", "children")

    def __init__(self, token: Token):
        self.token = token
        self.children = []


def build_tree(tokens) -> list:
    root = []
    stack = [root]
    for tok in tokens:
        if tok.nesting == 1:
            node = Node(tok)
            stack[-1].append(node)
            stack.append(node.children)
        elif tok.nesting == -1:
            stack.pop()
        else:
            stack[-1].append(Node(tok))
    return root


class Converter:
    def __init__(self, md: MarkdownIt, variables: dict, topic_index: dict = None, image_index: dict = None,
                 broken_ext_link_color: str = None, image_url_prefix: str = "/images/"):
        self.md = md
        self.variables = variables
        self.topic_index = topic_index or {}
        self.image_index = image_index or {}
        self.broken_ext_link_color = broken_ext_link_color
        # Overridable so a different deployment target (e.g. populate_db.py's
        # database-backed site, which serves images from "/k/html/images/"
        # rather than a bare "/images/") can retarget every image src without
        # a separate rewrite pass - resolve_image_src just uses this prefix
        # directly.
        self.image_url_prefix = image_url_prefix
        self.current_source = None
        # Populated as a side effect of resolve_href/resolve_image_src failing
        # to resolve a reference; find_missing_assets.py reuses this same
        # resolution logic (rather than re-parsing links with regexes) by
        # running convert_file over every page and reading this list back.
        self.warnings = []
        # Reset per file in convert_file - two headings with the same text
        # on the same page would otherwise share a slug, so an anchor to the
        # second one lands on the first.
        self.seen_heading_ids = set()

    def unique_heading_id(self, text: str) -> str:
        """slugify(text), de-duped against every other heading id already
        seen on this page - resolve_href points at these ids verbatim (via
        the source's own #anchor), so two headings sharing a slug would
        make any link to the second one land on the first instead."""
        base = slug = slugify(text)
        n = 2
        while slug in self.seen_heading_ids:
            slug = f"{base}-{n}"
            n += 1
        self.seen_heading_ids.add(slug)
        return slug

    def resolve_href(self, href: str):
        """"other-page.md#anchor" -> "/<page-id>.html#anchor", or None to leave href untouched."""
        m = MD_LINK_RE.match(href or "")
        if not m:
            return None
        stem, anchor = m.groups()
        page_id = self.topic_index.get(stem)
        if page_id is None:
            print(f"warning: link to unknown topic {href!r}", file=sys.stderr)
            self.warnings.append({"kind": "link", "source": self.current_source, "reference": href})
            return None
        return f"/{page_id}.html{anchor or ''}"

    def resolve_image_src(self, src: str):
        """"foo.png" -> "<image_url_prefix><path-within-images>", or None to leave src untouched."""
        if not src or "://" in src or src.startswith("/") or "/" in src:
            return None
        rel = self.image_index.get(src)
        if rel is None:
            print(f"warning: image not found: {src!r}", file=sys.stderr)
            self.warnings.append({"kind": "image", "source": self.current_source, "reference": src})
            return None
        return f"{self.image_url_prefix}{rel}"

    def rewrite_urls(self, html: str) -> str:
        """Rewrites every href="...md" / src="foo.png" attribute found in a
        blob of rendered/raw HTML. Applied to markdown-rendered HTML *and* to
        Writerside's raw <a>/<img> passthrough HTML (which markdown-it never
        tokenizes as links/images at all, so token-level rewriting alone
        would miss it); resolve_href/resolve_image_src already leave anything
        that isn't a bare same-tree ".md"/image reference untouched, so this
        is safe to run unconditionally on any HTML string."""
        if not html:
            return html

        def href_repl(m):
            new_href = self.resolve_href(m.group(1))
            return f'href="{new_href}"' if new_href is not None else m.group(0)

        def src_repl(m):
            new_src = self.resolve_image_src(m.group(1))
            return f'src="{new_src}"' if new_src is not None else m.group(0)

        html = re.sub(r'href="([^"]*)"', href_repl, html)
        # Scoped to <img ...> tags specifically - a bare src="..." regex
        # would also rewrite <script src=...>/<iframe src=...>, running the
        # image resolver against filenames that were never images and
        # producing false "image not found" warnings for them.
        html = IMG_TAG_RE.sub(lambda m: IMG_SRC_RE.sub(src_repl, m.group(0)), html)
        return self.style_broken_and_external_links(html)

    def classify_href(self, href: str):
        """Classifies an *already rewritten* href: 'external' for off-site
        (or mailto:) links, 'broken' for a same-tree ".md" reference that's
        still literally "foo.md" because resolve_href couldn't find it, or
        None for anything that resolved fine (now "/id.html...") or is a
        same-page "#anchor" link."""
        if not href or href.startswith("#"):
            return None
        if EXTERNAL_HREF_RE.match(href):
            return "external"
        if MD_LINK_RE.match(href):
            return "broken"
        return None

    def style_broken_and_external_links(self, html: str) -> str:
        """Colors broken and off-site <a> tags with --broken-ext-link-color
        (from the config passed on the command line) so readers can tell at a
        glance which links leave this site or don't go anywhere at all."""
        if not self.broken_ext_link_color:
            return html

        def repl(m):
            tag = m.group(0)
            if self.classify_href(m.group(1)) is None:
                return tag
            rule = f"color: {self.broken_ext_link_color};"
            # An <a> that already carries a style= (rare, but Writerside's
            # raw HTML passthrough allows it) would otherwise end up with
            # two style attributes - HTML5 keeps only the first, so the
            # color being added here would be the one silently ignored.
            # Merge into the existing attribute instead of appending a
            # second one.
            existing = STYLE_ATTR_RE.search(tag)
            if existing:
                sep = "" if not existing.group(1).strip() or existing.group(1).rstrip().endswith(";") else "; "
                return tag[:existing.start(1)] + existing.group(1) + sep + " " + rule + tag[existing.end(1):]
            return tag[:-1] + f' style="{rule}">'

        return LINK_TAG_RE.sub(repl, html)

    def fold_image_attrs(self, tokens) -> list:
        """Folds one or more `{...}` attribute-text groups immediately
        following an image (e.g. `![alt](x.png){width="500"}{type="joined"}`
        - CommonMark has no syntax for this, so it otherwise tokenizes as a
        literal "{width=..}{type=..}" text run right after the image) into
        that image's own HTML attributes instead of leaving it as visible
        text. Only the leading `{...}` group(s) are consumed - anything
        after them (a second attribute group is fine, but so is unrelated
        prose the author just happened to write right after the image) is
        kept as ordinary visible text rather than silently discarded along
        with the attribute groups."""
        result = []
        for tok in tokens:
            if tok.type == "text" and result and result[-1].type == "image":
                content = tok.content.strip()
                pos = 0
                folded_any = False
                while True:
                    m = IMAGE_ATTR_GROUP_RE.match(content, pos)
                    if not m:
                        break
                    result[-1].attrs.update(parse_attrs(m.group(1)))
                    pos = m.end()
                    folded_any = True
                if folded_any:
                    remainder = content[pos:]
                    if remainder.strip():
                        tok.content = remainder
                        result.append(tok)
                    continue
            if tok.children:
                tok.children = self.fold_image_attrs(tok.children)
            result.append(tok)
        return result

    def extract_trailing_attrs(self, children) -> dict:
        """Strips trailing `{...}` attribute group(s) off the last text
        child (if any) and returns the parsed attrs - the same Writerside
        `{key="value"}` convention fold_image_attrs handles for images, but
        written after a heading's own text instead (e.g.
        `## Enum classes {id="custom-anchor"}`). Found by running the real
        converter against the live kotlin-web-site corpus: unlike the image
        case, this one was landing as visible garbage text at the end of
        the rendered heading with no attribute handling at all."""
        if not children or children[-1].type != "text":
            return {}
        m = TRAILING_ATTR_GROUPS_RE.search(children[-1].content)
        if not m:
            return {}
        attrs = {}
        for group in re.findall(r"\{([^{}]*)\}", m.group(1)):
            attrs.update(parse_attrs(group))
        if not attrs:
            return {}
        children[-1].content = children[-1].content[: m.start()]
        return attrs

    def render_inline(self, inline_token: Token) -> str:
        children = self.fold_image_attrs(inline_token.children)
        return self.rewrite_urls(self.md.renderer.render(children, self.md.options, {}))

    def convert_nodes(self, nodes: list) -> list:
        blocks = []
        for n in nodes:
            result = self.convert_node(n)
            if result is None:
                continue
            if isinstance(result, list):
                blocks.extend(result)
            else:
                blocks.append(result)
        blocks = self.merge_attr_lines(blocks)
        blocks = self.group_containers(blocks)
        return blocks

    def convert_node(self, node: Node):
        t = node.token
        ttype = t.type

        if ttype == "paragraph_open":
            inline = node.children[0].token
            # "_raw" carries the unescaped markdown source so merge_attr_lines
            # can detect/parse a trailing "{...}" attribute line; it is
            # stripped out of the final block before output.
            return {"type": "paragraph", "html": self.render_inline(inline), "_raw": inline.content}

        if ttype == "heading_open":
            inline = node.children[0].token
            level = int(t.tag[1:])
            # Must run before render_inline (which also folds image attrs
            # off inline.children) so a trailing "{id=...}" doesn't survive
            # into the rendered heading text as literal garbage, and so an
            # explicit id, if given, can override the auto-generated slug -
            # that's the actual point of Writerside authors writing one.
            heading_attrs = self.extract_trailing_attrs(inline.children)
            if heading_attrs.get("id"):
                # An explicit id is an intentional, presumably-stable anchor
                # - registered so a *later* auto-slugified heading won't be
                # renamed into colliding with it, but not itself renamed
                # even if two headings happen to specify the same one.
                heading_id = heading_attrs["id"]
                self.seen_heading_ids.add(heading_id)
            else:
                # heading_attrs being non-empty (just with no "id" key, e.g.
                # only "completion-point") still means a suffix was found
                # and stripped off inline.children - inline.content itself
                # is a separate cached string on the parent token, so it
                # needs the same suffix removed before it's used for the
                # slug, or the leftover "{...}" text would still corrupt it.
                clean_text = TRAILING_ATTR_GROUPS_RE.sub("", inline.content) if heading_attrs else inline.content
                heading_id = self.unique_heading_id(clean_text)
            block = {
                "type": "heading",
                "level": level,
                "id": heading_id,
                "html": self.render_inline(inline),
            }
            if heading_attrs:
                block["attrs"] = heading_attrs
            return block

        if ttype in ("fence", "code_block"):
            # code_block is markdown-it's token for indented (4-space) code,
            # as opposed to fence (```-delimited) - it carries no language
            # info, but otherwise wants the same "code" block shape. Without
            # this case it fell through to the generic fallback below and
            # came out as an unescaped "html" block instead - raw <, & etc.
            # in the code would be interpreted as markup rather than shown
            # as text, and it lost its code typing entirely.
            return {
                "type": "code",
                "lang": (t.info or "").strip() or None,
                "code": t.content,
                "attrs": {},
            }

        if ttype == "blockquote_open":
            return {
                "type": "blockquote",
                "attrs": {},
                "blocks": self.convert_nodes(node.children),
            }

        if ttype in ("bullet_list_open", "ordered_list_open"):
            items = []
            for item_node in node.children:
                if item_node.token.type == "list_item_open":
                    items.append({"blocks": self.convert_nodes(item_node.children)})
            return {"type": "list", "ordered": ttype == "ordered_list_open", "items": items}

        if ttype == "table_open":
            headers = []
            rows = []
            for section in node.children:
                if section.token.type == "thead_open":
                    for tr in section.children:
                        row = [self.render_inline(td.children[0].token) for td in tr.children]
                        headers = row
                elif section.token.type == "tbody_open":
                    for tr in section.children:
                        row = [self.render_inline(td.children[0].token) for td in tr.children]
                        rows.append(row)
            return {"type": "table", "headers": headers, "rows": rows}

        if ttype == "hr":
            return {"type": "hr"}

        if ttype == "html_block":
            # CommonMark merges consecutive non-blank-line-separated HTML
            # lines into a single html_block token, so a lone <tabs ...> and
            # the <tab ...> that immediately follows it commonly land in the
            # same token. Split back into lines so each tag can be matched
            # (and grouped into a container) independently.
            result = []
            raw_run = []

            def flush_raw():
                if raw_run:
                    result.append({"type": "html", "html": self.rewrite_urls("\n".join(raw_run))})
                    raw_run.clear()

            for line in t.content.splitlines():
                m = TAG_RE.match(line.strip())
                if m:
                    flush_raw()
                    closing, tag, attrstr, self_closing = m.groups()
                    attrs = parse_attrs(attrstr)
                    tag = tag.lower()
                    if self_closing:
                        # "<tab .../>" has no children and no separate
                        # closer - emit the open/close pair immediately
                        # rather than treating it as an opener that leaves
                        # the container open for the rest of the page.
                        result.append({"type": "tag_marker", "closing": False, "tag": tag, "attrs": attrs})
                        result.append({"type": "tag_marker", "closing": True, "tag": tag, "attrs": {}})
                    else:
                        result.append({
                            "type": "tag_marker",
                            "closing": bool(closing),
                            "tag": tag,
                            "attrs": attrs,
                        })
                elif line.strip():
                    raw_run.append(line)
            flush_raw()
            return result

        if ttype == "inline":
            # top-level bare inline content (e.g. an attribute line with no
            # surrounding paragraph); treat like a paragraph.
            return {"type": "paragraph", "html": self.render_inline(t), "_raw": t.content}

        # Fallback: anything not explicitly handled (images are inline-only,
        # so plain "image" blocks don't occur at block level; captured via
        # paragraph HTML instead).
        return {"type": "html", "html": self.rewrite_urls(str(t.content or ""))}

    @staticmethod
    def merge_attr_lines(blocks: list) -> list:
        """Fold a standalone `{key="value"}` paragraph into the preceding block's attrs."""
        merged = []
        for b in blocks:
            if b["type"] == "paragraph":
                raw = b.pop("_raw", "").strip()
                m = ATTR_LINE_RE.match(raw)
                if m:
                    parsed = parse_attrs(m.group(1))
                    # Only fold (and drop the paragraph) when something was
                    # actually recovered - a `{...}`-shaped paragraph that
                    # parses to no attrs (e.g. `{ a.length }`, or a real
                    # attr line that ATTR_PAIR_RE just doesn't match) is
                    # still real page content, not a decoration to discard.
                    if parsed and merged:
                        merged[-1].setdefault("attrs", {}).update(parsed)
                        continue
            merged.append(b)
        return merged

    def group_containers(self, blocks: list) -> list:
        """Turn <tabs>/<tab>/<note>/<tip>/<warning> tag_marker pairs into nested blocks."""
        root: dict = {"blocks": []}
        stack = [root]
        for b in blocks:
            if b["type"] == "tag_marker":
                if not b["closing"]:
                    node = {"type": b["tag"], "attrs": b["attrs"], "blocks": []}
                    stack[-1]["blocks"].append(node)
                    stack.append(node)
                elif len(stack) > 1 and stack[-1]["type"] == b["tag"]:
                    stack.pop()
                else:
                    # A closing marker that doesn't match the top of the
                    # stack used to be discarded outright, leaving the
                    # wrong frame open - everything after it then landed
                    # inside a container it was never written to be part
                    # of. This always indicates malformed source (a missing
                    # closer somewhere), so it's always worth a warning -
                    # pop back to the nearest open frame of the same tag
                    # anywhere on the stack when one exists (closing
                    # whatever was left open in between), or just ignore the
                    # marker if it doesn't.
                    print(f"warning: unmatched </{b['tag']}> in {self.current_source!r}", file=sys.stderr)
                    self.warnings.append(
                        {"kind": "tag", "source": self.current_source, "reference": f"</{b['tag']}>"}
                    )
                    match_depth = next(
                        (i for i in range(len(stack) - 1, 0, -1) if stack[i]["type"] == b["tag"]), None
                    )
                    if match_depth is not None:
                        del stack[match_depth:]
                continue
            stack[-1]["blocks"].append(b)

        return Converter._finalize_list(root["blocks"])

    @staticmethod
    def _finalize_list(blocks: list) -> list:
        """Maps _finalize_container over a list, splicing in any block it
        unwraps back into a plain list instead of a "tabs" block (see below)
        in place, rather than nesting a list-within-a-list."""
        result = []
        for b in blocks:
            out = Converter._finalize_container(b)
            if isinstance(out, list):
                result.extend(out)
            else:
                result.append(out)
        return result

    @staticmethod
    def _finalize_container(b: dict):
        if b.get("type") == "tabs" and "blocks" in b:
            children = Converter._finalize_list(b["blocks"])
            tab_children = [c for c in children if c.get("type") == "tab"]
            if tab_children:
                # Well-formed <tabs><tab>...</tab>...</tabs>. Any sibling
                # that isn't itself a <tab> (intro/trailing prose written
                # inside the <tabs> block) doesn't belong inside any one
                # tab, so splice it back in at the tabs block's own
                # position instead of dropping it - only the <tab> children
                # feed the "tabs" list below.
                tabs_block = {
                    "type": "tabs",
                    "attrs": b["attrs"],
                    "tabs": [
                        {"title": c["attrs"].get("title"), "attrs": c["attrs"], "blocks": c["blocks"]}
                        for c in tab_children
                    ],
                }
                if len(tab_children) == len(children):
                    return tabs_block
                result = []
                inserted = False
                for c in children:
                    if c.get("type") == "tab":
                        if not inserted:
                            result.append(tabs_block)
                            inserted = True
                        continue
                    result.append(c)
                return result
            if len(children) >= 2 and all(c.get("type") == "code" for c in children):
                # Bare <tabs> wrapping only fenced code blocks with no <tab>
                # tags at all (seen in some compatibility guide pages, e.g.
                # a Kotlin and a Groovy fence back to back) - Writerside
                # authors apparently rely on adjacency here instead of
                # writing <tab> explicitly, so treat each code block's own
                # language as its tab instead of rendering a tabs shell with
                # no tabs in it (which silently dropped every one of these
                # code blocks - see the "type": "tabs" but no "tabs" key
                # schema mismatch this used to produce).
                return {
                    "type": "tabs",
                    "attrs": b["attrs"],
                    "tabs": [
                        {
                            "title": (c["lang"] or "").capitalize() or f"Tab {i + 1}",
                            "attrs": {"group-key": (c["lang"] or "").lower() or str(i + 1)},
                            "blocks": [c],
                        }
                        for i, c in enumerate(children)
                    ],
                }
            # Bare <tabs> wrapper with no <tab> children and no recognizable
            # tab structure to synthesize - there's nothing tab-like left to
            # preserve, so drop the wrapper and splice its content in place
            # instead of emitting a "tabs" block with no "tabs" key (which
            # templates/page.peb can't render - it silently produces an
            # empty <div class="tabs"> and drops the content entirely).
            return children
        if "blocks" in b:
            b["blocks"] = Converter._finalize_list(b["blocks"])
        return b

    def convert_file(self, path: Path, page_id: str, source_rel: str) -> dict:
        self.current_source = source_rel
        self.seen_heading_ids = set()
        raw = path.read_text(encoding="utf-8")
        # Substitute %variables% in the raw source, before markdown-it ever
        # sees it. Doing this post-render instead would (a) miss the title,
        # which is extracted straight from the raw source, and (b) run into
        # markdown-it percent-encoding "%" inside link URLs, which turns
        # "%kotlinEapVersion%" into "%25kotlinEapVersion%25" before we'd get
        # a chance to match it.
        raw = substitute_vars(raw, self.variables)
        title, body = extract_title(raw)
        tokens = self.md.parse(body)
        tree = build_tree(tokens)
        blocks = self.convert_nodes(tree)
        return {
            "id": page_id,
            "sourceFile": source_rel,
            "title": title,
            "blocks": blocks,
        }


def make_markdown_it() -> MarkdownIt:
    md = MarkdownIt("commonmark")
    md.enable("table")
    return md


CONFIG_KEYS = ("broken-ext-link-color", "menu-no-link-color")


def load_config(config_path: Path) -> dict:
    """Loads the {"broken-ext-link-color": ..., "menu-no-link-color": ...}
    theming config. Missing keys just disable that particular styling (a
    warning is printed) rather than being a hard error, since neither is
    required for the JSON conversion itself to be correct. A present key
    that isn't a plausible CSS color, however, IS a hard error:
    broken-ext-link-color is interpolated directly into a `style="color:
    ...;"` HTML attribute (see style_broken_and_external_links), so an
    unvalidated value is a markup-injection hole - config.json is
    repo-controlled today, not attacker-reachable, but validating here means
    that stays true if it ever grows a CI override."""
    config = json.loads(config_path.read_text(encoding="utf-8"))
    for key in CONFIG_KEYS:
        if key not in config:
            print(f"warning: config {config_path} is missing {key!r}; that styling will be skipped", file=sys.stderr)
        elif not isinstance(config[key], str) or not COLOR_RE.match(config[key]):
            print(f"error: config {config_path} has an invalid {key!r} value {config[key]!r} "
                  f"(expected a hex color like \"#cc0000\" or a CSS color name)", file=sys.stderr)
            sys.exit(1)
    return config


def _copy_if_changed(src, dst, *, follow_symlinks=True):
    """shutil.copytree copy_function that skips a file whose size and mtime
    already match the destination - kotlin-web-site's images/ is roughly
    1,800 files / 90MB, and copytree(dirs_exist_ok=True) otherwise re-copies
    every one of them on every run even when only a single topic changed."""
    if os.path.exists(dst):
        s_stat, d_stat = os.stat(src), os.stat(dst)
        if s_stat.st_size == d_stat.st_size and int(s_stat.st_mtime) == int(d_stat.st_mtime):
            return dst
    return shutil.copy2(src, dst, follow_symlinks=follow_symlinks)


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("docs_root", type=Path, help="Path to kotlin-web-site/docs")
    parser.add_argument("output_dir", type=Path, help="Directory to write JSON files into")
    parser.add_argument("config", type=Path,
                         help='Path to a JSON config with "broken-ext-link-color" and "menu-no-link-color"')
    parser.add_argument("--topics-subdir", default="topics", help="Subdirectory of docs_root holding .md files")
    parser.add_argument("--images-subdir", default="images", help="Subdirectory of docs_root holding image files")
    parser.add_argument("--allow-failures", action="store_true",
                         help="Exit 0 even if some files failed to convert (default: exit 1 if any did, "
                              "so a CI step calling this can tell a partial run from a complete one)")
    args = parser.parse_args()

    docs_root: Path = args.docs_root
    topics_dir = docs_root / args.topics_subdir
    images_dir = docs_root / args.images_subdir
    if not topics_dir.is_dir():
        print(f"error: {topics_dir} is not a directory", file=sys.stderr)
        sys.exit(1)
    if not args.config.is_file():
        print(f"error: {args.config} does not exist", file=sys.stderr)
        sys.exit(1)

    config = load_config(args.config)
    variables = load_variables(docs_root)
    topic_index = build_topic_index(topics_dir)
    image_index, _image_collisions = build_image_index(images_dir)  # collisions warning already printed inside
    md = make_markdown_it()
    converter = Converter(md, variables, topic_index, image_index,
                           broken_ext_link_color=config.get("broken-ext-link-color"))

    md_files = sorted(topics_dir.rglob("*.md"))
    args.output_dir.mkdir(parents=True, exist_ok=True)

    # menu-no-link-color applies to the sidebar, which build_nav.py builds
    # separately from nav.json/kr.tree - it reads this back from the output
    # directory it's already given, rather than needing its own copy of the
    # config on its own command line.
    (args.output_dir / "theme.json").write_text(
        json.dumps({key: config.get(key) for key in CONFIG_KEYS}, indent=2), encoding="utf-8"
    )

    if images_dir.is_dir():
        shutil.copytree(images_dir, args.output_dir / "images", dirs_exist_ok=True, copy_function=_copy_if_changed)
    else:
        print(f"warning: {images_dir} not found; image references will 404", file=sys.stderr)

    # Clear out whatever a previous run wrote here first - otherwise a topic
    # deleted upstream since then keeps its stale JSON (and keeps shipping)
    # forever, since nothing else here ever removes a file on its own.
    topics_out_dir = args.output_dir / args.topics_subdir
    if topics_out_dir.is_dir():
        if topics_out_dir.resolve() == topics_dir.resolve():
            print(f"error: output topics dir {topics_out_dir} is the source topics dir {topics_dir}",
                  file=sys.stderr)
            sys.exit(1)
        shutil.rmtree(topics_out_dir)

    count = 0
    failed = 0
    for md_path in md_files:
        rel = md_path.relative_to(topics_dir)
        # .as_posix(), not str() - build_topic_index resolves cross-page
        # links through the same forward-slashed ids, so an OS-native
        # separator here would make the two disagree on Windows.
        page_id = rel.with_suffix("").as_posix()
        source_rel = f"{args.topics_subdir}/{rel.as_posix()}"
        try:
            page = converter.convert_file(md_path, page_id, source_rel)
        except Exception as exc:  # noqa: BLE001 - surface which file broke
            print(f"error converting {md_path}: {exc}", file=sys.stderr)
            failed += 1
            continue
        out_path = args.output_dir / args.topics_subdir / rel.with_suffix(".json")
        out_path.parent.mkdir(parents=True, exist_ok=True)
        out_path.write_text(json.dumps(page, separators=(",", ":"), ensure_ascii=False), encoding="utf-8")
        count += 1

    print(f"Converted {count}/{len(md_files)} files into {args.output_dir}")
    if failed and not args.allow_failures:
        print(f"error: {failed} file(s) failed to convert (pass --allow-failures to tolerate this)", file=sys.stderr)
        sys.exit(1)


if __name__ == "__main__":
    main()
