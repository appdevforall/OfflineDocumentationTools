"""Regression tests for md_to_json.py, one per bug found in PR #23 review.

Each test's docstring names the finding it guards; several reuse the
reviewers' own repro snippets verbatim so a future regression reproduces the
exact case that was originally reported.
"""
import json
import subprocess
import sys
from pathlib import Path

import pytest

import md_to_json as m


class FakeToken:
    """Minimal stand-in for markdown_it.token.Token - just the attributes
    fold_image_attrs/extract_trailing_attrs actually touch."""

    def __init__(self, type_, content="", children=None):
        self.type = type_
        self.content = content
        self.children = children
        self.attrs = {}


def make_converter(**kwargs):
    return m.Converter(m.make_markdown_it(), {}, **kwargs)


# --- TAG_RE: <table> must not match as tag "tab" -----------------------

def test_tag_re_does_not_match_table():
    """TAG_RE previously had no word boundary after the tag-name
    alternation, so <table>/</table> matched as tag "tab" with attrs "le",
    silently eating every raw HTML table in the corpus."""
    assert m.TAG_RE.match("<table>") is None
    assert m.TAG_RE.match("</table>") is None
    assert m.TAG_RE.match("<tabindex>") is None


def test_tag_re_still_matches_real_container_tags():
    assert m.TAG_RE.match('<tabs group="build">') is not None
    assert m.TAG_RE.match('<tab title="Gradle">') is not None
    assert m.TAG_RE.match("</warning>") is not None


# --- TAG_RE / html_block: self-closing tags must not stay open ----------

def test_tag_re_captures_self_closing_marker_separately():
    """The attrs group used to be greedy, so it swallowed a self-closing
    tag's trailing "/" before the optional "/?" at the end ever got a
    chance to match it - "<tab .../>" came out with an empty closing
    group, i.e. indistinguishable from a plain opener."""
    closing, tag, attrs, self_closing = m.TAG_RE.match('<tab title="A"/>').groups()
    assert (closing, tag, self_closing) == ("", "tab", "/")
    assert attrs.strip() == 'title="A"'

    closing, tag, attrs, self_closing = m.TAG_RE.match('<tab title="A">').groups()
    assert self_closing == ""


def test_html_block_self_closing_tag_emits_open_and_close_markers():
    """convert_node's html_block dispatch must turn a self-closing tag into
    an immediate open/close pair rather than a bare opener - otherwise the
    container never closes and silently nests the rest of the page."""
    node = m.Node(FakeToken("html_block", content='<tab title="A"/>'))
    conv = make_converter()
    result = conv.convert_node(node)
    assert [(b["type"], b["closing"], b["tag"]) for b in result] == [
        ("tag_marker", False, "tab"),
        ("tag_marker", True, "tab"),
    ]


def test_self_closing_tag_does_not_swallow_trailing_content():
    """End-to-end repro: a self-closing <tab/> immediately followed by a
    paragraph must not leave that paragraph nested inside the tab."""
    node = m.Node(FakeToken("html_block", content='<tab title="A"/>'))
    conv = make_converter()
    conv.current_source = "test.md"
    markers = conv.convert_node(node)
    blocks = markers + [{"type": "paragraph", "html": "After."}]
    result = conv.group_containers(blocks)
    assert [b["html"] for b in result if b.get("type") == "paragraph"] == ["After."]
    assert conv.warnings == []


# --- convert_node: indented code_block must render as code, not html ----

def test_indented_code_block_renders_as_code_not_html():
    """convert_node only handled markdown-it's "fence" (```-delimited)
    token; an indented (4-space) code block produces a different token
    type, "code_block", which fell through to the generic fallback and
    came out as an unescaped "html" block instead - raw "<"/"&" in the
    code would be interpreted as markup rather than shown as text, and the
    block lost its code typing entirely."""
    md = m.make_markdown_it()
    conv = m.Converter(md, {})
    tokens = md.parse('    List<String> x = listOf("a", "b");\n')
    tree = m.build_tree(tokens)
    block = conv.convert_node(tree[0])
    assert block["type"] == "code"
    assert block["lang"] is None
    assert "List<String>" in block["code"]


