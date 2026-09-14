#!/usr/bin/env python3
# /// script
# requires-python = ">=3.9"
# dependencies = [
#     "brotli",
#     "Pillow",
#     "scour",
#     "pytest",
# ]
# ///
"""
test_media_bundle.py

Covers the rollback bundle optimize_db_media.py --save-originals writes and
update_media_references.py adds to: the three hand-rolled text formats, the
exact-inverse rewrite/undo pair, and one end-to-end conversion and
reconstruction over a real SQLite database.

These are the parts whose failure mode is a bundle that looks fine and cannot
actually put the documentation back, which is not something the tools
themselves would report. The round-trip test is the one that matters most: it
converts media (renaming files and rewriting the pages that link to them),
restores both halves, and asserts the database is byte-for-byte what it
started as, per path.

Run it either way - as a plain script, needing nothing installed:

    uv run scripts/test_media_bundle.py

or under pytest, which needs the same four packages on the path:

    uv run --with brotli --with Pillow --with scour --with pytest pytest scripts/test_media_bundle.py
"""
from __future__ import annotations

import hashlib
import io
import shutil
import sqlite3
import sys
import tempfile
import traceback
from pathlib import Path

import brotli
import pytest
from PIL import Image

sys.path.insert(0, str(Path(__file__).resolve().parent))

import optimize_db_media as opt  # noqa: E402
import update_media_references as ref  # noqa: E402


# --- the pure pieces ---------------------------------------------------------

def test_bundle_relative_refuses_paths_that_escape_the_bundle():
    assert opt.bundle_relative("a/b/c.png") == "a/b/c.png"
    assert opt.bundle_relative("./a//b.png") == "a/b.png"
    assert opt.bundle_relative("a/./b.png") == "a/b.png"
    for escaping in ("/etc/passwd", "../../etc/passwd", "a/../../x.png", "..", ""):
        assert opt.bundle_relative(escaping) is None, escaping


def test_rewrite_and_undo_are_exact_inverses():
    """Also pins the anchoring rules: a converted name is only rewritten where
    it is used as a reference, never as the tail of a longer filename and never
    in prose."""
    renames = {"copy.png": "copy.webp", "a.gif": "a.webp"}
    pattern = ref.build_pattern(renames)
    text = ('<img src="/i/copy.png"><img src="my_copy.png"><p>save as copy.png</p>'
            "url(a.gif) \"copy.pngx\" 'a.gif' =a.gif ")

    rewritten, sites = ref.rewrite_text(text, pattern, renames)

    assert rewritten == ('<img src="/i/copy.webp"><img src="my_copy.png"><p>save as copy.png</p>'
                         "url(a.webp) \"copy.pngx\" 'a.webp' =a.webp ")
    assert len(sites) == 4
    # Every recorded offset addresses the new name in the REWRITTEN text - that
    # is the contract undo_rewrite is walking back.
    for offset, old_name in sites:
        assert rewritten[offset:offset + len(renames[old_name])] == renames[old_name]
    assert ref.undo_rewrite(rewritten, sites, renames) == text


def test_rewrite_reports_no_sites_when_nothing_matches():
    pattern = ref.build_pattern({"copy.png": "copy.webp"})
    text = "<p>nothing to see</p>"
    assert ref.rewrite_text(text, pattern, {"copy.png": "copy.webp"}) == (text, [])


def test_undo_refuses_text_whose_offsets_have_moved():
    """Every edit here lengthens the text BEFORE the recorded site, which is
    what moves an offset. A same-length edit moves nothing and is undetectable
    from the sites alone, which is why restore_references gates on the whole
    row's digest before it ever gets here."""
    renames = {"copy.png": "copy.webp"}
    pattern = ref.build_pattern(renames)
    rewritten, sites = ref.rewrite_text('<img src="/i/copy.png">', pattern, renames)
    assert ref.undo_rewrite(rewritten, sites, renames) == '<img src="/i/copy.png">'

    for moved in ("<!-- inserted -->" + rewritten,       # grown at the front
                  rewritten.replace("<img", "<image"),   # grown before the site
                  rewritten.replace('src="', 'src ="')):
        with pytest.raises(RuntimeError):
            ref.undo_rewrite(moved, sites, renames)


def test_undo_refuses_a_name_the_rename_map_does_not_cover():
    with pytest.raises(RuntimeError):
        ref.undo_rewrite("x.webp", [(0, "x.png")], {})


