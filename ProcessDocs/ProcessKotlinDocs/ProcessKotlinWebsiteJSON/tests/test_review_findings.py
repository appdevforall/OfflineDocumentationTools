"""Regression tests for the PR #24 review findings (F01-F15).

Each test constructs the specific input the reviewer identified as untested -
the shapes nothing else in the suite feeds these functions. Every one of them
fails against the code as it stood before the corresponding fix, which is the
only reason they are worth having: the two criticals in particular were silent,
exit-0 data loss that truthful-looking statistics actively concealed.
"""
import json
import random
import sqlite3
import sys

import brotli
import pytest
from PIL import Image

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
                                     logger=om.Logger(sys.stdout), stats=_new_stats())

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