# --- convert_node: html_block preserves blank lines in a raw run -------

def test_html_block_preserves_blank_lines_within_a_raw_run():
    """CommonMark's html_block type 1 (<pre>/<textarea>/<script>/<style>)
    continues across blank lines, so their content can genuinely contain
    one - but a blank line failed the "elif line.strip():" check the same
    way a tag-marker line failed to match TAG_RE, so it was dropped
    entirely instead of preserved, reflowing the raw HTML."""
    node = m.Node(FakeToken("html_block", content="<pre>\nline1\n\nline3\n</pre>"))
    result = make_converter().convert_node(node)
    assert result == [{"type": "html", "html": "<pre>\nline1\n\nline3\n</pre>"}]


# --- slugify: hyphenate punctuation instead of deleting it --------------

def test_slugify_hyphenates_punctuation_instead_of_deleting_it():
    """Deleting punctuation merged e.g. "package.json" into "packagejson",
    diverging from Writerside's own anchor algorithm (which hyphenates it)
    on any heading with a "." "/" ":" "(" ")" etc. - measured at 209 dead
    #anchor links across the real kotlin-web-site corpus; these are its
    own reported examples."""
    assert m.slugify("package.json customization") == "package-json-customization"
    assert m.slugify("Kotlin/JS") == "kotlin-js"
    assert m.slugify("New default JVM target: 1.8") == "new-default-jvm-target-1-8"
    assert m.slugify("Generation of TypeScript declaration files (d.ts)") == \
        "generation-of-typescript-declaration-files-d-ts"


def test_slugify_still_collapses_whitespace_and_strips_edges():
    assert m.slugify("  Hello   World  ") == "hello-world"
    assert m.slugify("Already-Hyphenated") == "already-hyphenated"


# --- ATTR_PAIR_RE / parse_attrs -----------------------------------------

def test_parse_attrs_allows_whitespace_around_equals():
    """{ style = "note" } used to parse to {} entirely (ATTR_PAIR_RE
    required name=value with no surrounding whitespace)."""
    assert m.parse_attrs('style = "note"') == {"style": "note"}


def test_parse_attrs_mixed_quoted_and_bare_pairs():
    """The whole-string '="' check forced every bare value in the same
    attr_str to '' as soon as ANY pair in it was quoted."""
    assert m.parse_attrs('title="Gradle" group-key=gradle') == {
        "title": "Gradle", "group-key": "gradle",
    }


def test_parse_attrs_quoted_empty_value_stays_empty_string():
    assert m.parse_attrs('title=""') == {"title": ""}


# --- merge_attr_lines: don't delete content that parsed to nothing -----

def test_merge_attr_lines_keeps_paragraph_with_no_parsed_attrs():
    """A {...}-shaped paragraph that parses to zero attrs (e.g. a lambda
    literal, not a real attribute line) was deleted outright."""
    blocks = [
        {"type": "paragraph", "html": "Plain para", "_raw": "Plain para"},
        {"type": "paragraph", "html": "{ it.length }", "_raw": "{ it.length }"},
    ]
    merged = m.Converter.merge_attr_lines(blocks)
    assert [b["html"] for b in merged] == ["Plain para", "{ it.length }"]


def test_merge_attr_lines_still_folds_a_real_attr_line():
    blocks = [
        {"type": "blockquote", "attrs": {}, "blocks": []},
        {"type": "paragraph", "html": '{ style = "note" }', "_raw": '{ style = "note" }'},
    ]
    merged = m.Converter.merge_attr_lines(blocks)
    assert len(merged) == 1
    assert merged[0]["attrs"] == {"style": "note"}


# --- fold_image_attrs: multiple brace groups + trailing prose ----------