def test_unloggable_names_checks_both_halves_of_the_map():
    assert ref.unloggable_names({"a.png": "a.webp"}) == []
    assert ref.unloggable_names({"a,b.png": "x.webp", "c:d.png": "y.webp"}) == ["a,b.png", "c:d.png"]
    # A new name only has to survive renames.tsv's columns, but it does have to.
    assert ref.unloggable_names({"a.png": "we\tbp"}) == ["we\tbp"]
    assert ref.unloggable_names({"a.png": "b,c.webp"}) == []


def test_conflicting_renames_spots_one_name_meaning_two_things(tmp_path):
    ref.write_rename_log(tmp_path, {"a.png": "a.webp"})
    assert ref.conflicting_renames(tmp_path / ref.BUNDLE_RENAMES, {"a.png": "a.webp"}) == []
    assert ref.conflicting_renames(tmp_path / ref.BUNDLE_RENAMES, {"b.png": "b.webp"}) == []
    assert ref.conflicting_renames(tmp_path / ref.BUNDLE_RENAMES, {"a.png": "a-png.webp"}) == ["a.png"]


# --- the three text formats --------------------------------------------------

def test_originals_index_round_trips(tmp_path):
    archive = opt.OriginalsArchive(tmp_path, lambda message: None)
    archive.add("k/img/one.png", "image/png", 1, 0, b"one")
    archive.add("k/img/two.svg", "image/svg+xml", 2, 7, b"two")
    archive.mark_converted("k/img/one.png", "k/img/one.webp")
    index_path = archive.write_index()

    entries = {e[2]: e for e in opt.read_originals_index(index_path)}
    assert entries["k/img/one.png"][:7] == [
        opt.digest(b"one"), 3, "k/img/one.png", "image/png", 1, 0, "originals/k/img/one.png"]
    assert entries["k/img/one.png"][7] == "k/img/one.webp"
    assert entries["k/img/two.svg"][4:7] == [2, 7, "originals/k/img/two.svg"]
    assert entries["k/img/two.svg"][7] is None
    assert (tmp_path / "originals/k/img/one.png").read_bytes() == b"one"


def test_originals_index_rejects_a_location_outside_the_bundle(tmp_path):
    index = tmp_path / opt.BUNDLE_ORIGINALS_INDEX
    fields = [opt.digest(b"x"), "1", "k/img/a.png", "image/png", "1", "0", "../../../etc/hosts", "-"]
    index.write_text(opt.ORIGINALS_HEADER + "\n" + "\t".join(fields) + "\n", encoding="utf-8")
    with pytest.raises(RuntimeError, match="not a location inside the bundle"):
        opt.read_originals_index(index)


def test_originals_index_rejects_a_truncated_line(tmp_path):
    index = tmp_path / opt.BUNDLE_ORIGINALS_INDEX
    index.write_text(opt.ORIGINALS_HEADER + "\nsha\t1\tk/img/a.png\n", encoding="utf-8")
    with pytest.raises(RuntimeError, match="expected 8 tab-separated fields"):
        opt.read_originals_index(index)


def test_archive_is_additive_and_keeps_the_first_original(tmp_path):
    log = []
    first = opt.OriginalsArchive(tmp_path, log.append)
    assert first.add("k/img/a.png", "image/png", 1, 0, b"original") is True
    first.write_index()

    # A second run over the same bundle, after someone overwrote the file.
    second = opt.OriginalsArchive(tmp_path, log.append)
    assert second.preloaded == 1
    assert second.add("k/img/a.png", "image/png", 1, 0, b"original") is True  # unchanged: no new copy
    assert second.reused == 1
    assert second.add("k/img/a.png", "image/png", 1, 0, b"edited by an author") is True
    assert second.revisions == 1
    second.write_index()

    entries = opt.read_originals_index(tmp_path / opt.BUNDLE_ORIGINALS_INDEX)
    assert len(entries) == 2
    primaries = [e for e in entries if e[6].startswith(f"{opt.BUNDLE_ORIGINALS_DIR}/")]
    assert len(primaries) == 1
    assert (tmp_path / primaries[0][6]).read_bytes() == b"original"


