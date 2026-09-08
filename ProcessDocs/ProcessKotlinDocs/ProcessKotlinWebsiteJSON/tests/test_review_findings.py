"""Regression tests for the PR #24 review findings (F01-F15).

Each test constructs the specific input the reviewer identified as untested -
the shapes nothing else in the suite feeds these functions. Every one of them
fails against the code as it stood before the corresponding fix, which is the
only reason they are worth having: the two criticals in particular were silent,
exit-0 data loss that truthful-looking statistics actively concealed.
"""
import json
import sqlite3
import sys

import brotli
import pytest
from PIL import Image, ImageDraw

import optimize_media as om
from build_nav import load_page_index
from insert_optimized_media import reassemble_content
from migrate_content_to_dictionary_brotli import is_chunked_base, load_base_rows, write_item
from populate_db import CHUNK_SIZE
from renumber_misnumbered_fragments import find_chains, find_fragment_paths

SCHEMA_SQL = """
CREATE TABLE Languages (id INTEGER PRIMARY KEY AUTOINCREMENT, value TEXT NOT NULL UNIQUE);
CREATE TABLE ContentTypes (id INTEGER PRIMARY KEY AUTOINCREMENT, value TEXT NOT NULL UNIQUE,
                            compression TEXT NOT NULL);
CREATE TABLE Content (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    path TEXT NOT NULL,
    languageID INTEGER NOT NULL,
    content BLOB NOT NULL,
    contentTypeID INTEGER NOT NULL,
    templateId INTEGER,
    UNIQUE(path)
);
"""


def _new_stats():
    return {"raster": 0, "svg": 0, "svg_rasterized": 0, "copied": 0, "errors": 0,
            "original_bytes": 0, "optimized_bytes": 0}


@pytest.fixture
def conn():
    connection = sqlite3.connect(":memory:")
    connection.executescript(SCHEMA_SQL)
    connection.execute("INSERT INTO Languages (value) VALUES ('en-US')")
    connection.execute("INSERT INTO ContentTypes (value, compression) VALUES ('text/html', 'brotli')")
    yield connection
    connection.close()


def _insert(conn, path, blob):
    conn.execute(
        "INSERT INTO Content (path, languageID, content, contentTypeID, templateId) VALUES (?, 1, ?, 1, 0)",
        (path, blob),
    )


# --- F01: two sources colliding on a rewritten extension ---------------------

def test_sources_differing_only_by_extension_both_survive(tmp_path):
    """logo.png + logo.jpg both become logo.webp, and the loser used to be
    silently gone - with errors 0 and both pages repointed at the survivor."""
    src, out = tmp_path / "in", tmp_path / "out"
    src.mkdir(), out.mkdir()
    Image.new("RGB", (40, 30), (200, 30, 30)).save(src / "logo.png")
    Image.new("RGB", (40, 30), (30, 30, 200)).save(src / "logo.jpg")

    cfg = dict(om.BUILTIN_DEFAULTS) | {"webp": True}
    renamed = om.optimize_directory(src, out, cfg=cfg, pngquant_path=om.find_pngquant(),
                                     logger=om.Logger(sys.stdout), stats=_new_stats()).renamed

    assert len(list(out.iterdir())) == 2, "one source was clobbered by the other"
    # Both renames are reported, so a caller rewriting stored URLs follows them.
    assert set(renamed) == {"logo.png", "logo.jpg"}
    assert len(set(renamed.values())) == 2, "both sources still map to one output"


def test_three_way_extension_collision_all_survive(tmp_path):
    src, out = tmp_path / "in", tmp_path / "out"
    src.mkdir(), out.mkdir()
    for name, colour in (("logo.png", (1, 1, 1)), ("logo.jpg", (2, 2, 2)), ("logo.gif", (3, 3, 3))):
        Image.new("RGB", (20, 20), colour).save(src / name)

    cfg = dict(om.BUILTIN_DEFAULTS) | {"webp": True}
    om.optimize_directory(src, out, cfg=cfg, pngquant_path=om.find_pngquant(),
                           logger=om.Logger(sys.stdout), stats=_new_stats())

    assert len(list(out.iterdir())) == 3


def test_non_colliding_names_keep_their_own_stems(tmp_path):
    """The de-confliction must not rename anything that didn't collide."""
    src, out = tmp_path / "in", tmp_path / "out"
    src.mkdir(), out.mkdir()
    Image.new("RGB", (20, 20), (5, 5, 5)).save(src / "alpha.png")
    Image.new("RGB", (20, 20), (6, 6, 6)).save(src / "beta.png")

    om.optimize_directory(src, out, cfg=dict(om.BUILTIN_DEFAULTS), pngquant_path=om.find_pngquant(),
                           logger=om.Logger(sys.stdout), stats=_new_stats())

    assert sorted(p.name for p in out.iterdir()) == ["alpha.png", "beta.png"]


# --- F07: optimizing a directory into itself ---------------------------------

def test_optimizing_into_the_input_directory_is_refused(tmp_path):
    """Used to destroy the originals in place; the only error raised was a
    copy2 SameFileError on the first non-image file, long after the damage."""
    Image.new("RGB", (400, 300), (10, 200, 10)).save(tmp_path / "logo.png")
    before = (tmp_path / "logo.png").read_bytes()

    with pytest.raises(ValueError, match="input directory"):
        om.optimize_directory(tmp_path, tmp_path, cfg=dict(om.BUILTIN_DEFAULTS),
                               pngquant_path=om.find_pngquant(), logger=om.Logger(sys.stdout),
                               stats=_new_stats())

    assert (tmp_path / "logo.png").read_bytes() == before


# --- F02: an unrelated "X-1" page alongside "X" -------------------------------

def test_independent_page_named_like_a_fragment_is_not_a_continuation(conn):
    _insert(conn, "k/html/guide.html", brotli.compress(b"the base page, comfortably under one chunk"))
    _insert(conn, "k/html/guide.html-1", brotli.compress(b"a wholly unrelated page"))

    scanned = {row[0] for row in load_base_rows(conn)}
    assert "k/html/guide.html-1" in scanned, "victim was invisible to the migration entirely"