def test_fold_image_attrs_folds_multiple_adjacent_groups():
    """`{width=25}{type="joined"}` used to be captured as ONE greedy
    ATTR_LINE_RE match spanning both braces, so parse_attrs saw the literal
    string 'width=25}{type="joined"' and produced {'width': ''}."""
    img = FakeToken("image")
    text = FakeToken("text", '{width=25}{type="joined"}')
    make_converter().fold_image_attrs([img, text])
    assert img.attrs == {"width": "25", "type": "joined"}


def test_fold_image_attrs_preserves_trailing_prose():
    """getting-started.md:89 - ![Slack](slack.svg){width=25}{type="joined"}
    Slack: [get an invite](...) - the attribute groups must fold, but
    "Slack: " must survive as visible text, not be silently dropped."""
    img = FakeToken("image")
    text = FakeToken("text", '{width=25}{type="joined"} Slack: ')
    out = make_converter().fold_image_attrs([img, text])
    assert img.attrs == {"width": "25", "type": "joined"}
    assert len(out) == 2
    # Asserting the exact string (not .strip()'d) matters here: the
    # remainder used to be re-stripped after slicing, silently eating the
    # leading space that separates the image from this text - "Slack:"
    # would pass that bug just as well as " Slack:" does.
    assert out[1].content == " Slack:"


def test_fold_image_attrs_single_group_still_leaves_no_leftover_token():
    """Backward-compat: the original, common case (one attribute group,
    nothing else) must still fold cleanly with no empty leftover token."""
    img = FakeToken("image")
    text = FakeToken("text", '{width="500"}')
    out = make_converter().fold_image_attrs([img, text])
    assert img.attrs == {"width": "500"}
    assert out == [img]


def test_fold_image_attrs_leaves_unrelated_text_alone():
    img = FakeToken("image")
    text = FakeToken("text", "just a caption, no braces")
    out = make_converter().fold_image_attrs([img, text])
    assert img.attrs == {}
    assert out == [img, text]


def test_fold_image_attrs_stops_on_group_that_parses_to_nothing():
    """folded_any used to be set on a regex MATCH, not a successful parse,
    so a brace group that isn't an attribute group (e.g. a CSS class
    shorthand, or prose) was swallowed right along with any group already
    folded - merge_attr_lines and extract_trailing_attrs both already
    guard their own "{...}" folding on this same thing."""
    img = FakeToken("image")
    text = FakeToken("text", "{.rounded} caption here")
    out = make_converter().fold_image_attrs([img, text])
    assert img.attrs == {}
    assert out == [img, text]


def test_fold_image_attrs_keeps_a_group_already_folded_before_a_prose_group():
    img = FakeToken("image")
    text = FakeToken("text", '{width="500"}{ this is prose }')
    out = make_converter().fold_image_attrs([img, text])
    assert img.attrs == {"width": "500"}
    assert len(out) == 2
    assert out[1].content == "{ this is prose }"


def test_fold_image_attrs_folds_trailing_group_after_a_link():
    """Nothing on the inline path folded a "{...}" group written right
    after a *link* (only after an image or a heading) - it rendered as
    literal visible text instead of becoming an attribute on the <a>. Real
    hit: js/js-ir-compiler.md's `{nullable="true"}` right after a link."""
    link_open = FakeToken("link_open")
    inner_text = FakeToken("text", "link text")
    link_close = FakeToken("link_close")
    trailing = FakeToken("text", '{target="_blank"} rest')
    out = make_converter().fold_image_attrs([link_open, inner_text, link_close, trailing])
    assert link_open.attrs == {"target": "_blank"}
    assert out[-1].content == " rest"


def test_fold_image_attrs_link_group_that_parses_to_nothing_is_left_alone():
    link_open = FakeToken("link_open")
    link_close = FakeToken("link_close")
    trailing = FakeToken("text", "{not an attr}")
    out = make_converter().fold_image_attrs([link_open, link_close, trailing])
    assert link_open.attrs == {}
    assert out[-1].content == "{not an attr}"


# --- <tabs> block: don't discard non-<tab> siblings ---------------------