def test_archive_does_not_let_one_path_overwrite_another(tmp_path):
    """Two Content.path values can want the same file in the bundle - SQLite's
    UNIQUE is case-sensitive, macOS's default filesystem is not."""
    archive = opt.OriginalsArchive(tmp_path, lambda message: None)
    assert archive.add("k/img/Logo.png", "image/png", 1, 0, b"upper") is True
    assert archive.add("k/img/logo.png", "image/png", 1, 0, b"lower") is True
    locations = {e[2]: e[6] for e in archive._entries}
    assert locations["k/img/Logo.png"] != locations["k/img/logo.png"]
    assert (tmp_path / locations["k/img/Logo.png"]).read_bytes() == b"upper"
    assert (tmp_path / locations["k/img/logo.png"]).read_bytes() == b"lower"


def test_archive_reports_failure_when_it_cannot_write(tmp_path):
    """The optimizer relies on this False to leave the row alone; if add() ever
    goes back to swallowing the error, the row is rewritten with no copy kept.

    The OSError is provoked by putting a FILE where add() needs a directory,
    which fails for root as well - a permissions trick would not, and most CI
    containers run as root."""
    archive = opt.OriginalsArchive(tmp_path, lambda message: None)
    assert archive.add("/etc/passwd", "image/png", 1, 0, b"x") is False  # refused as an unsafe path

    (tmp_path / opt.BUNDLE_ORIGINALS_DIR).mkdir()
    (tmp_path / opt.BUNDLE_ORIGINALS_DIR / "k").write_text("not a directory")
    assert archive.add("k/img/a.png", "image/png", 1, 0, b"x") is False  # refused as an OSError
    assert archive.skipped == 2


def test_archive_rewrites_an_original_that_was_pruned_from_the_bundle(tmp_path):
    """An index entry is not a copy. originals/ is the bulk of a bundle's size
    and so the obvious thing to delete to reclaim space; reporting those assets
    as archived would let the optimizer destroy rows nothing has a copy of."""
    first = opt.OriginalsArchive(tmp_path, lambda message: None)
    assert first.add("k/img/a.png", "image/png", 1, 0, b"original") is True
    first.write_index()
    (tmp_path / "originals/k/img/a.png").unlink()

    second = opt.OriginalsArchive(tmp_path, lambda message: None)
    assert second.add("k/img/a.png", "image/png", 1, 0, b"original") is True
    assert second.replaced_missing == 1 and second.reused == 0
    assert (tmp_path / "originals/k/img/a.png").read_bytes() == b"original"


def test_reference_log_accumulates_passes(tmp_path):
    ref.write_reference_log(tmp_path, [("before1", "after1", "a.html", [(3, "x.png")])])
    ref.write_reference_log(tmp_path, [("after1", "after2", "a.html", [(3, "x.webp")]),
                                       ("beforeB", "afterB", "b.html", [(9, "y.gif")])])
    # Writing the same pass twice must not duplicate it.
    ref.write_reference_log(tmp_path, [("before1", "after1", "a.html", [(3, "x.png")])])

    records = ref.read_reference_log(tmp_path / ref.BUNDLE_REFERENCES)
    assert len(records) == 3
    assert sorted(r[2] for r in records) == ["a.html", "a.html", "b.html"]
    assert ("before1", "after1", "a.html", [(3, "x.png")]) in records


def test_rename_log_merges_rather_than_replaces(tmp_path):
    ref.write_rename_log(tmp_path, {"a.png": "a.webp"})
    ref.write_rename_log(tmp_path, {"b.gif": "b.webp"})
    assert ref.read_rename_log(tmp_path / ref.BUNDLE_RENAMES) == {"a.png": "a.webp", "b.gif": "b.webp"}


def test_rewind_row_walks_a_two_pass_chain_back_to_the_original():
    renames = {"x.png": "x.webp", "x.webp": "x-2.webp"}
    original = '<img src="/i/x.png">'
    once, sites1 = ref.rewrite_text(original, ref.build_pattern({"x.png": "x.webp"}), renames)
    twice, sites2 = ref.rewrite_text(once, ref.build_pattern({"x.webp": "x-2.webp"}), renames)
    chain = [
        (ref.digest(original.encode()), ref.digest(once.encode()), "p.html", sites1),
        (ref.digest(once.encode()), ref.digest(twice.encode()), "p.html", sites2),
    ]

    text, final, undone = ref.rewind_row(twice, ref.digest(twice.encode()), chain, renames)

    assert len(undone) == 2
    assert text == original
    assert final == ref.digest(original.encode())
    # A row already back at the original undoes nothing rather than erroring.
    assert ref.rewind_row(original, ref.digest(original.encode()), chain, renames)[2] == []


