"""Regressions from the PR #21 review.

Each test here pins behaviour that was wrong and is now right. They are grouped by the review
comment they came from, because the failure each one guards against is silent: a swallowed table,
a blanked attribute, a deleted tooltip. Nothing downstream notices any of them.
"""

import json
import sqlite3
import sys
import tempfile
import xml.etree.ElementTree as ET
from pathlib import Path

import pytest

MODULE_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(MODULE_DIR))
sys.path.insert(0, str(MODULE_DIR.parent.parent.parent / "scripts" / "sync_kotlin_stdlib_docs"))

import build_nav  # noqa: E402
import md_to_json  # noqa: E402
from sync_kdoc_json_to_db import cleanup_orphaned_tooltips  # noqa: E402


def convert(source: str) -> list:
    """The blocks one markdown source converts to."""
    converter = md_to_json.Converter(md_to_json.make_markdown_it(), {}, {}, {})
    with tempfile.TemporaryDirectory() as work:
        path = Path(work, "page.md")
        path.write_text(source, encoding="utf-8")
        return converter.convert_file(path, "k/html/page", "page.md")["blocks"]


class TestContainerTags:
    """`tab` had no word boundary, so it matched the first three letters of `<table>`."""

    @pytest.mark.parametrize("markup", ["<table>", "</table>", '<table class="x">', "<tablet>"])
    def test_a_table_is_not_a_tab(self, markup):
        assert md_to_json.TAG_RE.match(markup) is None

    @pytest.mark.parametrize("markup", ["<tab>", "</tab>", "<tabs>", '<tab title="Gradle">'])
    def test_the_real_containers_still_match(self, markup):
        assert md_to_json.TAG_RE.match(markup) is not None

    def test_a_raw_html_table_survives_conversion(self):
        # The docstring of md_to_json notes pages like roadmap.md are "basically hand-written HTML
        # tables". They used to come out as a `tab` block holding bare <tr>/<td>, which browsers
        # discard, losing the table entirely.
        blocks = convert("<table>\n<tr><td>Kotlin 2.0</td><td>Supported</td></tr>\n</table>\n")
        assert [b["type"] for b in blocks] == ["html"]
        assert "<table>" in blocks[0]["html"] and "</table>" in blocks[0]["html"]

    def test_a_table_and_a_tabs_block_side_by_side(self):
        blocks = convert(
            "<table>\n<tr><td>cell</td></tr>\n</table>\n\n"
            "<tabs>\n<tab title=\"Gradle\">\n\ngradle text\n\n</tab>\n</tabs>\n"
        )
        kinds = [b["type"] for b in blocks]
        assert "html" in kinds, "the table stays raw HTML"
        assert "tabs" in kinds, "the tabs container is still recognised"


class TestSelfClosingTags:
    """A greedy attribute group ate the trailing slash, so `<tab/>` opened a container."""

    def test_a_self_closing_tag_does_not_swallow_the_rest_of_the_page(self):
        blocks = convert("<tab/>\n\nAfter the self-closing tag\n")
        assert [b["type"] for b in blocks] == ["tab", "paragraph"]
        assert blocks[0]["blocks"] == [], "the self-closing container has no children"
        assert "After the self-closing tag" in blocks[1]["html"]

    def test_an_ordinary_container_still_takes_children(self):
        blocks = convert("<note>\n\nInside the note\n\n</note>\n")
        assert [b["type"] for b in blocks] == ["note"]
        assert "Inside the note" in blocks[0]["blocks"][0]["html"]


class TestParseAttrs:
    """The quoted-vs-bare test was made against the whole string, not each pair."""

    def test_a_bare_value_survives_beside_a_quoted_one(self):
        # A <tabs group=build-script title="Gradle"> that lost its group silently stopped
        # tabs.js's cross-block tab syncing.
        assert md_to_json.parse_attrs(' title="Gradle" group-key=gradle') == {
            "title": "Gradle", "group-key": "gradle"}

    def test_bare_values_alone_still_work(self):
        assert md_to_json.parse_attrs(" x=1 y=2") == {"x": "1", "y": "2"}

    def test_an_explicitly_empty_value_is_not_a_bare_one(self):
        assert md_to_json.parse_attrs(' x="" y=2') == {"x": "", "y": "2"}


class TestLinkColouring:
    """A second `style` attribute was emitted; HTML parsers keep the first and drop it."""

    def colourer(self):
        return md_to_json.Converter(md_to_json.make_markdown_it(), {}, {}, {},
                                    broken_ext_link_color="#cc0000")

    def test_the_colour_merges_into_an_existing_style(self):
        out = self.colourer().style_broken_and_external_links(
            '<a href="https://example.com" style="font-weight:bold">ext</a>')
        assert out.count("style=") == 1
        assert "font-weight:bold;color: #cc0000;" in out

    def test_a_link_with_no_style_gets_one(self):
        out = self.colourer().style_broken_and_external_links('<a href="https://example.com">ext</a>')
        assert out.count("style=") == 1 and "color: #cc0000;" in out

    def test_an_internal_link_is_left_alone(self):
        markup = '<a href="/k/html/enum-classes.html">internal</a>'
        assert self.colourer().style_broken_and_external_links(markup) == markup