def test_write_item_does_not_delete_an_unrelated_lookalike_page(conn):
    victim = brotli.compress(b"a wholly unrelated page")
    _insert(conn, "k/html/guide.html", brotli.compress(b"the base page"))
    _insert(conn, "k/html/guide.html-1", victim)

    write_item(conn, "k/html/guide.html", 1, 1, 0, brotli.compress(b"rewritten base page"))

    row = conn.execute("SELECT content FROM Content WHERE path = 'k/html/guide.html-1'").fetchone()
    assert row is not None, "unrelated page deleted as a surplus fragment"
    assert row[0] == victim, "unrelated page overwritten"


def test_genuinely_chunked_base_still_owns_its_continuations(conn):
    """The length gate must not break real chunking - including an ADFA-5171
    chain numbered from -2, which has no -1 at all."""
    _insert(conn, "k/html/big.html", b"x" * CHUNK_SIZE)
    _insert(conn, "k/html/big.html-2", b"y" * 10)

    scanned = {row[0] for row in load_base_rows(conn)}
    assert "k/html/big.html-2" not in scanned, "real continuation treated as its own page"
    assert is_chunked_base({"k/html/big.html": CHUNK_SIZE}, "k/html/big.html")
    assert not is_chunked_base({"k/html/guide.html": 42}, "k/html/guide.html")


# --- F06: reassembly of a chain numbered from -2 ------------------------------

def test_reassemble_content_handles_a_chain_numbered_from_two(conn):
    """Probing "<path>-1" first returned a truncated stream for the exact shape
    renumber_misnumbered_fragments.py exists to repair."""
    tail = b"z" * 20
    _insert(conn, "k/html/images/big.png", b"a" * CHUNK_SIZE)
    _insert(conn, "k/html/images/big.png-2", tail)

    assembled = reassemble_content(conn, "k/html/images/big.png", b"a" * CHUNK_SIZE)
    assert assembled == b"a" * CHUNK_SIZE + tail


# --- F08: a chain with an interior gap ---------------------------------------

def test_chain_with_interior_gap_is_reported_as_gapped(conn):
    """p-1, p-2, p-4 starts at 1, so it used to short-circuit as healthy and be
    reported as "0 chain(s) had a real gap" on a truncated page."""
    _insert(conn, "k/html/page.html", b"a" * CHUNK_SIZE)
    for n in (1, 2, 4):
        _insert(conn, f"k/html/page.html-{n}", b"a" * (CHUNK_SIZE if n != 4 else 10))

    misnumbered, gapped = find_chains(conn, find_fragment_paths(conn))

    assert [path for path, _f in gapped] == ["k/html/page.html"]
    assert misnumbered == []


def test_contiguous_chain_from_one_is_left_alone(conn):
    _insert(conn, "k/html/page.html", b"a" * CHUNK_SIZE)
    _insert(conn, "k/html/page.html-1", b"a" * 10)

    misnumbered, gapped = find_chains(conn, find_fragment_paths(conn))
    assert misnumbered == [] and gapped == []


# --- F03: build_nav reading its own nav.json ---------------------------------

def test_load_page_index_skips_the_generated_nav_json(tmp_path):
    """The documented invocation passes output_dir as the scan dir, so a second
    run read its own nav.json - a top-level array - and died on list.get."""
    (tmp_path / "page.json").write_text(json.dumps({"id": "k/html/a", "title": "A"}), encoding="utf-8")
    (tmp_path / "nav.json").write_text(json.dumps([{"id": "k/html/a", "children": []}]), encoding="utf-8")

    stem_to_id, id_to_title = load_page_index(tmp_path)

    assert stem_to_id == {"a": "k/html/a"}
    assert id_to_title == {"k/html/a": "A"}


# =============================================================================
# Second review round (F01-F15). Several of these are regressions introduced by
# the fixes above - the chunking protocol in particular was re-derived at four
# call sites, each wrong differently, which is why it now lives in one module.
# =============================================================================

from content_chunking import owned_fragment_paths, served_fragment_paths  # noqa: E402


# --- F13: a short fragment terminates the chain, as WebServer.kt does --------

def test_reassembly_stops_at_the_short_fragment(conn):
    """A gapped chain (p-1 full, p-2 short, p-4 orphaned) is served as
    p + p-1 + p-2. Concatenating the whole discovered chain instead produces
    bytes the server never had - which rewrite_pages would re-compress and
    store."""
    full = b"a" * CHUNK_SIZE
    _insert(conn, "p", full)
    _insert(conn, "p-1", full)
    _insert(conn, "p-2", b"short")
    _insert(conn, "p-4", b"orphaned tail")

    assert served_fragment_paths(conn, "p") == ["p-1", "p-2"]
    assert reassemble_content(conn, "p", full) == full + full + b"short"


def test_ownership_still_includes_the_orphaned_tail(conn):
    """Deleting or replacing a base must take everything named after it, or
    the tail is orphaned - a different question from what gets served."""
    full = b"a" * CHUNK_SIZE
    _insert(conn, "p", full)
    _insert(conn, "p-1", b"short")
    _insert(conn, "p-4", b"orphaned tail")

    assert owned_fragment_paths(conn, "p") == ["p-1", "p-4"]


# --- F08: a real chain hidden behind an unrelated lookalike base -------------

def test_misnumbered_chain_behind_a_lookalike_base_is_found(conn):
    """An ordinary page at "guide.html" must not make a genuinely chunked,
    misnumbered page at "guide.html-1" look like its fragment - that hid the
    chain from the tool written to repair it, reporting a clean run."""
    _insert(conn, "k/html/guide.html", b"an ordinary small page")
    _insert(conn, "k/html/guide.html-1", b"a" * CHUNK_SIZE)
    _insert(conn, "k/html/guide.html-1-2", b"tail")

    fragments = find_fragment_paths(conn)
    misnumbered, _gapped = find_chains(conn, fragments)

    assert "k/html/guide.html-1" not in fragments
    assert [path for path, _f in misnumbered] == ["k/html/guide.html-1"]


# --- F04/F05: de-confliction keyed on the predicted output, flat namespace ---

