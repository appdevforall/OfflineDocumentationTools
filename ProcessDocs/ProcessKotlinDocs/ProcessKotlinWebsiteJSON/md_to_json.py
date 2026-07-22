#!/usr/bin/env python3
"""
Converts JetBrains Writerside-flavored Markdown (as used by kotlin-web-site/docs)
into a simple JSON block schema suitable for a templating engine.

Usage:
    python3 md_to_json.py <docs-root> <output-dir> <config> [--topics-subdir topics]

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
  {"type": "paragraph", "html": "..."}
  {"type": "code", "lang": "kotlin", "code": "...", "attrs": {"kotlin-runnable": "true"}}
  {"type": "blockquote", "attrs": {"style": "note"}, "blocks": [...]}
  {"type": "list", "ordered": false, "items": [{"blocks": [...]}]}
  {"type": "table", "headers": ["a", "b"], "rows": [["1", "2"]]}
  {"type": "image", "src": "...", "alt": "..."}
  {"type": "hr"}
  {"type": "tabs", "attrs": {"group": "build-system"},
   "tabs": [{"title": "Gradle", "attrs": {"group-key": "gradle"}, "blocks": [...]}]}
  {"type": "html", "html": "<raw passthrough for anything unrecognized>"}

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
"""
import argparse
import json
import re
import shutil
import sys
import xml.etree.ElementTree as ET
from pathlib import Path

from markdown_it import MarkdownIt
from markdown_it.token import Token

TITLE_RE = re.compile(r"^\[//\]:\s*#\s*\(title:\s*(.*?)\)\s*$", re.MULTILINE)
ATTR_LINE_RE = re.compile(r"^\{(.*)\}$")
ATTR_PAIR_RE = re.compile(r'([\w-]+)=(?:"([^"]*)"|(\S+))')
TAG_RE = re.compile(r"^<(/?)(tabs|tab|note|tip|warning)([^>]*)/?>$", re.I)
VAR_RE = re.compile(r"%([\w.-]+)%")
MD_LINK_RE = re.compile(r"^([\w.-]+)\.md(#.*)?$")
EXTERNAL_HREF_RE = re.compile(r"^(?:[a-zA-Z][a-zA-Z0-9+.-]*:)?//|^mailto:", re.I)
LINK_TAG_RE = re.compile(r'<a\b[^>]*\bhref="([^"]*)"[^>]*>')

CONTAINER_TAGS = {"tabs", "tab", "note", "tip", "warning"}


def build_topic_index(topics_dir: Path) -> dict:
    """Bare filename stem (e.g. "enum-classes") -> page id (e.g. "kotlin-tour/enum-classes")."""
    index = {}
    for md_path in sorted(topics_dir.rglob("*.md")):
        page_id = str(md_path.relative_to(topics_dir).with_suffix("")).replace("\\", "/")
        index.setdefault(md_path.stem, page_id)
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
    attrs = {}
    for name, quoted, bare in ATTR_PAIR_RE.findall(attr_str or ""):
        attrs[name] = quoted if quoted != "" or '="' in (attr_str or "") else bare
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
        html = re.sub(r'src="([^"]*)"', src_repl, html)
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
            return tag[:-1] + f' style="color: {self.broken_ext_link_color};">'

        return LINK_TAG_RE.sub(repl, html)

    def fold_image_attrs(self, tokens) -> list:
        """Folds a `{...}` attribute-text token immediately following an
        image (e.g. `![alt](x.png){width="500"}` - CommonMark has no syntax
        for this, so it otherwise tokenizes as a literal "{width=..}" text
        run right after the image) into that image's own HTML attributes
        instead of leaving it as visible text."""
        result = []
        for tok in tokens:
            if tok.type == "text" and result and result[-1].type == "image":
                m = ATTR_LINE_RE.match(tok.content.strip())
                if m:
                    result[-1].attrs.update(parse_attrs(m.group(1)))
                    continue
            if tok.children:
                tok.children = self.fold_image_attrs(tok.children)
            result.append(tok)
        return result

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
            text = inline.content
            return {
                "type": "heading",
                "level": level,
                "id": slugify(text),
                "html": self.render_inline(inline),
            }

        if ttype == "fence":
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
                    closing, tag, attrstr = m.groups()
                    result.append({
                        "type": "tag_marker",
                        "closing": bool(closing),
                        "tag": tag.lower(),
                        "attrs": parse_attrs(attrstr),
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
                if m and merged:
                    merged[-1].setdefault("attrs", {}).update(parse_attrs(m.group(1)))
                    continue
            merged.append(b)
        return merged

    @staticmethod
    def group_containers(blocks: list) -> list:
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
                # Well-formed <tabs><tab>...</tab>...</tabs>.
                return {
                    "type": "tabs",
                    "attrs": b["attrs"],
                    "tabs": [
                        {"title": c["attrs"].get("title"), "attrs": c["attrs"], "blocks": c["blocks"]}
                        for c in tab_children
                    ],
                }
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
    required for the JSON conversion itself to be correct."""
    config = json.loads(config_path.read_text(encoding="utf-8"))
    for key in CONFIG_KEYS:
        if key not in config:
            print(f"warning: config {config_path} is missing {key!r}; that styling will be skipped", file=sys.stderr)
    return config


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("docs_root", type=Path, help="Path to kotlin-web-site/docs")
    parser.add_argument("output_dir", type=Path, help="Directory to write JSON files into")
    parser.add_argument("config", type=Path,
                         help='Path to a JSON config with "broken-ext-link-color" and "menu-no-link-color"')
    parser.add_argument("--topics-subdir", default="topics", help="Subdirectory of docs_root holding .md files")
    parser.add_argument("--images-subdir", default="images", help="Subdirectory of docs_root holding image files")
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
    image_index, _image_collisions = build_image_index(images_dir)
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
        shutil.copytree(images_dir, args.output_dir / "images", dirs_exist_ok=True)
    else:
        print(f"warning: {images_dir} not found; image references will 404", file=sys.stderr)

    count = 0
    for md_path in md_files:
        rel = md_path.relative_to(topics_dir)
        page_id = str(rel.with_suffix(""))
        source_rel = str(Path(args.topics_subdir) / rel)
        try:
            page = converter.convert_file(md_path, page_id, source_rel)
        except Exception as exc:  # noqa: BLE001 - surface which file broke
            print(f"error converting {md_path}: {exc}", file=sys.stderr)
            continue
        out_path = args.output_dir / args.topics_subdir / rel.with_suffix(".json")
        out_path.parent.mkdir(parents=True, exist_ok=True)
        out_path.write_text(json.dumps(page, separators=(",", ":"), ensure_ascii=False), encoding="utf-8")
        count += 1

    print(f"Converted {count}/{len(md_files)} files into {args.output_dir}")


if __name__ == "__main__":
    main()