def test_tabs_block_preserves_intro_and_trailing_prose():
    """Intro/trailing prose written inside a <tabs>...</tabs> block used to
    vanish entirely - only <tab> children were kept."""
    blocks = [
        {"type": "tag_marker", "closing": False, "tag": "tabs", "attrs": {"group": "build"}},
        {"type": "paragraph", "html": "Intro paragraph that should not vanish."},
        {"type": "tag_marker", "closing": False, "tag": "tab", "attrs": {"title": "Gradle"}},
        {"type": "paragraph", "html": "gradle body"},
        {"type": "tag_marker", "closing": True, "tag": "tab", "attrs": {}},
        {"type": "tag_marker", "closing": False, "tag": "tab", "attrs": {"title": "Maven"}},
        {"type": "paragraph", "html": "maven body"},
        {"type": "tag_marker", "closing": True, "tag": "tab", "attrs": {}},
        {"type": "paragraph", "html": "Trailing note."},
        {"type": "tag_marker", "closing": True, "tag": "tabs", "attrs": {}},
    ]
    conv = make_converter()
    conv.current_source = "test.md"
    result = conv.group_containers(blocks)
    paragraphs = [b["html"] for b in result if b.get("type") == "paragraph"]
    tabs_blocks = [b for b in result if b.get("type") == "tabs"]
    assert "Intro paragraph that should not vanish." in paragraphs
    assert "Trailing note." in paragraphs
    assert len(tabs_blocks) == 1
    assert len(tabs_blocks[0]["tabs"]) == 2


def test_well_formed_tabs_with_no_siblings_unchanged():
    blocks = [
        {"type": "tag_marker", "closing": False, "tag": "tabs", "attrs": {}},
        {"type": "tag_marker", "closing": False, "tag": "tab", "attrs": {"title": "A"}},
        {"type": "paragraph", "html": "a"},
        {"type": "tag_marker", "closing": True, "tag": "tab", "attrs": {}},
        {"type": "tag_marker", "closing": True, "tag": "tabs", "attrs": {}},
    ]
    conv = make_converter()
    conv.current_source = "test.md"
    result = conv.group_containers(blocks)
    assert len(result) == 1
    assert result[0]["type"] == "tabs"


# --- group_containers: mismatched closing tag ---------------------------

def test_unmatched_closing_tag_does_not_relocate_trailing_content():
    """A missing </tab> before </tabs> used to leave the </tabs> marker
    silently discarded, keeping the tab frame open - content written after
    the whole container ended up nested inside it instead."""
    blocks = [
        {"type": "tag_marker", "closing": False, "tag": "tabs", "attrs": {}},
        {"type": "tag_marker", "closing": False, "tag": "tab", "attrs": {"title": "A"}},
        {"type": "paragraph", "html": "body A"},
        {"type": "tag_marker", "closing": True, "tag": "tabs", "attrs": {}},  # missing </tab> first
        {"type": "paragraph", "html": "After everything."},
    ]
    conv = make_converter()
    conv.current_source = "test.md"
    result = conv.group_containers(blocks)
    top_level_paragraphs = [b["html"] for b in result if b.get("type") == "paragraph"]
    assert "After everything." in top_level_paragraphs
    assert any(w["kind"] == "tag" for w in conv.warnings)


def test_well_formed_open_close_does_not_warn():
    blocks = [
        {"type": "tag_marker", "closing": False, "tag": "note", "attrs": {}},
        {"type": "paragraph", "html": "hi"},
        {"type": "tag_marker", "closing": True, "tag": "note", "attrs": {}},
    ]
    conv = make_converter()
    conv.current_source = "ok.md"
    conv.group_containers(blocks)
    assert conv.warnings == []