def test_rewind_row_completes_from_the_middle_of_a_chain():
    """A row that simply was not at the newest state still rewinds all the way
    to the single root. Judging completeness by how many records were consumed
    refused exactly this case."""
    renames = {"x.png": "x.webp", "x.webp": "x-2.webp"}
    original = '<img src="/i/x.png">'
    once, sites1 = ref.rewrite_text(original, ref.build_pattern({"x.png": "x.webp"}), renames)
    twice, sites2 = ref.rewrite_text(once, ref.build_pattern({"x.webp": "x-2.webp"}), renames)
    chain = [
        (ref.digest(original.encode()), ref.digest(once.encode()), "p.html", sites1),
        (ref.digest(once.encode()), ref.digest(twice.encode()), "p.html", sites2),
    ]

    text, final, undone = ref.rewind_row(once, ref.digest(once.encode()), chain, renames)

    assert text == original
    assert len(undone) == 1 and len(chain) == 2, "fewer records, but still complete"
    assert ref.chain_roots(chain) == {ref.digest(original.encode())}
    assert final in ref.chain_roots(chain), "landing on the single root is what makes it complete"


def test_rewind_row_refuses_an_ambiguous_step():
    """Two passes recorded as producing identical text leave no way to know
    which edits to undo; picking one would silently undo the wrong ones."""
    chain = [("before-one", "same", "p.html", [(10, "x.png")]),
             ("before-two", "same", "p.html", [(20, "y.png")])]
    with pytest.raises(RuntimeError, match="ambiguous"):
        ref.rewind_row("text", "same", chain, {"x.png": "x.webp", "y.png": "y.webp"})


def test_rewind_row_stops_where_a_chain_was_broken_by_an_edit():
    """If someone edits a page between two passes, the earlier pass's "after"
    digest describes text that no longer existed when the later pass ran. Its
    offsets have moved, so it cannot be replayed backwards at all - and the
    caller has to be able to see that only part of the history came apart."""
    renames = {"x.png": "x.webp", "y.gif": "y.webp"}
    original = '<img src="/i/x.png">'
    once, sites1 = ref.rewrite_text(original, ref.build_pattern({"x.png": "x.webp"}), renames)
    edited = once + '<p>an author added this</p><img src="/i/y.gif">'
    twice, sites2 = ref.rewrite_text(edited, ref.build_pattern({"y.gif": "y.webp"}), renames)
    chain = [
        (ref.digest(original.encode()), ref.digest(once.encode()), "p.html", sites1),
        (ref.digest(edited.encode()), ref.digest(twice.encode()), "p.html", sites2),
    ]

    _text, _final, undone = ref.rewind_row(twice, ref.digest(twice.encode()), chain, renames)

    assert len(undone) == 1 and len(chain) == 2, "the broken link must not be crossed"
    # Two roots is what tells the caller this history is in pieces, and is why
    # the row is refused rather than written back half-reversed.
    assert len(ref.chain_roots(chain)) == 2


# --- end to end ---------------------------------------------------------------

CONTENT_TYPES = [(2, "image/svg+xml", "brotli"), (3, "image/png", "none"), (11, "image/gif", "none"),
                 (12, "text/html", "brotli"), (1, "text/css", "brotli"), (23, "image/x-icon", "none"),
                 (26, "image/webp", "brotli")]


def _png(width, height):
    image = Image.new("RGB", (width, height))
    image.putdata([((x * 7) % 256, (y * 5) % 256, 0) for y in range(height) for x in range(width)])
    buffer = io.BytesIO()
    image.save(buffer, "PNG")
    return buffer.getvalue()


def _gif(width, height):
    image = Image.new("P", (width, height))
    image.putpalette([i % 256 for i in range(768)])
    image.putdata([(x + y) % 256 for y in range(height) for x in range(width)])
    buffer = io.BytesIO()
    image.save(buffer, "GIF")
    return buffer.getvalue()


SVG = (b'<?xml version="1.0"?><!-- c --><svg xmlns="http://www.w3.org/2000/svg" width="10" height="10" '
       b'viewBox="0 0 10 10"><circle cx="5.123456789" cy="5.98765" r="4.5555"/></svg>')