def test_same_basename_in_different_directories_deconflicts(tmp_path):
    """insert_optimized_media addresses images by bare basename, so
    sub-a/logo.png and sub-b/logo.jpg both land on logo.webp under --webp
    even though they are in different source directories."""
    src, out = tmp_path / "in", tmp_path / "out"
    (src / "sub-a").mkdir(parents=True)
    (src / "sub-b").mkdir(parents=True)
    out.mkdir()
    Image.new("RGB", (20, 20), (200, 0, 0)).save(src / "sub-a" / "logo.png")
    Image.new("RGB", (20, 20), (0, 0, 200)).save(src / "sub-b" / "logo.jpg")

    om.optimize_directory(src, out, cfg=dict(om.BUILTIN_DEFAULTS) | {"webp": True},
                           pngquant_path=om.find_pngquant(), logger=om.Logger(sys.stdout),
                           stats=_new_stats())

    basenames = [p.name for p in out.rglob("*") if p.is_file()]
    assert len(basenames) == 2
    assert len(set(basenames)) == 2, "collide once flattened to bare basenames"


def test_no_rename_when_the_extension_cannot_change(tmp_path):
    """Without --webp neither encoder rewrites an extension, so logo.png and
    logo.jpg cannot collide - renaming anyway put a bogus entry in `renamed`,
    which rewrote every stored URL and churned the row for nothing."""
    src, out = tmp_path / "in", tmp_path / "out"
    src.mkdir(), out.mkdir()
    Image.new("RGB", (20, 20), (1, 1, 1)).save(src / "logo.png")
    Image.new("RGB", (20, 20), (2, 2, 2)).save(src / "logo.jpg")
    (src / "notes.txt").write_text("x")
    (src / "notes.md").write_text("y")

    renamed = om.optimize_directory(src, out, cfg=dict(om.BUILTIN_DEFAULTS),
                                     pngquant_path=om.find_pngquant(), logger=om.Logger(sys.stdout),
                                     stats=_new_stats()).renamed

    assert renamed == {}
    assert sorted(p.name for p in out.iterdir()) == ["logo.jpg", "logo.png", "notes.md", "notes.txt"]


# --- F10: a broken symlink is one file's error, not the run's ---------------

def test_broken_symlink_does_not_abort_the_run(tmp_path):
    """`not p.is_dir()` keeps dangling symlinks, and process_file used to stat
    outside its try - so one of them escaped as an unhandled FileNotFoundError
    past insert_optimized_media's ValueError-only catch."""
    src, out = tmp_path / "in", tmp_path / "out"
    src.mkdir(), out.mkdir()
    Image.new("RGB", (20, 20), (9, 9, 9)).save(src / "real.png")
    (src / "dangling.png").symlink_to(src / "nonexistent.png")

    stats = _new_stats()
    om.optimize_directory(src, out, cfg=dict(om.BUILTIN_DEFAULTS), pngquant_path=om.find_pngquant(),
                           logger=om.Logger(sys.stdout), stats=stats)

    assert stats["errors"] == 1
    assert sorted(p.name for p in out.iterdir()) == ["real.png"]


# =============================================================================
# Third review round. Both of these are defects in the fixes above: the shared
# chunking module implemented half of its own stated terminator rule, and the
# dangling-nav-link fix was applied to the database path but not the static one.
# =============================================================================

from build_nav import build_node, drop_unreachable_ids, render_node  # noqa: E402


# --- reassembly must stop at a gap, not just at a short fragment -------------

def test_reassembly_stops_at_a_gap_in_the_numbering(conn):
    """The server probes consecutive suffixes and stops at the first miss.
    Jumping the hole appends a tail it never reaches, which insert_optimized_media
    would then store back or die decompressing."""
    full = b"a" * CHUNK_SIZE
    _insert(conn, "p", full)
    _insert(conn, "p-1", full)
    _insert(conn, "p-2", full)
    _insert(conn, "p-4", b"tail the server never reaches")

    assert served_fragment_paths(conn, "p") == ["p-1", "p-2"]
    assert reassemble_content(conn, "p", full) == full + full + full
    # ownership is unchanged: a delete still has to take the orphaned tail
    assert owned_fragment_paths(conn, "p") == ["p-1", "p-2", "p-4"]


def test_adfa_5171_chain_from_two_is_still_read_whole(conn):
    """Contiguity is enforced from wherever the chain starts, not from 1 - the
    repair and migration tooling has to be able to read a -2 chain whole."""
    full = b"a" * CHUNK_SIZE
    _insert(conn, "p", full)
    _insert(conn, "p-2", full)
    _insert(conn, "p-3", b"end")

    assert served_fragment_paths(conn, "p") == ["p-2", "p-3"]


def test_gap_inside_a_misnumbered_chain_still_terminates(conn):
    full = b"a" * CHUNK_SIZE
    _insert(conn, "p", full)
    _insert(conn, "p-2", full)
    _insert(conn, "p-5", b"tail")

    assert served_fragment_paths(conn, "p") == ["p-2"]


# --- the static nav path must agree with the database one -------------------

def test_render_node_emits_a_group_title_when_the_page_is_missing():
    """nav.peb and render_node both branch on the id, so a synthesized id for
    an unconverted *.topic renders as a live <a href> to a URL that 404s."""
    node = {"title": "Overview", "id": None, "hidden": False,
            "noLinkColor": "#999999", "children": []}
    html = render_node(node)

    assert 'class="nav-group-title"' in html
    assert "<a " not in html


def test_unconverted_topic_node_is_stripped_of_its_dangling_id():
    """End to end over the pass both renderers depend on: build_node hands back
    a synthesized id for an unconverted *.topic, and nothing generates a page
    there, so it has to be cleared or the sidebar links to a 404."""
    import xml.etree.ElementTree as ET

    tree = ET.fromstring(
        '<toc-element toc-title="API reference">'
        '<toc-element topic="api-references.topic" toc-title="Overview"/>'
        '<toc-element topic="real.md" toc-title="Real"/>'
        '</toc-element>'
    )
    warnings = []
    nav = [build_node(tree, {"real": "k/html/real"}, {}, warnings, "#999999", id_prefix="k/html/")]

    cleared = drop_unreachable_ids(nav, {"k/html/real"})

    assert cleared == ["k/html/api-references"]
    overview, real = nav[0]["children"]
    assert overview["id"] is None and "<a " not in render_node(overview)
    assert real["id"] == "k/html/real" and 'class="nav-link"' in render_node(real)