def test_top_level_unmatched_closer_does_not_crash():
    """A closing tag_marker with no matching opener anywhere on the stack -
    stack is back down to the bare {"blocks": []} root, which has no "type"
    key at all - used to raise KeyError('type') instead of hitting the
    already-correct "no matching frame" warning path."""
    blocks = [
        {"type": "tag_marker", "closing": True, "tag": "note", "attrs": {}},
        {"type": "paragraph", "html": "After."},
    ]
    conv = make_converter()
    conv.current_source = "test.md"
    result = conv.group_containers(blocks)
    assert [b["html"] for b in result if b.get("type") == "paragraph"] == ["After."]
    assert any(w["kind"] == "tag" for w in conv.warnings)


def test_container_left_open_at_eof_warns():
    """The unmatched-*closer* case (above) always warns, since that always
    means malformed source - a container that's still open when the block
    list runs out (a missing closer, with no closing marker at all) is the
    same signal from the other end, but used to close over the rest of the
    page with no warning at all."""
    blocks = [
        {"type": "tag_marker", "closing": False, "tag": "note", "attrs": {}},
        {"type": "paragraph", "html": "Inside note."},
        {"type": "paragraph", "html": "Rest of page paragraph."},
    ]
    conv = make_converter()
    conv.current_source = "test.md"
    conv.group_containers(blocks)
    assert any(w["kind"] == "tag" for w in conv.warnings)


def test_well_formed_container_at_eof_does_not_warn():
    blocks = [
        {"type": "tag_marker", "closing": False, "tag": "note", "attrs": {}},
        {"type": "paragraph", "html": "Inside note."},
        {"type": "tag_marker", "closing": True, "tag": "note", "attrs": {}},
    ]
    conv = make_converter()
    conv.current_source = "test.md"
    conv.group_containers(blocks)
    assert conv.warnings == []


# --- style_broken_and_external_links: no duplicate style= ---------------

def test_broken_link_style_merges_into_existing_style_attr():
    """Appending style="color: ...;" unconditionally left an <a> that
    already had a style= with two style attributes - HTML5 keeps only the
    first, silently dropping the color being added."""
    conv = make_converter(broken_ext_link_color="#cc0000")
    html = conv.style_broken_and_external_links('<a href="https://x.com" style="font-weight:bold">x</a>')
    assert html.count('style="') == 1
    assert "color: #cc0000" in html
    assert "font-weight:bold" in html


def test_broken_link_style_merge_inserts_semicolon_separator():
    """The merge used to concatenate the existing style value and the
    injected rule with just a space when the existing value had no trailing
    ";" - "font-weight: bold color: red;" is ONE malformed CSS declaration,
    which browsers discard entirely, losing both rules."""
    conv = make_converter(broken_ext_link_color="#cc0000")
    html = conv.style_broken_and_external_links(
        '<a href="https://x.com" style="font-weight: bold">x</a>')
    assert html.count('style="') == 1
    assert "font-weight: bold;" in html
    assert "color: #cc0000;" in html


def test_non_broken_link_untouched():
    conv = make_converter(broken_ext_link_color="#cc0000", topic_index={})
    html = conv.style_broken_and_external_links('<a href="#anchor">x</a>')
    assert html == '<a href="#anchor">x</a>'


# --- rewrite_urls: src= scoped to <img>, not any element ---------------

def test_src_rewrite_only_touches_img_tags():
    """A bare src="..." regex also rewrote <script src=...>/<iframe
    src=...>, running the image resolver against non-image filenames and
    producing false "image not found" warnings for them."""
    conv = make_converter(image_index={"x.js": "sub/x.js"})
    html = conv.rewrite_urls('<script src="x.js"></script><iframe src="x.js"></iframe><img src="x.js">')
    assert 'src="x.js"></script>' in html
    assert 'src="x.js"></iframe>' in html
    assert '/images/sub/x.js' in html
    assert not any(w["kind"] == "image" for w in conv.warnings)


# --- resolve_href / resolve_image_src: bare-filename path prefixes ------