class TestNavForUnconvertedTopics:
    """A `.topic` with no page behind it was given a guessed id, so it linked to a 404."""

    def build(self, stem_to_id):
        tree = ET.fromstring('<x><toc-element topic="home.topic"/>'
                             '<toc-element topic="api-references.topic"/>'
                             '<toc-element topic="real.md"/></x>')
        warnings = []
        nodes = [build_nav.build_node(el, stem_to_id, {}, warnings, "#999", id_prefix="k/html/")
                 for el in tree.findall("toc-element")]
        return {n["title"]: n for n in nodes}, warnings

    def test_an_unconverted_topic_gets_no_id(self):
        nodes, warnings = self.build({"home": "k/html/home", "real": "k/html/real"})
        # No id means nav.peb renders a header rather than a link, and populate_db's
        # flatten_nav_ids leaves it out of the prev/next chain.
        assert nodes["Api References"]["id"] is None
        assert any("api-references" in w for w in warnings)

    def test_home_still_links_because_populate_db_gives_it_a_page(self):
        nodes, _ = self.build({"home": "k/html/home", "real": "k/html/real"})
        assert nodes["Home"]["id"] == "k/html/home"

    def test_a_converted_page_is_unaffected(self):
        nodes, _ = self.build({"home": "k/html/home", "real": "k/html/real"})
        assert nodes["Real"]["id"] == "k/html/real"


class TestNavIgnoresItsOwnOutput:
    """`build_nav.py <docs-root> <out> <out>` is the documented invocation, so run 2 read its
    own nav.json as if it were a page."""

    def test_the_nav_output_is_skipped(self, tmp_path):
        (tmp_path / "page.json").write_text(json.dumps({"id": "k/html/x", "title": "X"}))
        (tmp_path / "nav.json").write_text(json.dumps([{"title": "A", "id": "k/html/x"}]))
        stem_to_id, _ = build_nav.load_page_index(tmp_path, skip={tmp_path / "nav.json"})
        assert stem_to_id == {"x": "k/html/x"}

    def test_genuinely_malformed_input_still_raises(self, tmp_path):
        # Skipping by path rather than by shape: a JSON file that is not a page and not our own
        # output is a real problem and should not be swallowed.
        (tmp_path / "broken.json").write_text(json.dumps(["not", "a", "page"]))
        with pytest.raises(AttributeError):
            build_nav.load_page_index(tmp_path)


class TestTooltipCleanup:
    """One dead button used to delete the whole tooltip, including hand-authored text that no
    part of the pipeline can regenerate."""

    DEAD = "k/kotlin-stdlib/kotlin.text/old-name/index.html"

    def fixture(self):
        conn = sqlite3.connect(":memory:")
        conn.executescript("""
            CREATE TABLE Tooltips (id INTEGER PRIMARY KEY, categoryId INT, tag TEXT,
                                   summary TEXT, detail TEXT);
            CREATE TABLE TooltipButtons (tooltipId INT, buttonNumberId INT, description TEXT,
                                         uri TEXT);
            INSERT INTO Tooltips VALUES (1, 1, 'listOf', 'Hand-written', 'Hand-written detail'),
                                        (2, 1, 'oldName', 'Other', 'Other detail');
            INSERT INTO TooltipButtons VALUES
              (1, 1, 'gone', 'k/kotlin-stdlib/kotlin.text/old-name/index.html'),
              (1, 2, 'alive', 'k/kotlin-stdlib/kotlin.collections/list-of.html'),
              (2, 1, 'gone', 'k/kotlin-stdlib/kotlin.text/old-name/index.html');
        """)
        return conn

    def test_a_tooltip_with_a_surviving_button_keeps_its_text(self):
        conn = self.fixture()
        cur = conn.cursor()
        tooltips, _buttons = cleanup_orphaned_tooltips(cur, [self.DEAD], dry_run=False)
        assert tooltips == 0
        assert cur.execute("SELECT summary FROM Tooltips WHERE id = 1").fetchone() == ("Hand-written",)
        # Its dead button goes; the working one stays.
        assert cur.execute("SELECT count(*) FROM TooltipButtons WHERE tooltipId = 1").fetchone() == (1,)

    def test_a_fully_orphaned_tooltip_is_kept_unless_asked(self):
        conn = self.fixture()
        cur = conn.cursor()
        cleanup_orphaned_tooltips(cur, [self.DEAD], dry_run=False)
        assert cur.execute("SELECT count(*) FROM Tooltips WHERE id = 2").fetchone() == (1,)

    def test_prune_tooltips_removes_only_the_fully_orphaned_one(self):
        conn = self.fixture()
        cur = conn.cursor()
        tooltips, _ = cleanup_orphaned_tooltips(cur, [self.DEAD], dry_run=False, prune_tooltips=True)
        assert tooltips == 1
        assert [r[0] for r in cur.execute("SELECT id FROM Tooltips ORDER BY id")] == [1]

    def test_a_dry_run_changes_nothing(self):
        conn = self.fixture()
        cur = conn.cursor()
        cleanup_orphaned_tooltips(cur, [self.DEAD], dry_run=True, prune_tooltips=True)
        assert cur.execute("SELECT count(*) FROM Tooltips").fetchone() == (2,)
        assert cur.execute("SELECT count(*) FROM TooltipButtons").fetchone() == (3,)