def test_build_node_synthesizes_an_id_for_an_unconverted_topic():
    """Pins the precondition the fix depends on: build_node does hand back an
    id here, so callers must clear it against the set of real pages."""
    import xml.etree.ElementTree as ET

    el = ET.fromstring('<toc-element topic="api-references.topic" toc-title="Overview"/>')
    warnings = []
    node = build_node(el, {}, {}, warnings, "#999999", id_prefix="k/html/")

    assert node["id"] == "k/html/api-references"
    assert node["noLinkColor"] == "#999999"


# =============================================================================
# Fourth review round (F26-F34).
# =============================================================================
import shutil  # noqa: E402
import subprocess  # noqa: E402
from pathlib import Path  # noqa: E402

import insert_optimized_media as iom  # noqa: E402
from insert_optimized_media import (  # noqa: E402
    delete_unreferenced_media,
    list_stored_media,
    rewrite_pages,
)
from find_missing_assets import INCLUDE_RE, outside_fences  # noqa: E402
from md_to_json import Converter, build_tree, make_markdown_it  # noqa: E402
from populate_db import DictionaryCompressor, pages_linking_to  # noqa: E402

needs_brotli_cli = pytest.mark.skipif(shutil.which("brotli") is None, reason="brotli CLI not installed")


def _animated_gif(path, frames=3, size=(40, 40), loop=None):
    """An animated GIF, with a NETSCAPE loop block only if `loop` is given -
    Pillow signals "plays once" by omitting the key entirely.

    Each frame draws a rectangle in a different place: frames that are
    byte-identical get collapsed on save, which quietly produces a
    single-frame "animation" that proves nothing."""
    images = []
    for i in range(frames):
        frame = Image.new("RGB", size, (0, 0, 0))
        ImageDraw.Draw(frame).rectangle([i * 5, i * 5, i * 5 + 10, i * 5 + 10], fill=(255, i * 80, 0))
        images.append(frame.convert("P"))
    kwargs = {"save_all": True, "append_images": images[1:], "duration": 80}
    if loop is not None:
        kwargs["loop"] = loop
    images[0].save(path, **kwargs)
    return path


# --- F26: an animated GIF must not be planned as, or replaced by, a .webp ----

def test_animated_gif_survives_a_webp_run(tmp_path):
    """--webp cannot re-encode an animated GIF (optimize_raster resizes it as
    a GIF instead), so predicting a ".webp" output for it left the animation
    stored under a name nothing referenced - and rewrite_pages repointed every
    page at a .webp that was never written."""
    src, out = tmp_path / "in", tmp_path / "out"
    src.mkdir(), out.mkdir()
    _animated_gif(src / "spin.gif")

    result = om.optimize_directory(src, out, cfg=dict(om.BUILTIN_DEFAULTS) | {"webp": True},
                                    pngquant_path=om.find_pngquant(), logger=om.Logger(sys.stdout),
                                    stats=_new_stats())

    assert result.renamed == {}, "nothing was renamed, so no page URL should be rewritten"
    assert [p.name for p in out.iterdir()] == ["spin.gif"]
    with Image.open(out / "spin.gif") as written:
        assert getattr(written, "n_frames", 1) == 3


def test_still_gif_is_still_converted_to_webp(tmp_path):
    """The animated-GIF exemption is exactly that: a single-frame GIF keeps
    converting, so the fix doesn't quietly opt every GIF out of --webp."""
    src, out = tmp_path / "in", tmp_path / "out"
    src.mkdir(), out.mkdir()
    Image.new("P", (40, 40), 7).save(src / "static.gif")

    result = om.optimize_directory(src, out, cfg=dict(om.BUILTIN_DEFAULTS) | {"webp": True},
                                    pngquant_path=om.find_pngquant(), logger=om.Logger(sys.stdout),
                                    stats=_new_stats())

    assert [p.name for p in out.iterdir()] == ["static.webp"]
    assert result.renamed == {"static.gif": "static.webp"}


def test_duplicate_basenames_keep_the_first_without_repointing_it(tmp_path):
    """Two sources with the *same* basename flatten onto one stored image.
    De-conflicting them (a/logo.png -> logo.png, b/logo.png -> logo-2.png)
    invented a rename for a file that was never written under the new name;
    keeping the first and skipping the rest matches what the insert loop
    downstream does with a duplicate."""
    src, out = tmp_path / "in", tmp_path / "out"
    (src / "a").mkdir(parents=True)
    (src / "b").mkdir(parents=True)
    out.mkdir()
    Image.new("RGB", (20, 20), (9, 9, 9)).save(src / "a" / "logo.png")
    Image.new("RGB", (20, 20), (8, 8, 8)).save(src / "b" / "logo.png")

    result = om.optimize_directory(src, out, cfg=dict(om.BUILTIN_DEFAULTS),
                                    pngquant_path=om.find_pngquant(), logger=om.Logger(sys.stdout),
                                    stats=_new_stats())

    written = [p for p in out.rglob("*") if p.is_file()]
    assert [p.name for p in written] == ["logo.png"]
    assert result.renamed == {}, "the skipped duplicate must not repoint the surviving name"


# --- F27: the insert loop follows what was written, not what is lying about --

def test_written_lists_only_this_run_s_outputs(tmp_path):
    """A work directory is a documented positional, so it can hold files from
    an earlier run whose sources are since gone. Those must not be reported as
    written - the insert loop reads this list, and an rglob would resurrect
    them."""
    src, out = tmp_path / "in", tmp_path / "out"
    src.mkdir(), out.mkdir()
    Image.new("RGB", (20, 20), (1, 2, 3)).save(src / "current.png")
    (out / "deleted-last-week.png").write_bytes(b"stale")

    result = om.optimize_directory(src, out, cfg=dict(om.BUILTIN_DEFAULTS),
                                    pngquant_path=om.find_pngquant(), logger=om.Logger(sys.stdout),
                                    stats=_new_stats())

    assert [p.name for p in result.written] == ["current.png"]
    assert (out / "deleted-last-week.png").exists(), "left alone on disk, just not re-inserted"


# --- F31: a play-once GIF must not come back looping forever -----------------

def test_resize_preserves_a_play_once_gif(tmp_path):
    """info.get("loop", 0) read "plays once" (key absent) and wrote "loops
    forever" (0), adding a NETSCAPE block the source never had."""
    src = _animated_gif(tmp_path / "once.gif", size=(300, 300))
    with Image.open(src) as img:
        assert "loop" not in img.info
        resized = om.resize_animated_gif(img, tmp_path / "out.gif", max_width=100)
    with Image.open(resized) as written:
        assert "loop" not in written.info
        assert written.n_frames == 3