def test_resolve_href_resolves_a_link_with_a_path_prefix():
    """MD_LINK_RE's char class excluded "/", so a link like
    "tour/hello.md" (a real, if unusual, way to reference a topic) neither
    resolved nor got flagged "broken" by classify_href (same regex) - it
    shipped verbatim with no warning at all, unlike the unknown-bare-stem
    case just below it."""
    conv = make_converter(topic_index={"hello": "kotlin-tour/hello"})
    assert conv.resolve_href("tour/hello.md#section") == "/kotlin-tour/hello.html#section"
    assert conv.warnings == []


def test_resolve_href_path_prefixed_unknown_stem_still_warns():
    conv = make_converter(topic_index={})
    assert conv.resolve_href("tour/missing.md") is None
    assert conv.warnings == [{"kind": "link", "source": None, "reference": "tour/missing.md"}]


def test_resolve_image_src_resolves_a_bare_filename_with_a_path_prefix():
    """A relative path prefix (as opposed to a bare filename) used to bail
    out silently here with no warning either way - unlike the bare-filename
    miss just below, which does warn. Writerside's own convention (see
    module docstring) is to resolve images by bare filename anywhere under
    images/, same as topics."""
    conv = make_converter(image_index={"pic.png": "sub/pic.png"})
    assert conv.resolve_image_src("sub/pic.png") == "/images/sub/pic.png"
    assert conv.warnings == []


def test_resolve_image_src_path_prefixed_unknown_name_warns():
    conv = make_converter(image_index={})
    assert conv.resolve_image_src("sub/missing.png") is None
    assert conv.warnings == [{"kind": "image", "source": None, "reference": "sub/missing.png"}]


def test_resolve_image_src_still_leaves_absolute_and_external_untouched():
    conv = make_converter(image_index={"pic.png": "pic.png"})
    assert conv.resolve_image_src("/already/absolute.png") is None
    assert conv.resolve_image_src("https://example.com/x.png") is None
    assert conv.warnings == []


# --- load_config: reject an unsafe/invalid color ------------------------

def test_load_config_rejects_markup_injection_payload(tmp_path):
    """broken-ext-link-color is interpolated directly into a style="..."
    HTML attribute; an unvalidated value is a markup-injection hole."""
    config_path = tmp_path / "config.json"
    config_path.write_text(json.dumps({
        "broken-ext-link-color": 'red"><script>alert(1)</script>',
        "menu-no-link-color": "#999999",
    }))
    with pytest.raises(SystemExit) as exc_info:
        m.load_config(config_path)
    assert exc_info.value.code == 1


def test_load_config_rejects_non_string_color_value(tmp_path):
    """A JSON number (an unquoted hex-like value is a plausible hand-edit
    slip, e.g. writing cc0000 instead of "#cc0000") used to raise a bare
    TypeError from COLOR_RE.match(int) instead of the intended clean
    "invalid ... value" error the line below is meant to produce."""
    config_path = tmp_path / "config.json"
    config_path.write_text(json.dumps({
        "broken-ext-link-color": 123,
        "menu-no-link-color": "#999999",
    }))
    with pytest.raises(SystemExit) as exc_info:
        m.load_config(config_path)
    assert exc_info.value.code == 1


def test_load_config_accepts_hex_and_named_colors(tmp_path):
    config_path = tmp_path / "config.json"
    config_path.write_text(json.dumps({
        "broken-ext-link-color": "#cc0000",
        "menu-no-link-color": "gray",
    }))
    config = m.load_config(config_path)
    assert config["broken-ext-link-color"] == "#cc0000"
    assert config["menu-no-link-color"] == "gray"


# --- heading ids: de-dup + explicit {id=...} override -------------------

def test_duplicate_heading_text_gets_deduped_ids():
    """Two headings with identical text used to share a slug, so an anchor
    to the second one landed on the first."""
    conv = make_converter()
    first = conv.unique_heading_id("Overview")
    second = conv.unique_heading_id("Overview")
    assert first == "overview"
    assert second == "overview-2"