def _build_database(path: Path) -> None:
    conn = sqlite3.connect(path)
    conn.executescript("""
        CREATE TABLE Languages (id INTEGER PRIMARY KEY AUTOINCREMENT, value TEXT NOT NULL UNIQUE);
        CREATE TABLE ContentTypes (id INTEGER PRIMARY KEY AUTOINCREMENT, value TEXT NOT NULL UNIQUE,
                                   compression TEXT NOT NULL);
        CREATE TABLE Content (id INTEGER PRIMARY KEY AUTOINCREMENT, path TEXT NOT NULL,
                              languageID INTEGER NOT NULL, content BLOB NOT NULL,
                              contentTypeID INTEGER NOT NULL, templateId INTEGER NOT NULL DEFAULT 0,
                              UNIQUE('path'));
    """)
    conn.execute("INSERT INTO Languages (id, value) VALUES (1, 'en')")
    conn.executemany("INSERT INTO ContentTypes (id, value, compression) VALUES (?, ?, ?)", CONTENT_TYPES)
    by_value = {value: (type_id, compression) for type_id, value, compression in CONTENT_TYPES}

    rows = [
        ("k/images/hero.png", "image/png", 0, _png(900, 400)),
        ("a/media/badge.gif", "image/gif", 0, _gif(640, 480)),
        ("j/api/diagram.svg", "image/svg+xml", 0, SVG),
        ("i/favicon.ico", "image/x-icon", 0, _png(16, 16)),
        ("k/home.html", "text/html", 7,
         b'<img src="/k/images/hero.png"><p>saved as hero.png</p><img src="../a/media/badge.gif">'),
        ("a/site.css", "text/css", 0, b'body{background:url("media/badge.gif")}'),
    ]
    for stored_path, value, template_id, data in rows:
        type_id, compression = by_value[value]
        blob = brotli.compress(data) if compression == "brotli" else data
        conn.execute("INSERT INTO Content (path, languageID, content, contentTypeID, templateId) "
                     "VALUES (?, ?, ?, ?, ?)", (stored_path, 1, blob, type_id, template_id))
    conn.commit()
    conn.close()


def _snapshot(path: Path) -> dict:
    """Every row's logical content: type, language, template and a digest of the
    DECODED bytes, which is what has to survive a conversion and its undo."""
    conn = sqlite3.connect(path)
    codec = opt.BrotliCodec(opt.load_dictionary(conn))
    rows = conn.execute(
        "SELECT c.path, c.content, c.languageID, c.templateId, ct.value, ct.compression "
        "FROM Content c JOIN ContentTypes ct ON c.contentTypeID = ct.id").fetchall()
    lengths = {row[0]: len(row[1]) for row in rows}
    out = {}
    for stored_path, blob, language_id, template_id, value, compression in rows:
        if opt.is_continuation_path(lengths, stored_path):
            continue
        data = codec.decompress(opt.reassemble(conn, stored_path, blob), compression)
        out[stored_path] = (value, language_id, template_id, hashlib.sha256(data).hexdigest())
    codec.close()
    conn.close()
    return out


def _optimize_cfg(db_path: Path, **overrides) -> dict:
    cfg = {"db_path": db_path, "dry_run": False, "verbose": False, "webp": True,
           "manifest_out": None, "manifest_in": None, "save_originals": None, "restore_originals": None,
           **opt.DEFAULTS}
    cfg.update(overrides)
    return cfg


def test_convert_then_restore_reproduces_the_database(tmp_path):
    """The whole point of the bundle, over a real database: convert media to
    WEBP (which renames files and rewrites the pages linking to them), then put
    both halves back and require the result to be what it started as."""
    db_path = tmp_path / "documentation.db"
    bundle = tmp_path / "bundle"
    _build_database(db_path)
    before = _snapshot(db_path)
    pre_conversion = tmp_path / "before.db"
    shutil.copy(db_path, pre_conversion)

    assert opt.run(_optimize_cfg(db_path, save_originals=bundle)) == 0
    converted = _snapshot(db_path)
    assert converted != before, "the optimizer should have changed something"
    # The GIF is the one asset guaranteed to convert: WEBP beats it comfortably,
    # whereas a smooth-gradient PNG can legitimately come out no smaller and is
    # then left alone, extension and all.
    assert "a/media/badge.webp" in converted and "a/media/badge.gif" not in converted
    # Untouchable types are archived but never rewritten.
    assert converted["i/favicon.ico"] == before["i/favicon.ico"]

    reference_cfg = {"db_path": db_path, "before": pre_conversion, "dry_run": False, "workers": 2,
                     "verbose": False, "save_originals": bundle, "restore_originals": None}
    assert ref.run(reference_cfg) == 0
    # Both reference forms - a relative href in HTML and a CSS url() - follow.
    rewritten = _snapshot(db_path)
    assert rewritten["k/home.html"] != converted["k/home.html"]
    assert rewritten["a/site.css"] != converted["a/site.css"]

    assert opt.restore_originals(_optimize_cfg(db_path, restore_originals=bundle)) == 0
    assert ref.restore_references({"db_path": db_path, "before": None, "dry_run": False, "workers": 2,
                                   "verbose": False, "save_originals": None,
                                   "restore_originals": bundle}) == 0

    assert _snapshot(db_path) == before