def test_resize_preserves_an_explicit_loop_count(tmp_path):
    src = _animated_gif(tmp_path / "thrice.gif", size=(300, 300), loop=3)
    with Image.open(src) as img:
        resized = om.resize_animated_gif(img, tmp_path / "out.gif", max_width=100)
    with Image.open(resized) as written:
        assert written.info.get("loop") == 3


# --- F29: pages converted before a failure still link to the failed page -----

def test_pages_linking_to_finds_the_pre_failure_pages():
    """Only pages converted before the failed stem was dropped carry the
    resolved href; ones converted after keep the raw "<stem>.md" and are
    already styled broken, so they must not be re-converted."""
    pages = [
        {"id": "before", "blocks": [{"type": "paragraph", "html": '<a href="/k/html/gone.html">x</a>'}]},
        {"id": "after", "blocks": [{"type": "paragraph", "html": '<a href="gone.md" style="color:red">x</a>'}]},
        {"id": "nested", "blocks": [{"type": "blockquote", "blocks": [
            {"type": "paragraph", "html": '<a href="/k/html/gone.html#anchor">x</a>'}]}]},
        {"id": "unrelated", "blocks": [{"type": "paragraph", "html": '<a href="/k/html/fine.html">x</a>'}]},
    ]

    assert pages_linking_to(pages, ["gone"]) == [0, 2]
    assert pages_linking_to(pages, []) == []
    assert pages_linking_to(pages, ["never-existed"]) == []


# --- F28: page.peb depends on images never being a block type of their own ---

def test_markdown_images_stay_inline_never_a_block():
    """templates/page.peb has no "image" branch, and IMAGE_REF_RE / rewrite_pages
    are anchored on src="..." - all three rest on this.

    Goes through convert_nodes rather than convert_file because convert_file
    wants a path on disk, and the block schema is what's under test."""
    converter = Converter(make_markdown_it(), {}, {}, {"a.png": "a.png"}, image_url_prefix="/k/html/images/")
    tokens = converter.md.parse("Text with ![alt](a.png) inline.\n\n![alt](a.png)\n")
    blocks = converter.convert_nodes(build_tree(tokens))

    assert all(b["type"] != "image" for b in blocks), "a standalone image block would render as nothing"
    assert any('src="/k/html/images/a.png"' in b.get("html", "") for b in blocks)


# --- F30/F34: reading pages and media back exactly once ----------------------

PAGE_TYPE_ID = 1  # the conn fixture's only ContentType
DICTIONARY = bytes(range(256)) * 64


@pytest.fixture
def compressor():
    instance = DictionaryCompressor(DICTIONARY)
    yield instance
    instance.close()


def _page_blob(compressor, *image_names):
    """A page row's stored bytes: the JSON md_to_json would produce, with each
    image referenced the only way it ever is - as an HTML src attribute."""
    blocks = [{"type": "paragraph", "html": f'<img src="{iom.IMAGES_URL_PREFIX}{name}" alt="">'}
              for name in image_names]
    text = json.dumps({"id": "p", "blocks": blocks})
    return compressor.compress(text.encode("utf-8"))


def _add(conn, path, blob=b"x", template_id=0):
    conn.execute(
        "INSERT INTO Content (path, languageID, content, contentTypeID, templateId) VALUES (?, 1, ?, ?, ?)",
        (path, blob, PAGE_TYPE_ID, template_id),
    )


@needs_brotli_cli
def test_pages_are_decompressed_once_per_run(conn, compressor):
    """rewrite_pages and collect_referenced_media selected the same rows and
    decompressed each of them independently. Every decompression is a `brotli`
    subprocess (the shared-dictionary path has no Python binding), so the
    second pass cost one process spawn per page - 268 of them, 2.3s, on the
    real corpus, for a set of references the first pass had already seen."""
    # A real rename, so the old code took both passes: four decompressions to
    # rewrite, then four more to collect what the rewritten pages reference.
    for i in range(4):
        _add(conn, f"k/html/page{i}.html", _page_blob(compressor, "logo.png"), template_id=2)
    _add(conn, "k/html/images/logo.webp")
    _add(conn, "k/html/images/orphan.png")

    calls = []
    real_decompress = compressor.decompress
    compressor.decompress = lambda data: (calls.append(1), real_decompress(data))[1]

    rewritten = rewrite_pages(conn, {"logo.png": "logo.webp"}, 1, PAGE_TYPE_ID, om.Logger(None), [], compressor)
    removed = delete_unreferenced_media(conn, PAGE_TYPE_ID, om.Logger(None), compressor,
                                        referenced=rewritten.referenced)

    assert rewritten.changed == 4
    assert rewritten.referenced == {"logo.webp"}, "collected post-substitution, as the pages now read"
    assert removed == 1, "orphan.png is referenced by nothing"
    assert len(calls) == 4, f"one decompression per page row, got {len(calls)}"


@needs_brotli_cli
def test_an_empty_rename_map_rewrites_nothing(conn, compressor):
    """Dropping rewrite_pages' "nothing to rename, return now" shortcut (so its
    single pass can also collect references) must not turn an empty rename_map
    into a regex that matches everywhere: re.compile("") matches at every
    position, which would have rewritten every page to no effect."""
    _add(conn, "k/html/page.html", _page_blob(compressor, "a.png"), template_id=2)
    before = conn.execute("SELECT content FROM Content WHERE path = 'k/html/page.html'").fetchone()[0]

    rewritten = rewrite_pages(conn, {}, 1, PAGE_TYPE_ID, om.Logger(None), [], compressor)

    assert rewritten.changed == 0
    assert conn.execute("SELECT content FROM Content WHERE path = 'k/html/page.html'").fetchone()[0] == before


def test_an_image_named_like_a_fragment_is_still_listed(conn):
    """An image genuinely called "diagram.png-1", stored beside an ordinary
    (not CHUNK_SIZE) "diagram.png", read as that row's continuation and
    vanished from the listing - so delete_unreferenced_media could neither see
    it nor remove it. The base row's length is what settles it."""
    _add(conn, "k/html/images/diagram.png", b"small")
    _add(conn, "k/html/images/diagram.png-1", b"a separate file that just looks like a fragment")

    assert set(list_stored_media(conn)) == {"diagram.png", "diagram.png-1"}


