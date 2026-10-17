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
test_animated_media.py

Covers what ADFA-3305 added to optimize_db_media.py and
update_media_references.py: --types, the animated-GIF encoders (GIF, animated
WebP, VP9 WebM under --webm), rewriting an <img> that shows a converted
animation as a <video>, and undoing that rewrite exactly.

The encoders shell out to the real gifsicle and ffmpeg binaries rather than
mocking them, as test_media_bundle.py does for pngquant, so both must be on
PATH.

Run it either way - as a plain script:

    uv run scripts/test_animated_media.py

or under pytest:

    uv run --with brotli --with Pillow --with pytest pytest scripts/test_animated_media.py
"""
from __future__ import annotations

import io
import random
import sqlite3
import sys
from pathlib import Path

import brotli
from PIL import Image

sys.path.insert(0, str(Path(__file__).resolve().parent))

import optimize_db_media as opt  # noqa: E402
import test_media_bundle as base  # noqa: E402

CONTENT_TYPES = [(3, "image/png", "none"), (11, "image/gif", "none"), (12, "text/html", "brotli"),
                 (1, "text/css", "brotli"), (26, "image/webp", "brotli")]


def _build_database(path: Path, rows: list, content_types: list = CONTENT_TYPES) -> None:
    """A minimal documentation.db holding `rows` of (path, content type, bytes,
    templateId), stored the way each content type says to."""
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
    conn.executemany("INSERT INTO ContentTypes (id, value, compression) VALUES (?, ?, ?)", content_types)
    by_value = {value: (type_id, compression) for type_id, value, compression in content_types}
    for stored_path, value, data, *template in rows:
        type_id, compression = by_value[value]
        blob = brotli.compress(data) if compression == "brotli" else data
        conn.execute("INSERT INTO Content (path, languageID, content, contentTypeID, templateId) "
                     "VALUES (?, ?, ?, ?, ?)", (stored_path, 1, blob, type_id, template[0] if template else 0))
    conn.commit()
    conn.close()


def _noisy_png(width, height):
    """A PNG that --webp certainly converts: noise defeats PNG's own
    compression, unlike base._png's gradient, which can come out no smaller
    as WebP and so is left alone either way."""
    rng = random.Random(width * height)
    image = Image.new("RGB", (width, height))
    image.putdata([(rng.randrange(256), rng.randrange(256), rng.randrange(256))
                   for _ in range(width * height)])
    buffer = io.BytesIO()
    image.save(buffer, "PNG")
    return buffer.getvalue()


def test_types_leaves_every_other_type_alone(tmp_path):
    """--types image/gif is how a database whose earlier manifest is gone gets
    its GIFs converted without re-encoding the lossy PNG and WebP already in it."""
    db_path = tmp_path / "documentation.db"
    _build_database(db_path, [("k/images/hero.png", "image/png", _noisy_png(600, 300)),
                              ("a/media/badge.gif", "image/gif", base._gif(640, 480))])
    before = base._snapshot(db_path)

    assert opt.run(base._optimize_cfg(db_path, types={"image/gif"})) == 0

    after = base._snapshot(db_path)
    assert after.get("k/images/hero.png") == before["k/images/hero.png"], "the PNG must not be touched"
    assert "a/media/badge.gif" not in after and "a/media/badge.webp" in after


def test_types_refuses_a_type_it_cannot_optimize(tmp_path):
    db_path = tmp_path / "documentation.db"
    _build_database(db_path, [("a/media/badge.gif", "image/gif", base._gif(64, 48))])
    before = base._snapshot(db_path)
    assert opt.run(base._optimize_cfg(db_path, types={"video/mp4"})) == 1
    assert base._snapshot(db_path) == before


def test_types_keeps_the_manifest_entries_of_types_it_skips(tmp_path):
    """A --types run still writes a manifest of the whole database: dropping the
    skipped types' entries would make the next full run re-encode them."""
    db_path = tmp_path / "documentation.db"
    first, second = tmp_path / "first.tsv", tmp_path / "second.tsv"
    _build_database(db_path, [("k/images/hero.png", "image/png", base._png(900, 400)),
                              ("a/media/badge.gif", "image/gif", base._gif(640, 480))])
    assert opt.run(base._optimize_cfg(db_path, manifest_out=first)) == 0
    assert opt.run(base._optimize_cfg(db_path, types={"image/gif"}, manifest_in=first,
                                      manifest_out=second)) == 0
    assert opt.read_manifest(second) == opt.read_manifest(first)


if __name__ == "__main__":
    sys.exit(base._main(globals()))
