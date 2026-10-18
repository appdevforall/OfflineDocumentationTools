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

import contextlib
import io
import os
import random
import shutil
import sqlite3
import subprocess
import sys
from pathlib import Path

import brotli
import pytest
from PIL import Image, ImageDraw

sys.path.insert(0, str(Path(__file__).resolve().parent))

import optimize_db_media as opt  # noqa: E402
import test_media_bundle as base  # noqa: E402
import update_media_references as ref  # noqa: E402

CONTENT_TYPES = [(3, "image/png", "none"), (11, "image/gif", "none"), (12, "text/html", "brotli"),
                 (1, "text/css", "brotli"), (26, "image/webp", "brotli")]
WEBM_TYPES = CONTENT_TYPES + [(30, "video/webm", "none")]


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


SQUARE = (200, 30, 30)


def _animated_gif(width, height, frames=20, loop=0, transparent=False, full_canvas=False) -> bytes:
    """A square moving across a still background drawn like a screen recording:
    rows of dark text-like bars on light grey, which is what most of these GIFs
    are. The detail is what makes a full-canvas frame expensive.

    Saved with disposal=1, Pillow stores every frame after the first as just the
    rectangle that changed - the layout most real GIFs here have. full_canvas
    stores every frame whole (disposal=2), as the old re-save did. transparent
    leaves the background clear instead."""
    background = Image.new("RGBA", (width, height), (0, 0, 0, 0) if transparent else (236, 236, 236, 255))
    if not transparent:
        draw = ImageDraw.Draw(background)
        for row in range(8, height - 8, 14):
            for col in range(8, width - 60, 70):
                draw.rectangle([col, row, col + 50 - (row * 7 + col) % 23, row + 6], fill=(60, 60, 70, 255))
    images = []
    for i in range(frames):
        frame = background.copy()
        x = 4 + i * (width - 40) // frames
        frame.paste(SQUARE + (255,), (x, height // 3, x + 24, height // 3 + 24))
        images.append(frame)
    buffer = io.BytesIO()
    images[0].save(buffer, "GIF", save_all=True, append_images=images[1:], duration=[40] * frames,
                   disposal=2 if (full_canvas or transparent) else 1,
                   **({} if loop is None else {"loop": loop}))
    return buffer.getvalue()


def _displayed(data: bytes) -> list:
    """[(start ms, duration, RGB picture flattened onto white)] for every frame
    an animation shows - what a viewer sees, independent of how it is stored."""
    shown, start = [], 0
    with Image.open(io.BytesIO(data)) as img:
        for i in range(getattr(img, "n_frames", 1)):
            img.seek(i)
            img.load()
            rgba = img.convert("RGBA")
            flat = Image.new("RGB", rgba.size, "white")
            flat.paste(rgba, mask=rgba.getchannel("A"))
            duration = img.info.get("duration", 100)
            shown.append((start, duration, flat))
            start += duration
    return shown


def _same_on_the_timeline(a: bytes, b: bytes) -> bool:
    """Whether b shows exactly a's pictures at a's frame start times, and runs
    as long. Frame counts may differ: gifsicle and libwebp both merge identical
    consecutive frames into one longer frame."""
    shown_a, shown_b = _displayed(a), _displayed(b)
    if sum(d for _s, d, _p in shown_a) != sum(d for _s, d, _p in shown_b):
        return False
    j = 0
    for start, _duration, picture in shown_a:
        while j + 1 < len(shown_b) and shown_b[j + 1][0] <= start:
            j += 1
        if picture.tobytes() != shown_b[j][2].tobytes():
            return False
    return True


def _stored_media(db_path: Path, stem: str) -> tuple:
    """(path, content type, decoded bytes) of the one media row whose path
    starts with `stem` - whatever extension a conversion gave it."""
    with sqlite3.connect(db_path) as conn:
        codec = opt.BrotliCodec(opt.load_dictionary(conn))
        rows = conn.execute("SELECT c.path, c.content, ct.value, ct.compression FROM Content c "
                            "JOIN ContentTypes ct ON c.contentTypeID = ct.id WHERE c.path LIKE ?",
                            (stem + ".%",)).fetchall()
        assert len(rows) == 1, rows
        path, blob, value, compression = rows[0]
        data = codec.decompress(opt.reassemble(conn, path, blob), compression)
        codec.close()
    return path, value, data


def test_a_subrectangle_gif_wider_than_max_width_is_downscaled(tmp_path):
    """ADFA-6586: re-saving every frame full-canvas made this GIF several times
    larger, so the never-larger rule threw the re-save away and the GIF was
    never downscaled at all."""
    db_path = tmp_path / "documentation.db"
    source = _animated_gif(1000, 250)
    _build_database(db_path, [("a/media/demo.gif", "image/gif", source)])

    assert opt.run(base._optimize_cfg(db_path, webp=False)) == 0

    _path, value, stored = _stored_media(db_path, "a/media/demo")
    assert value == "image/gif"
    with Image.open(io.BytesIO(stored)) as img:
        assert img.width == opt.DEFAULTS["max_width"], f"still {img.width} px wide"
    assert len(stored) < len(source)


def test_a_full_canvas_gif_needing_no_resize_shrinks_losslessly(tmp_path):
    """A GIF already within --max-width only goes through gifsicle -O3: no
    Pillow re-save, so nothing it shows can change."""
    db_path = tmp_path / "documentation.db"
    source = _animated_gif(300, 150, full_canvas=True)
    _build_database(db_path, [("a/media/demo.gif", "image/gif", source)])

    assert opt.run(base._optimize_cfg(db_path, webp=False)) == 0

    _path, _value, stored = _stored_media(db_path, "a/media/demo")
    assert len(stored) < len(source) / 2, f"{len(stored)} of {len(source)} bytes"
    assert _same_on_the_timeline(source, stored)


def test_a_gif_that_plays_once_still_plays_once():
    source = _animated_gif(600, 150, loop=None)
    with Image.open(io.BytesIO(source)) as img:
        assert "loop" not in img.info
        out = opt.optimize_animated_gif(img, source, 500, shutil.which("gifsicle"))
    with Image.open(io.BytesIO(out)) as img:
        assert "loop" not in img.info, f"now loops {img.info.get('loop')} time(s)"


def test_a_transparent_gif_leaves_no_trail():
    """Leave-in-place cannot turn a pixel transparent again, so a transparent
    GIF is re-saved with restore-to-background; otherwise the moving square
    would smear across every place it has been."""
    source = _animated_gif(600, 150, transparent=True)
    with Image.open(io.BytesIO(source)) as img:
        out = opt.optimize_animated_gif(img, source, 500, shutil.which("gifsicle"))
    _start, _duration, last = _displayed(out)[-1]
    rgb = last.tobytes()
    red = sum(1 for i in range(0, len(rgb), 3) if rgb[i] > 150 and rgb[i + 1] < 90)
    square = 24 * 24 * (500 / 600) ** 2
    assert red < square * 1.5, f"{red} red pixels in the last frame; one square is about {square:.0f}"


def _gradient_animation(width=300, height=150, frames=16, loop=0, clear_corner=False) -> bytes:
    """A gradient sliding sideways: every frame differs everywhere and holds
    far more than 256 colours - where GIF is at its worst and lossy WebP and
    WebM win. clear_corner leaves a transparent square in each frame."""
    images = []
    for i in range(frames):
        frame = Image.new("RGB", (width, height))
        frame.putdata([((x * 2 + i * 9) % 256, (y * 3 + x) % 256, (x + y + i * 5) % 256)
                       for y in range(height) for x in range(width)])
        if clear_corner:
            frame = frame.convert("RGBA")
            frame.paste((0, 0, 0, 0), (0, 0, 40, 40))
        images.append(frame)
    buffer = io.BytesIO()
    images[0].save(buffer, "GIF", save_all=True, append_images=images[1:], duration=[50] * frames,
                   **({} if loop is None else {"loop": loop}))
    return buffer.getvalue()


def test_webp_turns_an_animated_gif_into_animated_webp(tmp_path):
    db_path = tmp_path / "documentation.db"
    source = _gradient_animation()
    _build_database(db_path, [("a/media/demo.gif", "image/gif", source)])

    assert opt.run(base._optimize_cfg(db_path)) == 0

    path, value, stored = _stored_media(db_path, "a/media/demo")
    assert (path, value) == ("a/media/demo.webp", "image/webp")
    with Image.open(io.BytesIO(stored)) as img:
        assert img.is_animated and img.info.get("loop") == 0
    assert sum(d for _s, d, _p in _displayed(stored)) == sum(d for _s, d, _p in _displayed(source))


def test_indexed_webp_reproduces_a_few_colour_animation_exactly():
    """Quantizing to 64 colours changes nothing in an animation that has fewer,
    and the result is stored lossless - so it shows exactly what the GIF did."""
    source = _animated_gif(300, 150)
    with Image.open(io.BytesIO(source)) as img:
        frames, durations, _alpha = opt.animation_frames(img, 500)
    out = opt.encode_indexed_webp(frames, durations, 0, 64)
    assert _same_on_the_timeline(source, out)


def test_indexed_webp_keeps_colours_only_late_frames_show():
    """The palette comes from frames spread over the whole animation. Taking
    the first 16 of these 31 lost the green banner of frames 16-30, which came
    out dark grey, and the blue mark only the last frame has."""
    background = Image.new("RGB", (300, 150), (236, 236, 236))
    ImageDraw.Draw(background).rectangle([8, 100, 250, 106], fill=(60, 60, 70))
    images = []
    for i in range(31):
        frame = background.copy()
        frame.paste(SQUARE, (4 + i * 8, 50, 28 + i * 8, 74))
        if i >= 16:
            frame.paste((40, 170, 60), (0, 0, 300, 20))
        if i == 30:
            frame.paste((30, 60, 200), (270, 120, 290, 140))
        images.append(frame)
    buffer = io.BytesIO()
    images[0].save(buffer, "GIF", save_all=True, append_images=images[1:], duration=[40] * 31, loop=2)
    source = buffer.getvalue()
    with Image.open(io.BytesIO(source)) as img:
        frames, durations, _alpha = opt.animation_frames(img, 500)
    out = opt.encode_indexed_webp(frames, durations, opt.webp_loop(img), 64)
    assert _same_on_the_timeline(source, out)


def test_webp_loop_counts_plays_where_gif_counts_repeats():
    def loop_of(**info):
        image = Image.new("RGB", (1, 1))
        image.info.update(info)
        return opt.webp_loop(image)

    assert loop_of() == 1, "a GIF with no loop count plays once"
    assert loop_of(loop=0) == 0, "forever"
    assert loop_of(loop=2) == 3, "two repeats after the first play"
    assert loop_of(loop=65535) == 65535, "WebP's count is 16 bits too"


def test_a_gif_repeating_65535_times_still_converts():
    """GIF's largest count, 65,535 repeats, is 65,536 plays: one more than
    WebP's 16 bits hold. libwebp refused it, and the row failed with the GIF
    candidate lost too."""
    source = _gradient_animation(loop=65535)
    with Image.open(io.BytesIO(source)) as img:
        out, _value = opt.optimize_animation(img, source, "demo.gif", base._optimize_cfg(Path("unused.db")),
                                             shutil.which("gifsicle"))
    with Image.open(io.BytesIO(out)) as img:
        assert img.info.get("loop") == 65535


VIDEO_RENAMES = {"spring.gif": "spring.webm", "hero.png": "hero.webp"}
VIDEO_PATTERN = ref.build_pattern(VIDEO_RENAMES)


def _rewrite(text: str) -> str:
    rewritten, sites = ref.rewrite_text(text, VIDEO_PATTERN, VIDEO_RENAMES)
    assert ref.undo_rewrite(rewritten, sites, VIDEO_RENAMES) == text, "undo must give back the original exactly"
    return rewritten


def test_an_img_showing_a_converted_animation_becomes_a_video():
    assert _rewrite('<p>x</p><img alt="A spring" src="media/spring.gif" style="margin:0" width="300"/><p>y</p>') == (
        '<p>x</p><video autoplay loop muted playsinline role="img" aria-label="A spring" src="media/spring.webm" '
        'style="margin:0" width="300"></video><p>y</p>')


def test_a_json_escaped_img_keeps_its_escaping():
    """Kotlin's templated pages carry their markup inside JSON strings."""
    assert _rewrite('{"html":"<img src=\\"/k/html/images/spring.gif\\" alt=\\"Kotlin\\" width=\\"600\\"/>"}') == (
        '{"html":"<video autoplay loop muted playsinline src=\\"/k/html/images/spring.webm\\" '
        'role=\\"img\\" aria-label=\\"Kotlin\\" width=\\"600\\"></video>"}')


def test_an_empty_alt_hides_the_video_from_screen_readers():
    assert _rewrite('<img alt="" src="spring.gif">') == (
        '<video autoplay loop muted playsinline aria-hidden="true" src="spring.webm"></video>')


def test_other_renamed_files_in_the_same_tag_are_rewritten_with_it():
    text = '<img preview-src="/i/hero.png" src="/i/spring.gif"/> and <img src="/i/hero.png">'
    rewritten, sites = ref.rewrite_text(text, VIDEO_PATTERN, VIDEO_RENAMES)
    assert rewritten == ('<video autoplay loop muted playsinline preview-src="/i/hero.webp" '
                         'src="/i/spring.webm"></video> and <img src="/i/hero.webp">')
    assert [type(old) for _offset, old in sites] == [ref.TagEdit, str]
    assert ref.undo_rewrite(rewritten, sites, VIDEO_RENAMES) == text


def test_an_unquoted_filename_is_renamed_with_its_tag():
    """An unquoted value that is only a filename has nothing before the name
    of its own, and the pattern needs a separator there, so <img src=spring.gif>
    became a <video> still naming the deleted GIF - in any attribute, at the
    end of the tag or not."""
    for text, video in [
        ('<img src=spring.gif>', '<video autoplay loop muted playsinline src=spring.webm></video>'),
        ('<img src=spring.gif alt="x">',
         '<video autoplay loop muted playsinline src=spring.webm role="img" aria-label="x"></video>'),
        ('<img data-src0=spring.gif width=300>',
         '<video autoplay loop muted playsinline data-src0=spring.webm width=300></video>'),
        ('<img src="spring.gif" data-src0=hero.png>',
         '<video autoplay loop muted playsinline src="spring.webm" data-src0=hero.webp></video>'),
        ('{"html":"<img src=spring.gif alt=\\"x\\">"}',
         '{"html":"<video autoplay loop muted playsinline src=spring.webm role=\\"img\\" aria-label=\\"x\\">'
         '</video>"}'),
    ]:
        assert _rewrite(text) == video


def test_a_video_referenced_outside_an_img_is_refused():
    for text in ('div{background:url("media/spring.gif")}', '<a href="spring.gif">play</a>',
                 '<img srcset="spring.gif 2x" src="spring.gif">'):
        with pytest.raises(ref.UnconvertibleReference):
            ref.rewrite_text(text, VIDEO_PATTERN, VIDEO_RENAMES)


def test_tag_sites_survive_the_reference_log(tmp_path):
    """A tag's text is free to hold every separator the log reserves."""
    tag = ref.TagEdit('<img alt="a, b: c\td\ne" src="spring.gif">',
                      '<video autoplay loop muted playsinline aria-label="a, b: c\td\ne" src="spring.webm"></video>')
    ref.write_reference_log(tmp_path, [("before", "after", "p.html", [(3, "x.png"), (40, tag)])])
    (record,) = ref.read_reference_log(tmp_path / ref.BUNDLE_REFERENCES)
    assert record == ("before", "after", "p.html", [(3, "x.png"), (40, tag)])


def test_the_rename_map_follows_a_gif_into_video(tmp_path):
    before_path, after_path = tmp_path / "before.db", tmp_path / "after.db"
    _build_database(before_path, [("a/media/spring.gif", "image/gif", b"GIF89a")], WEBM_TYPES)
    _build_database(after_path, [("a/media/spring.webm", "video/webm", b"\x1aE\xdf\xa3")], WEBM_TYPES)
    with sqlite3.connect(after_path) as after, sqlite3.connect(before_path) as before:
        assert ref.build_rename_map(after, before) == {"spring.gif": "spring.webm"}


def test_the_rename_map_ignores_a_new_files_chunk_continuations(tmp_path):
    """conversations-bubble.gif became a 1.09 MB WebM, stored as "...webm" and
    "...webm-1"; counting the continuation as a second replacement made the
    rename ambiguous and left its page naming the deleted GIF."""
    before_path, after_path = tmp_path / "before.db", tmp_path / "after.db"
    _build_database(before_path, [("a/media/bubble.gif", "image/gif", b"GIF89a")], WEBM_TYPES)
    _build_database(after_path, [("a/media/bubble.webm", "video/webm", b"\x00" * opt.CHUNK_SIZE),
                                 ("a/media/bubble.webm-1", "video/webm", b"\x00" * 5)], WEBM_TYPES)
    with sqlite3.connect(after_path) as after, sqlite3.connect(before_path) as before:
        assert ref.build_rename_map(after, before) == {"bubble.gif": "bubble.webm"}


SAME_NAMED_PAGES = [("a/page.html", "text/html", b'<img alt="A" src="x/foo.gif">'),
                    ("b/page.html", "text/html", b'<img alt="B" src="y/foo.gif">')]
SAME_NAMED_GIFS = [("a/x/foo.gif", "image/gif", b"GIF89a-a"), ("b/y/foo.gif", "image/gif", b"GIF89a-b")]


def test_a_filename_converted_two_ways_stops_the_rewrite(tmp_path):
    """References are matched by filename, so one rename for foo.gif sent b's
    page to y/foo.webm, which only a/x has - or, where b's GIF was left alone,
    to y/foo.webp. Both tools exited 0. The run is refused instead, naming
    both paths, with nothing written."""
    cases = {"two-outputs": [("a/x/foo.webm", "video/webm", b"\x1aE\xdf\xa3"),
                             ("b/y/foo.webp", "image/webp", b"RIFF-b")],
             "one-left-alone": [("a/x/foo.webp", "image/webp", b"RIFF-a"), SAME_NAMED_GIFS[1]]}
    for label, media in cases.items():
        (tmp_path / label).mkdir()
        before_path, db_path = tmp_path / label / "before.db", tmp_path / label / "documentation.db"
        _build_database(before_path, SAME_NAMED_PAGES + SAME_NAMED_GIFS, WEBM_TYPES)
        _build_database(db_path, SAME_NAMED_PAGES + media, WEBM_TYPES)
        snapshot = base._snapshot(db_path)
        errors = io.StringIO()
        with contextlib.redirect_stderr(errors):
            code = ref.run({"db_path": db_path, "before": before_path, "dry_run": False, "workers": 2,
                            "verbose": False, "save_originals": None, "restore_originals": None})
        assert base._snapshot(db_path) == snapshot, f"{label}: the pages were rewritten"
        assert code == 1, label
        assert "a/x/foo.gif" in errors.getvalue() and "b/y/foo.gif" in errors.getvalue(), errors.getvalue()


def test_same_named_files_converted_alike_share_one_rename(tmp_path):
    before_path, after_path = tmp_path / "before.db", tmp_path / "after.db"
    _build_database(before_path, SAME_NAMED_PAGES + SAME_NAMED_GIFS, WEBM_TYPES)
    _build_database(after_path, SAME_NAMED_PAGES + [("a/x/foo.webm", "video/webm", b"\x1aE\xdf\xa3"),
                                                    ("b/y/foo.webm", "video/webm", b"\x1aE\xdf\xa4")], WEBM_TYPES)
    with sqlite3.connect(after_path) as after, sqlite3.connect(before_path) as before:
        assert ref.build_rename_map(after, before) == {"foo.gif": "foo.webm"}


def test_the_optimizer_is_told_which_gifs_cannot_become_video(tmp_path):
    db_path = tmp_path / "documentation.db"
    _build_database(db_path, [
        ("a/one.html", "text/html", b'<img alt="ok" src="media/spring.gif">'),
        ("a/site.css", "text/css", b'body{background:url("media/spinner.gif")}'),
        ("a/two.html", "text/html", b'<img srcset="media/retina.gif 2x" src="media/retina.gif">'),
    ])
    with sqlite3.connect(db_path) as conn:
        codec = opt.BrotliCodec(opt.load_dictionary(conn))
        blocked = ref.names_not_convertible_to_video(conn, codec, {"spring.gif", "spinner.gif", "retina.gif"}, 2)
        codec.close()
    assert blocked == {"spinner.gif", "retina.gif"}


def _probe(data: bytes, tmp_path: Path) -> tuple:
    """(codec name, duration in ms, sorted frame start times in ms) of a WebM."""
    video = tmp_path / "probe.webm"
    video.write_bytes(data)

    def ffprobe(*entries):
        return subprocess.run(["ffprobe", "-v", "error", "-select_streams", "v:0", "-show_entries", *entries,
                               "-of", "default=nw=1", str(video)], capture_output=True, text=True, check=True).stdout
    fields = dict(line.split("=", 1) for line in ffprobe("stream=codec_name:format=duration").split())
    starts = sorted(round(float(line.split("=", 1)[1]) * 1000) for line in ffprobe("packet=pts_time").split())
    return fields["codec_name"], round(float(fields["duration"]) * 1000), starts


def _uneven_animation() -> bytes:
    """Frames of 30, 70 and 130 ms and a 2,190 ms pause at the end - delays no
    fixed frame rate lands on, which is what bounce-demo.gif's are."""
    delays = [30, 70, 130, 30, 70, 130, 30, 2190]
    images = []
    for i, _delay in enumerate(delays):
        frame = Image.new("RGB", (200, 100))
        frame.putdata([((x * 3 + i * 31) % 256, (y * 5 + x) % 256, (x + y + i * 17) % 256)
                       for y in range(100) for x in range(200)])
        images.append(frame)
    buffer = io.BytesIO()
    images[0].save(buffer, "GIF", save_all=True, append_images=images[1:], duration=delays, loop=0)
    return buffer.getvalue()


def test_a_webm_keeps_every_frames_own_timing(tmp_path):
    """Each frame starts exactly where the GIF's does and the last one keeps
    its pause: at a fixed frame rate, every one of these would be off."""
    source = _uneven_animation()
    with Image.open(io.BytesIO(source)) as img:
        frames, durations, _alpha = opt.animation_frames(img, 500)
    codec, total, starts = _probe(opt.encode_webm(frames, durations, 32, shutil.which("ffmpeg")), tmp_path)
    shown = _displayed(source)
    assert (codec, total) == ("vp9", sum(d for _s, d, _p in shown))
    assert starts == [start for start, _d, _p in shown]


def test_a_webm_encodes_to_the_same_bytes_every_time():
    """Without bitexact, ffmpeg's muxer writes a random 8-byte track ID, so
    two runs differ and so does every database rebuilt from them."""
    with Image.open(io.BytesIO(_uneven_animation())) as img:
        frames, durations, _alpha = opt.animation_frames(img, 500)
    ffmpeg = shutil.which("ffmpeg")
    assert opt.encode_webm(frames, durations, 32, ffmpeg) == opt.encode_webm(frames, durations, 32, ffmpeg)


def test_a_webm_encodes_to_the_same_bytes_on_one_cpu_as_on_many():
    """libvpx takes a thread per CPU it may use unless told how many, and with
    one its row multithreading is off, so a 1-CPU machine or container wrote
    other bytes. ffmpeg inherits the affinity set here."""
    if not hasattr(os, "sched_setaffinity"):
        pytest.skip("needs os.sched_setaffinity, which this platform lacks")
    cpus = os.sched_getaffinity(0)
    if len(cpus) < 2:
        pytest.skip("needs two or more CPUs to compare one against")
    with Image.open(io.BytesIO(_uneven_animation())) as img:
        frames, durations, _alpha = opt.animation_frames(img, 500)
    ffmpeg = shutil.which("ffmpeg")
    os.sched_setaffinity(0, {min(cpus)})
    try:
        one_cpu = opt.encode_webm(frames, durations, 32, ffmpeg)
    finally:
        os.sched_setaffinity(0, cpus)
    assert one_cpu == opt.encode_webm(frames, durations, 32, ffmpeg)


def _webm_cfg(db_path: Path, **overrides) -> dict:
    return base._optimize_cfg(db_path, webm=True, **overrides)


def test_webm_turns_an_animated_gif_into_vp9_at_its_own_timing(tmp_path):
    db_path = tmp_path / "documentation.db"
    source = _gradient_animation()
    _build_database(db_path, [("a/media/demo.gif", "image/gif", source),
                              ("a/page.html", "text/html", b'<img alt="Demo" src="media/demo.gif">')], WEBM_TYPES)

    assert opt.run(_webm_cfg(db_path)) == 0

    path, value, stored = _stored_media(db_path, "a/media/demo")
    assert (path, value) == ("a/media/demo.webm", "video/webm")
    assert _probe(stored, tmp_path)[:2] == ("vp9", sum(d for _s, d, _p in _displayed(source)))


def test_webm_needs_its_content_type(tmp_path):
    db_path = tmp_path / "documentation.db"
    _build_database(db_path, [("a/media/demo.gif", "image/gif", _gradient_animation())])
    before = base._snapshot(db_path)
    assert opt.run(_webm_cfg(db_path)) == 1
    assert base._snapshot(db_path) == before


def test_a_gif_that_cannot_become_video_unnoticed_does_not(tmp_path):
    """Transparency (no alpha in VP9 as encoded), not looping forever (the
    <video> always loops), and a reference no <video> can replace each keep a
    GIF out of WebM - it converts to WebP instead."""
    db_path = tmp_path / "documentation.db"
    plays_once = _gradient_animation(loop=None)
    with Image.open(io.BytesIO(plays_once)) as img:
        assert "loop" not in img.info
    _build_database(db_path, [
        ("a/media/clear.gif", "image/gif", _gradient_animation(clear_corner=True)),
        ("a/media/once.gif", "image/gif", plays_once),
        ("a/media/styled.gif", "image/gif", _gradient_animation(frames=12)),
        ("a/site.css", "text/css", b'body{background:url("media/styled.gif")}'),
    ], WEBM_TYPES)

    assert opt.run(_webm_cfg(db_path)) == 0

    for stem in ("a/media/clear", "a/media/once", "a/media/styled"):
        _path, value, _data = _stored_media(db_path, stem)
        assert value != "video/webm", f"{stem} became WebM"


def test_a_gif_that_repeats_a_set_number_of_times_never_becomes_webm():
    """The <video> written for a WebM always loops, so only a GIF that loops
    forever qualifies. With only "plays once" tested, `loop != 1` in place of
    `loop == 0` passed every test while making a GIF that repeats twice loop
    forever."""
    cfg = {**base._optimize_cfg(Path("unused.db"), webp=False), "webm": True}
    gifsicle, ffmpeg = shutil.which("gifsicle"), shutil.which("ffmpeg")

    def output_type(loop):
        source = _gradient_animation(loop=loop)
        with Image.open(io.BytesIO(source)) as img:
            return opt.optimize_animation(img, source, "demo.gif", cfg, gifsicle, ffmpeg)[1]

    assert output_type(0) == "video/webm", "looping forever, this animation is smallest as WebM"
    for loop in (1, 2, 65535):
        assert output_type(loop) != "video/webm", f"a GIF repeating {loop} time(s) became a <video> that loops forever"


def test_convert_to_webm_rewrite_then_restore_reproduces_the_database(tmp_path):
    """test_media_bundle's round trip, with video in it: a GIF becomes WebM,
    the <img> showing it - plain and JSON-escaped - becomes a <video>, and
    putting both halves back gives exactly the database it started as."""
    db_path, bundle = tmp_path / "documentation.db", tmp_path / "bundle"
    _build_database(db_path, [
        ("a/media/demo.gif", "image/gif", _gradient_animation()),
        ("k/images/kdemo.gif", "image/gif", _gradient_animation(frames=12)),
        ("a/page.html", "text/html", b'<p>Watch:</p><img alt="A demo, slowly" src="media/demo.gif" width="300"/>'),
        ("k/page.html", "text/html",
         b'{"html":"<img src=\\"/k/images/kdemo.gif\\" alt=\\"K\\" width=\\"600\\"/>"}', 2),
    ], WEBM_TYPES)
    before = base._snapshot(db_path)
    pre_conversion = tmp_path / "before.db"
    shutil.copy(db_path, pre_conversion)

    assert opt.run(_webm_cfg(db_path, save_originals=bundle)) == 0
    reference_cfg = {"db_path": db_path, "before": pre_conversion, "dry_run": False, "workers": 2,
                     "verbose": False, "save_originals": bundle, "restore_originals": None}
    assert ref.run(reference_cfg) == 0

    _path, _value, page = _stored_media(db_path, "a/page")
    assert page == (b'<p>Watch:</p><video autoplay loop muted playsinline role="img" aria-label="A demo, slowly" '
                    b'src="media/demo.webm" width="300"></video>')
    _path, _value, kotlin = _stored_media(db_path, "k/page")
    assert b'<video autoplay loop muted playsinline src=\\"/k/images/kdemo.webm\\"' in kotlin
    with sqlite3.connect(db_path) as conn:
        assert conn.execute("SELECT templateId FROM Content WHERE path = 'k/page.html'").fetchone() == (2,)

    assert opt.restore_originals(base._optimize_cfg(db_path, restore_originals=bundle)) == 0
    assert ref.restore_references({**reference_cfg, "before": None, "save_originals": None,
                                   "restore_originals": bundle}) == 0
    assert base._snapshot(db_path) == before


def test_an_unquoted_src_becomes_the_webm_and_restores_exactly(tmp_path):
    """<img src=demo.gif> through the whole pipeline: the run used to write a
    <video> naming the deleted GIF and exit 0. Restoring must give back the
    unquoted original byte for byte."""
    db_path, bundle, pre_conversion = tmp_path / "documentation.db", tmp_path / "bundle", tmp_path / "before.db"
    _build_database(db_path, [("a/media/demo.gif", "image/gif", _gradient_animation()),
                              ("a/media/page.html", "text/html", b'<img src=demo.gif alt="Demo">')], WEBM_TYPES)
    before = base._snapshot(db_path)
    shutil.copy(db_path, pre_conversion)

    assert opt.run(_webm_cfg(db_path, save_originals=bundle)) == 0
    reference_cfg = {"db_path": db_path, "before": pre_conversion, "dry_run": False, "workers": 2,
                     "verbose": False, "save_originals": bundle, "restore_originals": None}
    assert ref.run(reference_cfg) == 0
    assert _stored_media(db_path, "a/media/page")[2] == (
        b'<video autoplay loop muted playsinline src=demo.webm role="img" aria-label="Demo"></video>')

    assert opt.restore_originals(base._optimize_cfg(db_path, restore_originals=bundle)) == 0
    assert ref.restore_references({**reference_cfg, "before": None, "save_originals": None,
                                   "restore_originals": bundle}) == 0
    assert base._snapshot(db_path) == before


def test_a_webm_keeps_its_manifest_entry_and_provenance(tmp_path):
    """The next run has to recognise the WebM as this tool's output and know
    which GIF it came from - or a re-inserted GIF lands beside it under a
    de-conflicted name nothing links to."""
    db_path = tmp_path / "documentation.db"
    first, second = tmp_path / "first.tsv", tmp_path / "second.tsv"
    _build_database(db_path, [("a/media/demo.gif", "image/gif", _gradient_animation()),
                              ("a/page.html", "text/html", b'<img src="media/demo.gif">')], WEBM_TYPES)
    assert opt.run(_webm_cfg(db_path, manifest_out=first)) == 0
    assert opt.read_manifest(first)["a/media/demo.webm"][2] == "a/media/demo.gif"
    assert opt.run(_webm_cfg(db_path, manifest_in=first, manifest_out=second)) == 0
    assert opt.read_manifest(second) == opt.read_manifest(first)


def _convert_link_and_reinsert(tmp_path: Path, reinserted: bytes) -> tuple:
    """demo.gif converted to demo.webm and its page rewritten to a <video>,
    then a GIF put back at demo.gif by an author: (database, first manifest)."""
    db_path, first, pre_conversion = tmp_path / "documentation.db", tmp_path / "first.tsv", tmp_path / "before.db"
    _build_database(db_path, [("a/media/demo.gif", "image/gif", _gradient_animation()),
                              ("a/page.html", "text/html", b'<img alt="Demo" src="media/demo.gif">')], WEBM_TYPES)
    shutil.copy(db_path, pre_conversion)
    assert opt.run(_webm_cfg(db_path, manifest_out=first)) == 0
    assert ref.run({"db_path": db_path, "before": pre_conversion, "dry_run": False, "workers": 2,
                    "verbose": False, "save_originals": None, "restore_originals": None}) == 0
    assert b'src="media/demo.webm"' in _stored_media(db_path, "a/page")[2]
    conn = sqlite3.connect(db_path)
    conn.execute("INSERT INTO Content (path, languageID, content, contentTypeID) VALUES ('a/media/demo.gif', 1, ?, 11)",
                 (reinserted,))
    conn.commit()
    conn.close()
    return db_path, first


def test_a_reinserted_gif_that_cannot_become_its_webm_again_is_left_alone(tmp_path):
    """With transparency it can only become demo.webp. Writing that left the
    page's demo.webm stale and demo.webp linked from nowhere, with exit 0."""
    db_path, first = _convert_link_and_reinsert(tmp_path, _gradient_animation(clear_corner=True))
    before, second = base._snapshot(db_path), tmp_path / "second.tsv"
    errors = io.StringIO()
    with contextlib.redirect_stderr(errors):
        code = opt.run(_webm_cfg(db_path, manifest_in=first, manifest_out=second))
    assert base._snapshot(db_path) == before, "a second output was written beside a/media/demo.webm"
    assert code == 1
    assert "a/media/demo.webm" in errors.getvalue() and "references" in errors.getvalue(), errors.getvalue()
    assert "a/media/demo.gif" not in opt.read_manifest(second), "an errored row must be retried next time"


def test_a_reinserted_gif_that_becomes_webm_again_replaces_its_webm(tmp_path):
    db_path, first = _convert_link_and_reinsert(tmp_path, _gradient_animation(frames=12))
    conn = sqlite3.connect(db_path)
    (old_webm,) = conn.execute("SELECT content FROM Content WHERE path = 'a/media/demo.webm'").fetchone()
    conn.close()
    assert opt.run(_webm_cfg(db_path, manifest_in=first)) == 0
    path, value, data = _stored_media(db_path, "a/media/demo")
    assert (path, value) == ("a/media/demo.webm", "video/webm") and data != old_webm


if __name__ == "__main__":
    sys.exit(base._main(globals()))