def test_a_real_continuation_is_still_collapsed(conn):
    """The other direction: a genuinely chunked image is one entry, not two."""
    _add(conn, "k/html/images/big.png", b"x" * CHUNK_SIZE)
    _add(conn, "k/html/images/big.png-1", b"tail")

    assert set(list_stored_media(conn)) == {"big.png"}


# --- F32: no ~250MB backup for a run that has nothing to repair --------------

def _repair_db(tmp_path, rows):
    path = tmp_path / "documentation.db"
    conn = sqlite3.connect(path)
    conn.executescript(SCHEMA_SQL)
    conn.execute("INSERT INTO Languages (value) VALUES ('en-US')")
    conn.execute("INSERT INTO ContentTypes (value, compression) VALUES ('text/html', 'brotli')")
    for row_path, blob in rows:
        conn.execute(
            "INSERT INTO Content (path, languageID, content, contentTypeID, templateId) VALUES (?, 1, ?, 1, 0)",
            (row_path, blob),
        )
    conn.commit()
    conn.close()
    return path


def _run_renumber_main(monkeypatch, db_path):
    """Runs the script's main() against db_path, reporting whether it took a
    backup."""
    import renumber_misnumbered_fragments as rmf
    backups = []
    monkeypatch.setattr(rmf, "backup_database", lambda p: (backups.append(p), Path(f"{p}.bak"))[1])
    monkeypatch.setattr(sys, "argv", ["renumber_misnumbered_fragments.py", str(db_path)])
    rmf.main()
    return backups


def test_a_clean_database_is_not_backed_up(tmp_path, monkeypatch, capsys):
    """This script is documented as idempotent, so re-running it against
    production is the normal case and finds nothing to do. Backing up first
    wrote another full VACUUM INTO copy of a ~250MB file every single time."""
    db_path = _repair_db(tmp_path, [("k/html/page.html", b"small"),
                                    ("k/html/big.html", b"x" * CHUNK_SIZE),
                                    ("k/html/big.html-1", b"tail")])

    assert _run_renumber_main(monkeypatch, db_path) == []
    assert "Renumbered 0 chain(s)" in capsys.readouterr().out


def test_a_database_needing_repair_is_backed_up(tmp_path, monkeypatch):
    """The other direction: skipping the backup must not extend to the run
    that actually rewrites paths."""
    db_path = _repair_db(tmp_path, [("k/html/big.html", b"x" * CHUNK_SIZE),
                                    ("k/html/big.html-2", b"tail")])

    assert _run_renumber_main(monkeypatch, db_path) == [db_path]

    conn = sqlite3.connect(db_path)
    paths = {row[0] for row in conn.execute("SELECT path FROM Content")}
    conn.close()
    assert paths == {"k/html/big.html", "k/html/big.html-1"}


# --- F35: a flag given no value should say so, not die inside bash -----------

REPO_ROOT = Path(__file__).resolve().parents[4]


@pytest.mark.parametrize("script, flag", [
    ("run-build-kotlin-docs-with-act.sh", "--db-path"),
    ("Dokka-plugin-kdoc2json/scripts/kotlin/build-stdlib-json-docs.sh", "--kotlin-libs-version"),
])
def test_a_flag_without_a_value_is_reported(script, flag):
    """Both scripts read $2 unguarded under `set -u`, so a trailing flag exited
    with bash's own "$2: unbound variable" instead of the usage message that
    exists for exactly this mistake."""
    path = REPO_ROOT / script
    if not path.exists():  # pragma: no cover - only when run outside the repo
        pytest.skip(f"{script} not found")
    proc = subprocess.run(["bash", str(path), flag], capture_output=True, text=True)

    assert proc.returncode == 1
    combined = proc.stdout + proc.stderr
    assert f"{flag} needs a value" in combined
    assert "unbound variable" not in combined


# --- an <include> shown inside a code sample is not a broken reference -------

@pytest.mark.parametrize("source, expected", [
    ("before\n~~~\n<include from=\"sample.md\"/>\n~~~\nafter\n", []),
    ("x\n````\n<include from=\"a.md\"/>\n```\n<include from=\"b.md\"/>\n````\ny\n", []),
    ("text\n<include from=\"real.md\"/>\n", ["real.md"]),
    ("```\n<include from=\"sample.md\"/>\n```\n<include from=\"real.md\"/>\n", ["real.md"]),
])
def test_includes_inside_any_fence_are_not_scanned(source, expected):
    """The old pattern was "```.*?```": a ~~~-fenced sample was scanned as real
    source, and a longer ```` fence wrapping a ``` sample closed early and
    exposed the rest of the block. Both warned about files nobody meant to
    ship. md_to_json.fenced_spans - which extract_title already relies on -
    knows both fence characters and the "at least as long" close rule."""
    assert INCLUDE_RE.findall(outside_fences(source, "topics/x.md")) == expected


# =============================================================================
# Self-review of the fourth round's own fixes.
# =============================================================================

def _apng(path, frames=3, size=(40, 40)):
    """An animated PNG. Pillow reports is_animated for these exactly as it
    does for a GIF, and optimize_raster copies any animated non-GIF through
    unchanged - so the output keeps the .png."""
    images = []
    for i in range(frames):
        frame = Image.new("RGB", size, (0, 0, 0))
        ImageDraw.Draw(frame).rectangle([i * 5, i * 5, i * 5 + 10, i * 5 + 10], fill=(255, i * 80, 0))
        images.append(frame)
    images[0].save(path, save_all=True, append_images=images[1:], duration=80)
    return path


# --- the duplicate-basename skip must not swallow a case-differing pair -----

