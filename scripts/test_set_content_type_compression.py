#!/usr/bin/env python3
# /// script
# requires-python = ">=3.9"
# dependencies = [
#     "brotli",
#     "Pillow",
#     "pytest",
# ]
# ///
"""
test_set_content_type_compression.py

Covers set_content_type_compression.py: rows and their ContentTypes flag change
together, the change is lossless both ways (including a row chunked across
several Content rows), a manifest is kept in step, and --create / --dry-run do
what they say.

    uv run scripts/test_set_content_type_compression.py
"""
from __future__ import annotations

import random
import sqlite3
import sys
from pathlib import Path

import brotli

sys.path.insert(0, str(Path(__file__).resolve().parent))

import optimize_db_media as opt  # noqa: E402
import set_content_type_compression as sctc  # noqa: E402
import test_animated_media as animated  # noqa: E402
import test_media_bundle as base  # noqa: E402

# Not real WebP, which this script never decodes: it only moves stored bytes
# between encodings. One item spans three Content rows.
SMALL = b"RIFF\x00\x00\x00\x00WEBPVP8 " + bytes(range(256)) * 8
LARGE = random.Random(5).randbytes(opt.CHUNK_SIZE * 2 + 1234)


def _database(tmp_path: Path) -> Path:
    db_path = tmp_path / "documentation.db"
    animated._build_database(db_path, [("a/media/small.webp", "image/webp", SMALL),
                                       ("a/media/large.webp", "image/webp", b"placeholder"),
                                       ("a/page.html", "text/html", b"<p>hi</p>")])
    # Stored the way a real chunked row is: one Brotli stream split across
    # "<path>", "<path>-1", "<path>-2", ...
    stored = brotli.compress(LARGE, quality=0)
    chunks = [stored[i:i + opt.CHUNK_SIZE] for i in range(0, len(stored), opt.CHUNK_SIZE)]
    with sqlite3.connect(db_path) as conn:
        conn.execute("UPDATE Content SET content = ? WHERE path = 'a/media/large.webp'", (chunks[0],))
        for number, chunk in enumerate(chunks[1:], start=1):
            conn.execute("INSERT INTO Content (path, languageID, content, contentTypeID, templateId) "
                         "VALUES (?, 1, ?, 26, 0)", (f"a/media/large.webp-{number}", chunk))
    return db_path


def _cfg(db_path: Path, content_type="image/webp", compression="none", **overrides) -> dict:
    cfg = {"db_path": db_path, "content_type": content_type, "compression": compression, "create": False,
           "manifest": None, "dry_run": False}
    cfg.update(overrides)
    return cfg


def _flag(db_path: Path, value: str):
    with sqlite3.connect(db_path) as conn:
        row = conn.execute("SELECT compression FROM ContentTypes WHERE value = ?", (value,)).fetchone()
    return row[0] if row else None


def _stored(db_path: Path, path: str) -> bytes:
    with sqlite3.connect(db_path) as conn:
        first = conn.execute("SELECT content FROM Content WHERE path = ?", (path,)).fetchone()[0]
        return opt.reassemble(conn, path, first)


def test_rows_and_flag_change_together_and_losslessly(tmp_path):
    db_path = _database(tmp_path)
    before = base._snapshot(db_path)

    assert sctc.run(_cfg(db_path)) == 0

    assert _flag(db_path, "image/webp") == "none"
    assert _stored(db_path, "a/media/small.webp") == SMALL, "stored uncompressed, byte for byte"
    assert _stored(db_path, "a/media/large.webp") == LARGE, "the chunked row reassembles uncompressed"
    assert base._snapshot(db_path) == before, "every row still decodes to the same content"


def test_running_it_back_restores_the_old_setting(tmp_path):
    db_path = _database(tmp_path)
    before = base._snapshot(db_path)
    assert sctc.run(_cfg(db_path)) == 0
    assert sctc.run(_cfg(db_path, compression="brotli")) == 0
    assert _flag(db_path, "image/webp") == "brotli"
    assert brotli.decompress(_stored(db_path, "a/media/small.webp")) == SMALL
    assert base._snapshot(db_path) == before


def test_manifest_entries_follow_the_rows_they_describe(tmp_path):
    """The optimizer's manifest records STORED bytes; left alone, it would see
    every re-stored row as edited and re-encode it on the next run."""
    db_path = _database(tmp_path)
    manifest = tmp_path / "manifest.tsv"
    small_stored = _stored(db_path, "a/media/small.webp")
    opt.write_manifest(manifest, {
        "a/media/small.webp": (opt.digest(small_stored), len(small_stored), "a/media/small.png"),
        "a/media/large.webp": ("0" * 64, 1, None),  # already stale before this run
    })

    assert sctc.run(_cfg(db_path, manifest=manifest)) == 0

    entries = opt.read_manifest(manifest)
    assert entries["a/media/small.webp"] == (opt.digest(SMALL), len(SMALL), "a/media/small.png")
    assert entries["a/media/large.webp"] == ("0" * 64, 1, None), "a stale entry is left as it was"


def test_create_adds_a_missing_type_and_is_then_a_no_op(tmp_path):
    db_path = _database(tmp_path)
    assert sctc.run(_cfg(db_path, content_type="video/webm")) == 1, "refused without --create"
    assert sctc.run(_cfg(db_path, content_type="video/webm", create=True)) == 0
    assert _flag(db_path, "video/webm") == "none"
    assert sctc.run(_cfg(db_path, content_type="video/webm", create=True)) == 0


def test_dry_run_changes_nothing(tmp_path):
    db_path = _database(tmp_path)
    before = {path: _stored(db_path, path) for path in ("a/media/small.webp", "a/media/large.webp")}
    assert sctc.run(_cfg(db_path, dry_run=True)) == 0
    assert _flag(db_path, "image/webp") == "brotli"
    assert {path: _stored(db_path, path) for path in before} == before


if __name__ == "__main__":
    sys.exit(base._main(globals()))