def test_restore_puts_back_language_and_template_even_when_bytes_match(tmp_path):
    """i/favicon.ico is archived but never rewritten, so its content always
    matches the archive - the shortcut that used to skip the row entirely and
    leave its other columns wherever they had drifted to."""
    db_path = tmp_path / "documentation.db"
    bundle = tmp_path / "bundle"
    _build_database(db_path)
    assert opt.run(_optimize_cfg(db_path, save_originals=bundle)) == 0
    with sqlite3.connect(db_path) as conn:
        conn.execute("UPDATE Content SET languageID = 99, templateId = 42 WHERE path = 'i/favicon.ico'")

    assert opt.restore_originals(_optimize_cfg(db_path, restore_originals=bundle)) == 0

    with sqlite3.connect(db_path) as conn:
        row = conn.execute("SELECT languageID, templateId FROM Content WHERE path = 'i/favicon.ico'"
                           ).fetchone()
    assert row == (1, 0)


def test_restore_refuses_a_bundle_from_another_database(tmp_path):
    """Several same-named databases sit side by side; pointing a restore at the
    wrong one would inject every archived asset into it."""
    db_path = tmp_path / "documentation.db"
    bundle = tmp_path / "bundle"
    _build_database(db_path)
    assert opt.run(_optimize_cfg(db_path, save_originals=bundle)) == 0

    stranger = tmp_path / "unrelated.db"
    _build_database(stranger)
    with sqlite3.connect(stranger) as conn:
        conn.execute("UPDATE Content SET path = 'z/' || path")
    unrelated_before = _snapshot(stranger)

    assert opt.restore_originals(_optimize_cfg(stranger, restore_originals=bundle)) == 1
    assert _snapshot(stranger) == unrelated_before, "a refused restore must change nothing"


def test_a_row_whose_original_cannot_be_archived_is_not_rewritten(tmp_path):
    """--save-originals promises nothing is destroyed without a copy surviving,
    so an unwritable bundle has to stop the conversion, not just log it."""
    db_path = tmp_path / "documentation.db"
    bundle = tmp_path / "bundle"
    _build_database(db_path)
    before = _snapshot(db_path)

    # add() is made to refuse directly rather than through a read-only
    # directory: root ignores the permission bits, and CI usually runs as root.
    unpatched = opt.OriginalsArchive.add
    opt.OriginalsArchive.add = lambda self, *args, **kwargs: False
    try:
        assert opt.run(_optimize_cfg(db_path, save_originals=bundle)) == 1
    finally:
        opt.OriginalsArchive.add = unpatched
    assert _snapshot(db_path) == before


def _main() -> int:
    """Runs the tests without pytest installed as a runner, so this file works
    the same way the tools beside it do (`uv run scripts/test_media_bundle.py`)."""
    failures = []
    for name, function in sorted(globals().items()):
        if not name.startswith("test_") or not callable(function):
            continue
        with tempfile.TemporaryDirectory() as directory:
            needs_tmp = "tmp_path" in function.__code__.co_varnames[:function.__code__.co_argcount]
            try:
                function(Path(directory)) if needs_tmp else function()
            # pytest's own failure signals (pytest.raises, pytest.fail) derive
            # from BaseException, not Exception, so catching Exception here let
            # a failing assertion escape and abort the whole run.
            except (KeyboardInterrupt, SystemExit):
                raise
            except BaseException as exc:  # noqa: BLE001 - a failure is the result here
                frame = traceback.extract_tb(exc.__traceback__)[-1]
                failures.append(f"{name} (line {frame.lineno}: {frame.line}): "
                                f"{type(exc).__name__}: {exc}")
                print(f"FAIL {name}", file=sys.stderr)
            else:
                print(f"ok   {name}")
    print()
    print(f"{len(failures)} failure(s)" if failures else "all tests passed")
    for failure in failures:
        print(f"  {failure}", file=sys.stderr)
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(_main())