def test_names_differing_only_by_case_are_both_kept(tmp_path):
    """populate_db indexes image basenames exactly ("Logo.png" and "logo.png"
    are two addressable images, and a page may reference either), so skipping
    one here as a "duplicate" left that page's reference resolving to a row
    nothing inserts. They are a collision, which de-confliction handles, not a
    duplicate."""
    src, out = tmp_path / "in", tmp_path / "out"
    (src / "a").mkdir(parents=True)
    (src / "b").mkdir(parents=True)
    out.mkdir()
    Image.new("RGB", (20, 20), (9, 9, 9)).save(src / "a" / "Logo.png")
    Image.new("RGB", (20, 20), (8, 8, 8)).save(src / "b" / "logo.png")

    result = om.optimize_directory(src, out, cfg=dict(om.BUILTIN_DEFAULTS),
                                    pngquant_path=om.find_pngquant(), logger=om.Logger(sys.stdout),
                                    stats=_new_stats())

    written = sorted(p.name for p in result.written)
    assert len(written) == 2, "neither source may be dropped - both are referenceable"
    assert len(set(n.lower() for n in written)) == 2, "and they must not collide once flattened"
    # The de-conflicted one is reported, so stored URLs follow it. Asserted as
    # a property rather than against the "{stem}-{ext}" literal: the naming
    # scheme is not what this test is about, and pinning it here would make a
    # rename of that convention look like a case-handling regression.
    assert list(result.renamed) == ["b/logo.png"], "only the second, de-conflicted source moved"
    new_name = Path(result.renamed["b/logo.png"]).name
    assert new_name in written and new_name.lower() != "logo.png"


def test_identical_basenames_are_still_skipped(tmp_path):
    """The other direction: an exact duplicate is still one image, because
    populate_db drops it too."""
    src, out = tmp_path / "in", tmp_path / "out"
    (src / "a").mkdir(parents=True)
    (src / "b").mkdir(parents=True)
    out.mkdir()
    Image.new("RGB", (20, 20), (9, 9, 9)).save(src / "a" / "logo.png")
    Image.new("RGB", (20, 20), (8, 8, 8)).save(src / "b" / "logo.png")

    result = om.optimize_directory(src, out, cfg=dict(om.BUILTIN_DEFAULTS),
                                    pngquant_path=om.find_pngquant(), logger=om.Logger(sys.stdout),
                                    stats=_new_stats())

    assert [p.name for p in result.written] == ["logo.png"]
    assert result.renamed == {}


# --- the animated exemption covers every animated raster, not just GIF ------

def test_animated_png_is_not_predicted_as_webp(tmp_path):
    """optimize_raster copies an animated non-GIF through untouched, so an
    APNG under --webp writes anim.png. Predicting anim.webp claimed a name
    nothing writes, and de-conflicted a genuine .webp producer against that
    phantom - a rename, and a rewrite of every stored URL, for a collision
    that never existed."""
    src, out = tmp_path / "in", tmp_path / "out"
    src.mkdir(), out.mkdir()
    _apng(src / "anim.png")
    Image.new("RGB", (20, 20), (4, 4, 4)).save(src / "anim.jpg")

    cfg = dict(om.BUILTIN_DEFAULTS) | {"webp": True}
    result = om.optimize_directory(src, out, cfg=cfg, pngquant_path=om.find_pngquant(),
                                    logger=om.Logger(sys.stdout), stats=_new_stats())

    assert sorted(p.name for p in result.written) == ["anim.png", "anim.webp"]
    # anim.jpg -> anim.webp is a real conversion; nothing else moved.
    assert result.renamed == {"anim.jpg": "anim.webp"}
    with Image.open(out / "anim.png") as written:
        assert written.n_frames == 3, "the animation survived"


@pytest.mark.parametrize("suffix, animated, expected", [
    (".gif", True, {"x.gif"}),
    (".gif", False, {"x.webp"}),
    (".gif", None, set()),            # undetermined: optimize_raster fails too, so nothing is written
    (".png", None, set()),
    (".png", True, {"x.png"}),
    (".png", False, {"x.webp"}),
    (".tiff", True, {"x.tiff"}),
    (".jpg", None, {"x.webp"}),      # cannot be animated, so never probed
    (".webp", None, {"x.webp"}),     # animated or not, the name is the same
])
def test_possible_output_names_resolves_every_animated_raster(suffix, animated, expected):
    """The prediction is a pure function of (suffix, cfg, animated) - no I/O,
    so it can be called once per de-confliction attempt without re-opening
    the source each time."""
    cfg = dict(om.BUILTIN_DEFAULTS) | {"webp": True}
    assert om.possible_output_names("x", suffix, cfg, animated) == expected


# --- an unterminated fence must not silently switch the scan off ------------

def test_unterminated_fence_is_reported(capsys):
    """fenced_spans runs an unclosed fence to EOF, which is CommonMark-correct
    but means one stray ``` line stops the <include> scan for the rest of the
    file. The old backtick-only regex needed a closing fence to match, so it
    kept scanning - losing that has to be said out loud, not inferred from a
    suddenly-short report."""
    text = 'a\n```\n<include from="sample.md"/>\n\n<include from="real.md"/>\n'

    assert INCLUDE_RE.findall(outside_fences(text, "topics/x.md")) == []
    assert "unterminated code fence" in capsys.readouterr().err


def test_a_closing_fence_on_the_last_line_is_not_reported(capsys):
    """The warning has to be precise: a file that simply ends with a closed
    code block reaches EOF too, and warning about it would train operators to
    ignore the message."""
    outside_fences("a\n```\nx\n```\n", "topics/x.md")

    assert capsys.readouterr().err == ""


# --- repair() takes both halves of a scan, or neither ----------------------

def test_repair_refuses_half_a_scan(tmp_path):
    """The two lists come from one find_chains call. Answering a half-supplied
    pair with a fresh scan silently discarded the caller's snapshot, so
    main()'s backup decision could describe a different repair than the one
    that ran."""
    import renumber_misnumbered_fragments as rmf
    conn = sqlite3.connect(":memory:")
    conn.executescript(SCHEMA_SQL)
    try:
        with pytest.raises(TypeError, match="both misnumbered and gapped"):
            rmf.repair(conn, [])
        with pytest.raises(TypeError, match="both misnumbered and gapped"):
            rmf.repair(conn, gapped=[])
        assert rmf.repair(conn)["chains_renumbered"] == 0          # neither: scans
        assert rmf.repair(conn, [], [])["chains_renumbered"] == 0  # both: uses them
    finally:
        conn.close()


# --- what process_file writes has to be what the planner claimed ------------