def test_heading_trailing_id_attr_overrides_slug_and_is_stripped_from_html():
    """## Checks with `is`/`!is` operators {id="is-and-is-operators"} used
    to render the literal "{id=&quot;is-and-is-operators&quot;}" as visible
    heading text, with no id override and no attribute handling at all."""
    md = m.make_markdown_it()
    conv = m.Converter(md, {})
    tokens = md.parse('## Checks with `is` {id="is-and-is-operators"}\n')
    tree = m.build_tree(tokens)
    block = conv.convert_node(tree[0])
    assert block["id"] == "is-and-is-operators"
    assert "{id=" not in block["html"]
    assert block["attrs"] == {"id": "is-and-is-operators"}


def test_heading_without_trailing_attrs_unaffected():
    md = m.make_markdown_it()
    conv = m.Converter(md, {})
    tokens = md.parse("## Plain heading\n")
    tree = m.build_tree(tokens)
    block = conv.convert_node(tree[0])
    assert block["id"] == "plain-heading"
    assert "attrs" not in block


# --- build_topic_index: collision warning mirrors build_image_index -----

def test_build_topic_index_warns_on_duplicate_stem(tmp_path, capsys):
    """Two topics sharing a filename stem used to resolve first-wins with
    no diagnostic at all, unlike the equivalent image-filename collision."""
    topics = tmp_path / "topics"
    (topics / "native").mkdir(parents=True)
    (topics / "js").mkdir(parents=True)
    (topics / "native" / "basics.md").write_text("native")
    (topics / "js" / "basics.md").write_text("js")

    index = m.build_topic_index(topics)
    assert index["basics"] in ("native/basics", "js/basics")
    assert "warning: ambiguous topic filename 'basics'" in capsys.readouterr().err


def test_build_topic_index_no_warning_without_collision(tmp_path, capsys):
    topics = tmp_path / "topics"
    topics.mkdir()
    (topics / "a.md").write_text("a")
    m.build_topic_index(topics)
    assert capsys.readouterr().err == ""


# --- main(): exit code reflects partial failure -------------------------

def _write_minimal_docs_root(tmp_path):
    docs_root = tmp_path / "docs"
    (docs_root / "topics").mkdir(parents=True)
    (docs_root / "topics" / "good.md").write_text("# Good\n\nHello.\n")
    config = tmp_path / "config.json"
    config.write_text(json.dumps({"broken-ext-link-color": "#cc0000", "menu-no-link-color": "#999999"}))
    return docs_root, config


def _run_main(*args):
    script = Path(__file__).resolve().parent.parent / "md_to_json.py"
    return subprocess.run([sys.executable, str(script), *map(str, args)], capture_output=True, text=True)


def test_main_exits_zero_on_full_success(tmp_path):
    docs_root, config = _write_minimal_docs_root(tmp_path)
    out_dir = tmp_path / "out"
    result = _run_main(docs_root, out_dir, config)
    assert result.returncode == 0
    assert "Converted 1/1" in result.stdout


def test_main_exits_nonzero_when_a_file_fails(tmp_path, monkeypatch):
    """A run that converts nothing (or partially fails) used to print
    "Converted 0/N files" and still exit 0 - a CI step calling this
    couldn't distinguish a complete run from a total failure."""
    docs_root, config = _write_minimal_docs_root(tmp_path)
    # A .md file that isn't valid UTF-8 makes convert_file's read_text raise.
    (docs_root / "topics" / "bad.md").write_bytes(b"\xff\xfe not utf-8")
    out_dir = tmp_path / "out"
    result = _run_main(docs_root, out_dir, config)
    assert result.returncode == 1
    assert "Converted 1/2" in result.stdout


def test_main_allow_failures_exits_zero_despite_failure(tmp_path):
    docs_root, config = _write_minimal_docs_root(tmp_path)
    (docs_root / "topics" / "bad.md").write_bytes(b"\xff\xfe not utf-8")
    out_dir = tmp_path / "out"
    result = _run_main(docs_root, out_dir, config, "--allow-failures")
    assert result.returncode == 0


