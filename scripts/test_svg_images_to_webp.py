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
test_svg_images_to_webp.py

Covers svg_images_to_webp.py: the rule that picks each image's encoding, the
checks that stop a bad re-encode, that nothing but the data: URI changes, and
end-to-end runs over real SQLite databases (dry run, rerun, chunked rows, the
database's own dictionary, refusals).

Run it either way:

    uv run scripts/test_svg_images_to_webp.py
    uv run --with brotli --with Pillow --with pytest pytest scripts/test_svg_images_to_webp.py
"""
from __future__ import annotations

import base64
import io
import random
import re
import shutil
import sqlite3
import struct
import sys
import tempfile
import traceback
import zlib
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path

import brotli
from PIL import ExifTags, Image, ImageCms, ImageDraw, ImageFilter

sys.path.insert(0, str(Path(__file__).resolve().parent))

import svg_images_to_webp as tool  # noqa: E402
from optimize_db_media import CHUNK_SIZE, BrotliCodec, reassemble  # noqa: E402
from update_media_references import replace_row  # noqa: E402


# --- images the rule is meant for ----------------------------------------------------------------

def screenshot() -> Image.Image:
    """Opaque and smooth, like a phone screenshot: lossy WebP holds it well above the threshold."""
    img = Image.new("RGB", (480, 360))
    d = ImageDraw.Draw(img)
    for y in range(360):
        d.line([(0, y), (480, y)], fill=(40 + y // 3, 90 + y // 4, 160 + y // 6))
    for i in range(6):
        d.ellipse([30 + i * 70, 60 + (i % 2) * 120, 110 + i * 70, 140 + (i % 2) * 120],
                  fill=(230, 200 - i * 20, 90 + i * 25))
    return img.filter(ImageFilter.GaussianBlur(3))


def code_text() -> Image.Image:
    """Opaque coloured text, like an editor screenshot: lossy WebP blurs it, a palette keeps it."""
    img = Image.new("RGB", (370, 78), (255, 255, 255))
    d = ImageDraw.Draw(img)
    for i, (word, colour) in enumerate([("import", (220, 40, 40)), ("android.os.Bundle;", (230, 120, 20)),
                                        ("val x = 3", (30, 90, 200))]):
        d.text((8 + i * 4, 6 + i * 22), word * 2, fill=colour, font_size=18)
    return img


def brand() -> Image.Image:
    """Fine colour detail with soft transparency, like brand art: only lossless counts."""
    random.seed(7)
    img = Image.new("RGBA", (400, 220), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)
    for x in range(0, 400, 2):
        d.line([(x, 0), (x, 220)], fill=(random.randrange(256), random.randrange(256), random.randrange(256), 255))
    mask = Image.new("L", (400, 220), 0)
    ImageDraw.Draw(mask).ellipse([20, 10, 380, 210], fill=255)
    img.putalpha(mask.filter(ImageFilter.GaussianBlur(8)))
    return img


def png(img: Image.Image, level: int = 1, **params) -> bytes:
    buf = io.BytesIO()
    img.save(buf, "PNG", compress_level=level, **params)
    return buf.getvalue()


def jpeg(img: Image.Image, **params) -> bytes:
    buf = io.BytesIO()
    img.save(buf, "JPEG", quality=95, **params)
    return buf.getvalue()


def with_chunk(data: bytes, kind: bytes, payload: bytes) -> bytes:
    """A PNG with one more chunk right after IHDR, which Pillow cannot write itself (gAMA, sRGB)."""
    chunk = struct.pack(">I", len(payload)) + kind + payload + struct.pack(">I", zlib.crc32(kind + payload))
    return data[:33] + chunk + data[33:]


def gamma(value: int) -> bytes:
    return struct.pack(">I", value)


def uri(raw: bytes, wrap: int = 0, mime: bytes = b"png") -> bytes:
    data = base64.b64encode(raw)
    if wrap:
        data = b"\n".join(data[i:i + wrap] for i in range(0, len(data), wrap))
    return b"data:image/" + mime + b";base64," + data


def svg(*uris: bytes) -> bytes:
    images = b"".join(b'<symbol id="i%d"><image width="10" height="10" xlink:href="%s"/></symbol>' % (n, u)
                      for n, u in enumerate(uris))
    return (b'<svg viewBox="0 0 10 10" xmlns="http://www.w3.org/2000/svg" '
            b'xmlns:xlink="http://www.w3.org/1999/xlink"><defs>' + images +
            b'<filter id="f"><feGaussianBlur stdDeviation="4"/></filter></defs>'
            b'<path d="m0 0 10 10" stroke="#000" filter="url(#f)"/><use xlink:href="#i0"/></svg>')


def outside_uris(data: bytes) -> list:
    """The SVG with every raster data: URI cut out: what must stay byte for byte."""
    return re.split(rb"data:image/[a-z]+;base64,[A-Za-z0-9+/=]+(?:\s+[A-Za-z0-9+/=]+)*", data)


def webp_images(data: bytes) -> list:
    return [Image.open(io.BytesIO(base64.b64decode(m.group(1))))
            for m in re.finditer(rb"data:image/webp;base64,([A-Za-z0-9+/=]+)", data)]


# --- the rule ------------------------------------------------------------------------------------

def test_choose_takes_the_smallest_candidate_that_counts():
    options = [("lossless", b"x" * 50, 99.0), ("lossy", b"x" * 20, 40.0), ("palette", b"x" * 30, 38.0)]
    assert tool.choose(100, options)[0] == "lossy"


def test_choose_ignores_a_lossy_candidate_below_the_threshold():
    options = [("lossless", b"x" * 50, 99.0), ("lossy", b"x" * 10, 34.9), ("palette", b"x" * 30, 20.0)]
    assert tool.choose(100, options)[0] == "lossless"


def test_choose_counts_a_candidate_at_exactly_the_threshold():
    options = [("lossless", b"x" * 50, 99.0), ("palette", b"x" * 30, tool.MIN_PSNR)]
    assert tool.choose(100, options)[0] == "palette"


def test_choose_keeps_the_image_when_nothing_is_smaller():
    options = [("lossless", b"x" * 100, 99.0), ("lossy", b"x" * 100, 50.0)]
    assert tool.choose(100, options) is None
    assert tool.choose(101, options) is not None


def test_each_kind_of_image_gets_the_encoding_the_rule_was_made_for():
    for make, kind in ((screenshot, "lossy"), (code_text, "palette"), (brand, "lossless")):
        img = make()
        assert tool.choose(len(png(img)), tool.candidates(img))[0] == kind, make.__name__


def test_a_palette_is_offered_only_for_an_opaque_image():
    assert "palette" not in [k for k, *_ in tool.candidates(brand())]
    assert "palette" in [k for k, *_ in tool.candidates(code_text())]
    assert "palette" in [k for k, *_ in tool.candidates(code_text().convert("RGBA"))]  # opaque alpha channel


def test_quality_is_judged_on_a_dark_page_too():
    # White pixels at two different opacities look the same on a white page and differ on a black one.
    original = Image.new("RGBA", (8, 8), (255, 255, 255, 200))
    candidate = Image.new("RGBA", (8, 8), (255, 255, 255, 100))
    assert tool.visible_psnr(original, candidate) < 20


def test_quality_is_judged_on_a_light_page_too():
    # Black pixels at two different opacities look the same on a black page and differ on a white one.
    original = Image.new("RGBA", (8, 8), (0, 0, 0, 200))
    candidate = Image.new("RGBA", (8, 8), (0, 0, 0, 100))
    assert tool.visible_psnr(original, candidate) < 20


def test_visible_psnr_on_known_errors():
    grey = Image.new("RGB", (8, 8), (100, 100, 100))
    # An error of 1 in every channel is an MSE of 1: 10 log10(255^2) = 48.13 dB.
    assert abs(tool.visible_psnr(grey, Image.new("RGB", (8, 8), (101, 101, 101))) - 48.13) < 0.01
    # In one channel of three, an MSE of 1/3: 4.77 dB more.
    assert abs(tool.visible_psnr(grey, Image.new("RGB", (8, 8), (101, 100, 100))) - 52.90) < 0.01


def test_the_candidates_use_the_settings_the_docstring_gives():
    # Pinned byte for byte: the 35 dB rule alone would let a lower quality or fewer colours through for as long as the
    # images stayed above it, and this rule was measured at these settings. (The dither argument needs no pin: Pillow's
    # quantize() ignores it unless it is given a palette.)
    img = screenshot()

    def encoded(image: Image.Image, **params) -> bytes:
        buf = io.BytesIO()
        image.save(buf, "WEBP", method=6, **params)
        return buf.getvalue()

    got = {kind: data for kind, data, _quality in tool.candidates(img)}
    assert got["lossy"] == encoded(img, quality=80)
    assert got["lossless"] == encoded(img, lossless=True, exact=True, quality=100)
    palette = img.quantize(256, method=Image.Quantize.MEDIANCUT, dither=Image.Dither.NONE).convert("RGB")
    assert got["palette"] == encoded(palette, lossless=True, exact=True, quality=100)


def test_a_lossless_candidate_that_changes_pixels_is_refused():
    real = tool.webp
    tool.webp = lambda img, lossless: real(img, lossless=False)
    try:
        try:
            tool.candidates(screenshot())
        except tool.Refused as exc:
            assert "same pixels" in str(exc)
        else:
            raise AssertionError("not refused")
    finally:
        tool.webp = real


def test_a_candidate_of_another_size_is_refused():
    real = tool.webp
    tool.webp = lambda img, lossless: real(img.resize((img.width // 2, img.height // 2)), lossless)
    try:
        try:
            tool.candidates(code_text())
        except tool.Refused as exc:
            assert "decodes to" in str(exc)
        else:
            raise AssertionError("not refused")
    finally:
        tool.webp = real


# --- rewriting one SVG ---------------------------------------------------------------------------

def test_only_the_data_uri_changes():
    old = svg(uri(png(screenshot()), wrap=76), uri(png(brand())))
    old = old.replace(b'"/></symbol>', b'  "/></symbol>', 1)  # whitespace after the payload stays
    new, images = tool.rewrite(old, {})
    assert outside_uris(new) == outside_uris(old)
    assert [k for k, *_ in images] == ["lossy", "lossless"]
    assert new.count(b"data:image/webp;base64,") == 2 and b"data:image/png" not in new


def test_identical_images_get_identical_bytes():
    raw = png(code_text())
    cache = {}
    first, _ = tool.rewrite(svg(uri(raw), uri(raw, wrap=60)), cache)
    second, _ = tool.rewrite(svg(uri(raw)), cache)
    uris = re.findall(rb"data:image/webp;base64,[A-Za-z0-9+/=]+", first + second)
    assert len(uris) == 3 and len(set(uris)) == 1


def test_webp_and_animated_images_are_left_alone():
    buf = io.BytesIO()
    code_text().save(buf, "WEBP", lossless=True)
    already = b"data:image/webp;base64," + base64.b64encode(buf.getvalue())
    frames = [screenshot(), code_text().resize((480, 360))]
    apng = io.BytesIO()
    frames[0].save(apng, "PNG", save_all=True, append_images=frames[1:])
    old = svg(already, uri(apng.getvalue()))
    new, images = tool.rewrite(old, {})
    assert new == old and [k for k, *_ in images] == ["left: animated"]


# What the pixel checks cannot see. Each image below would re-encode as lossless or lossy WebP with its checks passing,
# while the browser drew it differently afterwards (measured in Chrome in the review of PR #52), so it stays as it is.

def _left_alone(raw: bytes) -> str:
    new_uri, kind, _quality = tool.reencode(raw)
    assert new_uri is None, f"re-encoded as {kind}"
    assert kind.startswith(tool.LEFT), kind
    return kind[len(tool.LEFT):]


def test_an_image_with_a_colour_profile_is_left_as_it_is():
    profile = ImageCms.ImageCmsProfile(ImageCms.createProfile("sRGB")).tobytes()
    assert _left_alone(png(screenshot(), icc_profile=profile)) == "an ICC colour profile"


def test_an_image_with_a_gamma_other_than_srgbs_is_left_as_it_is():
    plain = png(screenshot())
    assert _left_alone(with_chunk(plain, b"gAMA", gamma(100000))) == "a gamma of 1.0 with no sRGB chunk"
    # sRGB's own gamma, or any gamma beside an sRGB chunk (which a browser follows instead), changes nothing.
    for raw in (with_chunk(plain, b"gAMA", gamma(45455)), with_chunk(plain, b"gAMA", gamma(45454)),
                with_chunk(with_chunk(plain, b"gAMA", gamma(100000)), b"sRGB", b"\x00")):
        assert tool.reencode(raw)[0] is not None


def test_an_image_with_an_exif_orientation_is_left_as_it_is():
    exif = Image.Exif()
    exif[ExifTags.Base.Orientation] = 6
    assert _left_alone(jpeg(screenshot(), exif=exif.tobytes())) == "EXIF orientation 6"
    exif[ExifTags.Base.Orientation] = 1
    assert tool.reencode(jpeg(screenshot(), exif=exif.tobytes()))[0] is not None


def test_an_image_in_a_pixel_format_webp_cannot_hold_is_left_as_it_is():
    deep = Image.new("I;16", (120, 80))
    deep.putdata([(x * 500 + y * 40) % 65536 for y in range(80) for x in range(120)])
    assert _left_alone(png(deep)) == "pixel format I;16"
    assert _left_alone(jpeg(screenshot().convert("CMYK"))) == "pixel format CMYK"


def test_a_kept_image_keeps_its_text_exactly():
    tiny = Image.new("RGB", (1, 1), (5, 6, 7))
    raw = png(tiny, level=9)
    real = tool.choose
    tool.choose = lambda stored, options: None
    try:
        old = svg(uri(raw, wrap=4))
        new, images = tool.rewrite(old, {})
    finally:
        tool.choose = real
    assert new == old and images[0][0] == "kept"


# --- whole runs ----------------------------------------------------------------------------------

SVG_ID, HTML_ID = 20, 12


def _database(tmp_path: Path, rows: dict, compression: str = "brotli", dictionary: bytes = None) -> Path:
    db_path = tmp_path / "documentation.db"
    conn = sqlite3.connect(db_path)
    conn.executescript(
        "CREATE TABLE ContentTypes (id INTEGER PRIMARY KEY, value TEXT NOT NULL UNIQUE, compression TEXT NOT NULL);"
        "CREATE TABLE Content (id INTEGER PRIMARY KEY AUTOINCREMENT, path TEXT NOT NULL, languageID INTEGER NOT NULL, "
        "content BLOB NOT NULL, contentTypeID INTEGER NOT NULL, templateId INTEGER NOT NULL DEFAULT 0, UNIQUE(path));"
    )
    conn.execute("INSERT INTO ContentTypes VALUES (?, 'image/svg+xml', ?), (?, 'text/html', 'brotli')",
                 (SVG_ID, compression, HTML_ID))
    if dictionary is not None:
        conn.execute("CREATE TABLE CompressionDictionary (id INTEGER PRIMARY KEY, data BLOB NOT NULL)")
        conn.execute("INSERT INTO CompressionDictionary VALUES (1, ?)", (dictionary,))
    codec = BrotliCodec(dictionary)
    for path, data in rows.items():
        type_id = HTML_ID if path.endswith(".html") else SVG_ID
        comp = "brotli" if type_id == HTML_ID else compression
        stored = codec.compress(data, comp)
        conn.execute("INSERT INTO Content (path, languageID, content, contentTypeID) VALUES (?, 1, x'00', ?)",
                     (path, type_id))
        replace_row(conn, path, 1, stored, 1, type_id, 0, lambda m: None)
    codec.close()
    conn.commit()
    conn.close()
    return db_path


def _read(db_path: Path, path: str, dictionary: bytes = None) -> bytes:
    conn = sqlite3.connect(db_path)
    codec = BrotliCodec(dictionary)
    try:
        content, comp = conn.execute("SELECT c.content, t.compression FROM Content c JOIN ContentTypes t "
                                     "ON t.id = c.contentTypeID WHERE c.path = ?", (path,)).fetchone()
        return codec.decompress(reassemble(conn, path, content), comp)
    finally:
        codec.close()
        conn.close()


def _cfg(db_path: Path, **overrides) -> dict:
    cfg = {"db_path": db_path, "dry_run": False, "workers": 2, "verbose": False}
    cfg.update(overrides)
    return cfg


def _three_rows() -> dict:
    return {"a/flow.svg": svg(uri(png(screenshot())), uri(png(screenshot()))),
            "i/text.svg": svg(uri(png(code_text()))),
            "k/art.svg": svg(uri(png(brand()))),
            "a/vector.svg": b'<svg viewBox="0 0 1 1"><path d="m0 0 1 1"/></svg>',
            "a/page.html": b"<html><img src='flow.svg'></html>"}


def test_end_to_end_rewrites_each_kind_and_nothing_else(tmp_path: Path):
    rows = _three_rows()
    db_path = _database(tmp_path, rows)
    assert tool.run(_cfg(db_path)) == 0
    sizes = {"a/flow.svg": (480, 360), "i/text.svg": (370, 78), "k/art.svg": (400, 220)}
    for path, size in sizes.items():
        new = _read(db_path, path)
        assert outside_uris(new) == outside_uris(rows[path]), path
        assert b"data:image/png" not in new, path
        assert [img.size for img in webp_images(new)] == [size] * rows[path].count(b"data:image/png"), path
    art = webp_images(_read(db_path, "k/art.svg"))[0].convert("RGBA")
    assert art.tobytes() == brand().tobytes()  # lossless: every pixel, transparent ones included
    assert _read(db_path, "a/vector.svg") == rows["a/vector.svg"]
    assert _read(db_path, "a/page.html") == rows["a/page.html"]
    assert len(list(tmp_path.glob("*.backup-*"))) == 1
    conn = sqlite3.connect(db_path)
    assert conn.execute("PRAGMA freelist_count").fetchone()[0] == 0  # VACUUMed after the rewrite
    conn.close()


def test_a_run_names_each_image_it_leaves_as_it_is(tmp_path: Path):
    profile = ImageCms.ImageCmsProfile(ImageCms.createProfile("sRGB")).tobytes()
    profiled = uri(png(screenshot(), icc_profile=profile))
    db_path = _database(tmp_path, {"a/profiled.svg": svg(profiled, uri(png(code_text())))})
    out = io.StringIO()
    with redirect_stdout(out):
        assert tool.run(_cfg(db_path)) == 0
    assert "a/profiled.svg: left 1 image(s) as they are: an ICC colour profile" in out.getvalue()
    new = _read(db_path, "a/profiled.svg")
    assert profiled in new and new.count(b"data:image/webp") == 1


def test_dry_run_writes_nothing(tmp_path: Path):
    db_path = _database(tmp_path, _three_rows())
    before = db_path.read_bytes()
    assert tool.run(_cfg(db_path, dry_run=True)) == 0
    assert db_path.read_bytes() == before and not list(tmp_path.glob("*.backup-*"))


def test_a_second_run_changes_nothing(tmp_path: Path):
    db_path = _database(tmp_path, _three_rows())
    assert tool.run(_cfg(db_path)) == 0
    once = db_path.read_bytes()
    assert tool.run(_cfg(db_path)) == 0
    assert db_path.read_bytes() == once and len(list(tmp_path.glob("*.backup-*"))) == 1


def test_an_svg_with_no_raster_is_nothing_to_do(tmp_path: Path):
    db_path = _database(tmp_path, {"a/vector.svg": b'<svg viewBox="0 0 1 1"><path d="m0 0 1 1"/></svg>'})
    before = db_path.read_bytes()
    assert tool.run(_cfg(db_path)) == 0
    assert db_path.read_bytes() == before


def test_an_undecodable_image_stops_the_run(tmp_path: Path):
    rows = _three_rows()
    rows["a/broken.svg"] = svg(b"data:image/png;base64,QUJDREVGRw==")
    db_path = _database(tmp_path, rows)
    before = db_path.read_bytes()
    assert tool.run(_cfg(db_path)) == 1
    assert db_path.read_bytes() == before and not list(tmp_path.glob("*.backup-*"))


def test_an_unreadable_row_stops_the_run(tmp_path: Path):
    db_path = _database(tmp_path, _three_rows())
    conn = sqlite3.connect(db_path)
    conn.execute("UPDATE Content SET content = x'0102030405' WHERE path = 'a/vector.svg'")
    conn.commit()
    conn.close()
    before = db_path.read_bytes()
    err = io.StringIO()
    with redirect_stderr(err):
        assert tool.run(_cfg(db_path)) == 1
    assert "error: a/vector.svg: cannot be read" in err.getvalue()
    assert "error: 1 SVG row(s) cannot be read; nothing was written" in err.getvalue()
    assert "embedded image" not in err.getvalue()
    assert db_path.read_bytes() == before and not list(tmp_path.glob("*.backup-*"))


def test_a_run_whose_images_are_all_kept_writes_nothing(tmp_path: Path):
    db_path = _database(tmp_path, _three_rows())
    before = db_path.read_bytes()
    real = tool.choose
    tool.choose = lambda stored, options: None
    try:
        assert tool.run(_cfg(db_path)) == 0
    finally:
        tool.choose = real
    assert db_path.read_bytes() == before and not list(tmp_path.glob("*.backup-*"))


def test_jpeg_images_are_re_encoded_too(tmp_path: Path):
    photo = jpeg(screenshot())
    rows = {"a/photo.svg": svg(uri(photo, mime=b"jpeg"), uri(photo, mime=b"jpg"))}
    db_path = _database(tmp_path, rows)
    assert tool.run(_cfg(db_path)) == 0
    new = _read(db_path, "a/photo.svg")
    assert b"data:image/jp" not in new and [img.size for img in webp_images(new)] == [(480, 360)] * 2
    assert outside_uris(new) == outside_uris(rows["a/photo.svg"])


def test_a_chunked_row_is_rewritten_into_one_row(tmp_path: Path):
    big = screenshot().resize((1100, 900))
    data = svg(uri(png(big, level=0)))  # uncompressed PNG: well over one chunk, stored as is
    assert len(data) > 2 * CHUNK_SIZE
    db_path = _database(tmp_path, {"a/big.svg": data}, compression="none")
    conn = sqlite3.connect(db_path)
    assert conn.execute("SELECT count(*) FROM Content WHERE path LIKE 'a/big.svg-%'").fetchone()[0] >= 2
    conn.close()
    assert tool.run(_cfg(db_path)) == 0
    conn = sqlite3.connect(db_path)
    assert conn.execute("SELECT count(*) FROM Content WHERE path LIKE 'a/big.svg-%'").fetchone()[0] == 0
    conn.close()
    new = _read(db_path, "a/big.svg")
    assert outside_uris(new) == outside_uris(data) and webp_images(new)[0].size == (1100, 900)


def test_a_chunked_dictionary_compressed_row_is_read_as_one_svg(tmp_path: Path):
    # Its continuation rows are fragments of one Brotli stream: read as SVGs of their own they do not decode, so the
    # run must take them only through their base row. (An uncompressed fragment, as above, decodes either way.)
    if shutil.which("brotli") is None:
        print("  (skipped: no brotli CLI)")
        return
    dictionary = (b'<svg viewBox="0 0 10 10" xmlns="http://www.w3.org/2000/svg" xlink:href="data:image/') * 40
    random.seed(11)
    filler = base64.b64encode(random.randbytes(1_300_000))  # about 1.3 MB however it is compressed
    data = svg(uri(png(screenshot()))).replace(b"<defs>", b"<!-- " + filler + b" --><defs>", 1)
    db_path = _database(tmp_path, {"a/big.svg": data}, dictionary=dictionary)
    conn = sqlite3.connect(db_path)
    assert conn.execute("SELECT count(*) FROM Content WHERE path LIKE 'a/big.svg-%'").fetchone()[0] >= 1
    conn.close()
    assert tool.run(_cfg(db_path)) == 0
    new = _read(db_path, "a/big.svg", dictionary)
    assert outside_uris(new) == outside_uris(data) and [img.size for img in webp_images(new)] == [(480, 360)]


def test_rows_are_compressed_with_the_databases_dictionary(tmp_path: Path):
    if shutil.which("brotli") is None:
        print("  (skipped: no brotli CLI)")
        return
    dictionary = (b'<svg viewBox="0 0 10 10" xmlns="http://www.w3.org/2000/svg" xlink:href="data:image/') * 40
    rows = {"i/text.svg": svg(uri(png(code_text())))}
    db_path = _database(tmp_path, rows, dictionary=dictionary)
    assert tool.run(_cfg(db_path)) == 0
    new = _read(db_path, "i/text.svg", dictionary)
    assert outside_uris(new) == outside_uris(rows["i/text.svg"])
    conn = sqlite3.connect(db_path)
    stored = conn.execute("SELECT content FROM Content WHERE path = 'i/text.svg'").fetchone()[0]
    conn.close()
    try:
        plain = brotli.decompress(stored)
    except brotli.error:
        plain = None
    assert plain != new  # it needs the dictionary to decode back


def _main() -> int:
    failures = []
    for name, function in sorted(globals().items()):
        if not name.startswith("test_") or not callable(function):
            continue
        with tempfile.TemporaryDirectory() as directory:
            needs_tmp = "tmp_path" in function.__code__.co_varnames[:function.__code__.co_argcount]
            try:
                function(Path(directory)) if needs_tmp else function()
            except (KeyboardInterrupt, SystemExit):
                raise
            except BaseException as exc:  # noqa: BLE001 - a failure is the result here
                frame = traceback.extract_tb(exc.__traceback__)[-1]
                failures.append(f"{name} (line {frame.lineno}: {frame.line}): {type(exc).__name__}: {exc}")
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