def test_a_write_the_planner_did_not_predict_is_refused(tmp_path, monkeypatch):
    """possible_output_names predicts, in a second place, what process_file ->
    optimize_raster -> encode_raster will do, and that prediction has drifted
    twice. Nothing connected the two halves, so both drifts were silent: a
    name claimed but never written de-conflicts an unrelated image and
    rewrites its stored URLs, and a name written but never claimed is free to
    overwrite another source's output."""
    src, out = tmp_path / "in", tmp_path / "out"
    src.mkdir(), out.mkdir()
    Image.new("RGB", (20, 20), (1, 1, 1)).save(src / "photo.png")

    # Stand in for a future encoder change the planner doesn't know about.
    real_process_file = om.process_file
    def drifting_process_file(source, dst, **kwargs):
        written = real_process_file(source, dst, **kwargs)
        return written.with_suffix(".avif") if written else written
    monkeypatch.setattr(om, "process_file", drifting_process_file)

    # ValueError, not RuntimeError: insert_optimized_media wraps this call in
    # `except ValueError` to turn it into a clean "error: ..." line, and a
    # RuntimeError sailed past that as a raw traceback.
    with pytest.raises(ValueError, match="possible_output_names did not predict"):
        om.optimize_directory(src, out, cfg=dict(om.BUILTIN_DEFAULTS), pngquant_path=om.find_pngquant(),
                               logger=om.Logger(sys.stdout), stats=_new_stats())


def test_an_unprobeable_source_claims_no_names(tmp_path, capsys):
    """A raster whose animation can't be determined is one optimize_raster is
    about to fail on for the same reason, so it writes nothing and must hold
    nothing. Claiming both names instead de-conflicted a perfectly good
    unrelated image against an output that never appears - renaming it, and
    rewriting every stored URL that pointed at it."""
    src, out = tmp_path / "in", tmp_path / "out"
    src.mkdir(), out.mkdir()
    (src / "broken.png").write_bytes(b"not a png at all")
    Image.new("RGB", (20, 20), (2, 2, 2)).save(src / "broken.jpg")

    cfg = dict(om.BUILTIN_DEFAULTS) | {"webp": True}
    stats = _new_stats()
    result = om.optimize_directory(src, out, cfg=cfg, pngquant_path=om.find_pngquant(),
                                    logger=om.Logger(sys.stdout), stats=stats)

    # broken.jpg keeps its own stem: nothing real ever claimed broken.webp.
    assert [p.name for p in result.written] == ["broken.webp"]
    assert result.renamed == {"broken.jpg": "broken.webp"}
    assert stats["errors"] == 1, "the unreadable file is still counted as one file's error"
    # And the probe said why, rather than swallowing it.
    assert "could not determine whether" in capsys.readouterr().out


def test_the_probe_reports_its_own_failure(tmp_path, capsys):
    """_is_animated_raster now runs for every PNG and TIFF in the tree, not
    just the handful of GIFs, so a systematic failure would shift the whole
    de-confliction pass. Swallowing it left no way to find out why."""
    bad = tmp_path / "corrupt.png"
    bad.write_bytes(b"still not a png")

    assert om._is_animated_raster(bad, om.Logger(sys.stdout)) is None
    assert "could not determine whether" in capsys.readouterr().out
    # Silent without a logger, so a standalone caller isn't forced to have one.
    assert om._is_animated_raster(bad) is None
    assert capsys.readouterr().out == ""


# --- the written-vs-claimed check must not fire on ordinary inputs ----------

@pytest.mark.parametrize("name", ["Diagram.SVG", "Diagram.Svg", "Photo.PNG", "Photo.JPG", "Notes.TXT"])
@pytest.mark.parametrize("webp", [True, False])
def test_an_uppercase_extension_is_not_mistaken_for_drift(tmp_path, name, webp):
    """possible_output_names' SVG branch returned the lowercase SVG_EXTENSION
    literal while optimize_svg writes `dst`, which carries the source's own
    spelling - so "Diagram.SVG" was claimed as "Diagram.svg". Harmless while
    it only fed the casefolded `claimed` map; once the written-vs-claimed
    check existed it aborted the whole run over one ordinary file."""
    src, out = tmp_path / "in", tmp_path / "out"
    src.mkdir(), out.mkdir()
    if name.lower().endswith(".svg"):
        (src / name).write_text('<svg xmlns="http://www.w3.org/2000/svg" width="10" height="10">'
                                '<rect width="10" height="10"/></svg>')
    elif name.lower().endswith(".txt"):
        (src / name).write_text("passthrough")
    else:
        Image.new("RGB", (20, 20), (3, 3, 3)).save(src / name)

    result = om.optimize_directory(src, out, cfg=dict(om.BUILTIN_DEFAULTS) | {"webp": webp},
                                    pngquant_path=om.find_pngquant(), logger=om.Logger(sys.stdout),
                                    stats=_new_stats())

    assert len(result.written) == 1, "the file was optimized, so it must survive the check"


def test_the_svg_branch_claims_the_source_s_own_spelling():
    """Every other branch echoes `suffix`; this one hardcoded the constant."""
    cfg = dict(om.BUILTIN_DEFAULTS) | {"webp": True}
    assert om.possible_output_names("d", ".SVG", cfg) == {"d.SVG", "d.webp"}
    assert om.possible_output_names("d", ".svg", cfg) == {"d.svg", "d.webp"}


def test_an_unpredicted_source_that_does_write_is_kept_not_fatal(tmp_path, monkeypatch, capsys):
    """A probe that fails where optimize_raster then succeeds is one file's
    bad luck, not drift between the planner and the writer. This module's
    rule is that a single file's problem is reported and the run continues -
    a dangling symlink killing the whole run was fixed as a bug in this same
    PR - so the check must not reintroduce that shape here."""
    src, out = tmp_path / "in", tmp_path / "out"
    src.mkdir(), out.mkdir()
    Image.new("RGB", (20, 20), (5, 5, 5)).save(src / "photo.png")
    monkeypatch.setattr(om, "_is_animated_raster", lambda source, logger=None: None)

    stats = _new_stats()
    result = om.optimize_directory(src, out, cfg=dict(om.BUILTIN_DEFAULTS) | {"webp": True},
                                    pngquant_path=om.find_pngquant(), logger=om.Logger(sys.stdout),
                                    stats=stats)

    assert [p.name for p in result.written] == ["photo.webp"], "a good file is kept, not discarded"
    assert "the de-confliction pass held no name for it" in capsys.readouterr().out