def test_main_uses_posix_separators_for_nested_page_ids(tmp_path):
    """page_id/sourceFile used str(Path(...)) instead of .as_posix(), which
    would disagree with build_topic_index's forward-slashed ids on Windows."""
    docs_root, config = _write_minimal_docs_root(tmp_path)
    (docs_root / "topics" / "tour").mkdir()
    (docs_root / "topics" / "tour" / "hello.md").write_text("# Hello\n")
    out_dir = tmp_path / "out"
    result = _run_main(docs_root, out_dir, config)
    assert result.returncode == 0
    page = json.loads((out_dir / "topics" / "tour" / "hello.json").read_text())
    assert page["id"] == "tour/hello"
    assert page["sourceFile"] == "topics/tour/hello.md"


def test_main_prunes_stale_topic_json(tmp_path):
    """A topic removed upstream used to keep shipping its stale JSON
    forever, since nothing ever cleared the output topics directory."""
    docs_root, config = _write_minimal_docs_root(tmp_path)
    out_dir = tmp_path / "out"
    assert _run_main(docs_root, out_dir, config).returncode == 0
    assert (out_dir / "topics" / "good.json").exists()

    (docs_root / "topics" / "good.md").unlink()
    (docs_root / "topics" / "new.md").write_text("# New\n")
    assert _run_main(docs_root, out_dir, config).returncode == 0
    assert not (out_dir / "topics" / "good.json").exists()
    assert (out_dir / "topics" / "new.json").exists()


def test_main_prunes_stale_images(tmp_path):
    """copytree(dirs_exist_ok=True) only ever adds/overwrites - an image
    deleted upstream used to keep shipping in the output images/ directory
    forever, unlike a removed topic's stale JSON (pruned by the rmtree
    exercised just above)."""
    docs_root, config = _write_minimal_docs_root(tmp_path)
    (docs_root / "images").mkdir()
    (docs_root / "images" / "keep.png").write_bytes(b"keep")
    (docs_root / "images" / "stale.png").write_bytes(b"stale")
    out_dir = tmp_path / "out"
    assert _run_main(docs_root, out_dir, config).returncode == 0
    assert (out_dir / "images" / "keep.png").exists()
    assert (out_dir / "images" / "stale.png").exists()

    (docs_root / "images" / "stale.png").unlink()
    assert _run_main(docs_root, out_dir, config).returncode == 0
    assert (out_dir / "images" / "keep.png").exists()
    assert not (out_dir / "images" / "stale.png").exists()


def test_main_prunes_now_empty_stale_image_subdirectories(tmp_path):
    docs_root, config = _write_minimal_docs_root(tmp_path)
    (docs_root / "images" / "sub").mkdir(parents=True)
    (docs_root / "images" / "sub" / "only.png").write_bytes(b"only")
    out_dir = tmp_path / "out"
    assert _run_main(docs_root, out_dir, config).returncode == 0
    assert (out_dir / "images" / "sub" / "only.png").exists()

    (docs_root / "images" / "sub" / "only.png").unlink()
    (docs_root / "images" / "sub").rmdir()
    assert _run_main(docs_root, out_dir, config).returncode == 0
    assert not (out_dir / "images" / "sub").exists()


def test_main_refuses_when_output_dir_is_docs_root(tmp_path):
    """output_dir == docs_root makes topics_out_dir the very same directory
    as the source topics/ - the pruning rmtree used to delete it outright,
    with every already-globbed source file then failing to convert."""
    docs_root, config = _write_minimal_docs_root(tmp_path)
    result = _run_main(docs_root, docs_root, config)
    assert result.returncode == 1
    assert (docs_root / "topics" / "good.md").exists()


def test_main_refuses_before_writing_theme_json_into_the_source(tmp_path):
    """The output_dir-aliases-docs_root guard used to run only right
    before the topics_out_dir rmtree, well after theme.json (and the
    images/ copytree) had already written into the very directory the
    refusal exists to protect."""
    docs_root, config = _write_minimal_docs_root(tmp_path)
    result = _run_main(docs_root, docs_root, config)
    assert result.returncode == 1
    assert not (docs_root / "theme.json").exists()
