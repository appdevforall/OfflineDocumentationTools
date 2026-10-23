#!/usr/bin/env python3
# /// script
# requires-python = ">=3.9"
# dependencies = [
#     "brotli",
#     "Pillow",
# ]
# ///
"""
svg_images_to_webp.py

Re-encodes the raster images that SVG rows of a documentation database embed
as data: URIs - screenshots and artwork wrapped in an SVG, not vector drawings -
as WebP, image by image, and changes nothing else in the SVG.

Why: eight SVG rows hold 2,351,067 stored bytes, almost all of it base64 PNG
saved with zlib's fastest setting, at up to three times the pixels the image is
drawn at. The Android notification-permission flow diagram alone embeds one
937x2048 screenshot three times (1.6 MB stored).

How each image is re-encoded (the rule):
- Candidates: lossless WebP (exact, every pixel kept); lossy WebP at quality
  80; and, for a fully opaque image only, a 256-colour palette (median cut, no
  dither) stored as lossless WebP.
- A lossy candidate counts only if its visible PSNR is at least 35 dB, where
  visible means composited over white and over black and the lower of the two
  taken, so a page in dark mode is covered too. Lossless always counts.
- The smallest candidate that counts is kept, and only if it is smaller than
  the image as stored; otherwise the image stays as it is.
- Identical images (the same bytes) get the same new bytes, so Brotli still
  finds the repeat inside a row.

Why those limits: 35 dB separates the content measured. Lossy WebP holds 36.7
to 56 dB on the screenshots of phone screens in the Android guides, but blurs
the coloured code text of Code On The Go's own editor screenshots (28.0-29.9 dB,
even at quality 90) and the Kotlin brand art (30.4-31.8 dB). A palette keeps
those code screenshots at 36.8-37.8 dB in half the bytes of lossless; it is not
used for an image with any transparency, because quantising alpha bands soft
edges and shadows against whatever background the page has. So the brand art,
which has alpha, stays pixel-identical in lossless WebP.

Measured on the 2026-10-19 ship-time database with Pillow 12.1.1 and libwebp
1.5.0 (the sizes depend a little on the encoder's version: a first measurement
stored the 8 rows in 320,194 bytes): stored bytes against its own dictionary;
image PSNR is the lowest of the row's lossy images; render PSNR is
compare_svg_renders.py's, the SVG drawn in Chrome before and after:

    SVG row                                             before    after  images                 image  render
    a/devsite/...notification-permission-...-flow.svg  1,604,946   94,591  lossy x5, lossless x1  36.7   42.6
    k/html/images/kotlin-night-cover.svg                 203,101   63,245  lossless               -      73.7
    a/devsite/...app-links_add-domains.svg               159,957   38,734  lossy x2, lossless x1  46.0   48.8
    a/devsite/...app-links_choose-domains.svg            157,719   42,227  lossy, lossless        45.7   48.3
    k/html/images/kotlin-night-fb.svg                    112,659   36,365  lossless               -      99
    i/images/boundaries-enabled.svg, not-enabled.svg,    112,685   44,860  palette x3             36.8   40.0
      not-enabled2.svg

All 8 rows: 2,351,067 -> 320,022 stored bytes. Packed as the pipeline packs
(rebuild_database.py --pack Content,TooltipTexts --page-size 4096) and set
against the same database packed the same way without this step, the file goes
from 90,783,744 to 88,752,128 bytes (-2,031,616) and its .br (brotli -q 11
-w 24) from 72,685,042 to 70,648,466 (-2,036,576). Packing alone moves the
unchanged database's .br from 72,546,065 to 72,685,042, so the baseline has to
be packed too. The cover art's pixels are identical (the lossless check below
proves it), yet it renders at 73.7 dB, not 99: Chrome draws that PNG and its
WebP slightly differently, most likely in how it premultiplies alpha (not
verified). The i/ rows are ADFA-6645's item 3.

Before anything is written, each new image is decoded and must have the same
pixel size as the old one, and a lossless one must give back exactly the same
pixels. An SVG row that cannot be read, an embedded image that cannot be
decoded, or a candidate that fails those checks stops the run with exit 1 and
nothing written. Animated images are left alone (a WebP of their first frame
would lose the animation), and so are images already in WebP, which is what
makes a second run change nothing.

Those checks compare decoded pixels, so they cannot see what else changes how
a browser draws an image, and the WebP is written without it. An image is left
as it is, and named in the report, when it carries an ICC colour profile, a
PNG gamma other than sRGB's (gAMA 0.45455) with no sRGB chunk, or an EXIF
orientation other than 1, or when it decodes to a pixel format outside 1, L,
LA, P, PA, RGB and RGBA (16-bit, CMYK and the like, which Pillow would convert
before the lossless check). None of the shipped images has any of these; the
three i/ PNGs carry an sRGB chunk with the standard gAMA, which changes
nothing.

Only the data: URI of each re-encoded image changes (data:image/png;base64,...
becomes data:image/webp;base64,...); the vector parts, attributes and text of
the SVG stay byte for byte. An image is a data:image/png, image/jpeg or
image/jpg URI with a base64 payload, as a quoted attribute value or inside a
CSS url(...); its payload runs to that quote or parenthesis, so nothing after
it is taken in. Left alone, without counting as a problem: any other type
(image/gif, and image/webp, which is what this step writes), an upper-case type
(data:image/PNG), a type with parameters (data:image/png;charset=...), a
payload holding anything but base64 and whitespace (an entity such as &#10;),
and a data: URI anywhere else, such as in text, where nothing draws it. A
payload without its = padding is decoded as a browser decodes it; one a browser
could not decode either stops the run.

Code On The Go's WebView draws a WebP inside an SVG shown through <img>
(checked on a Pixel 6 Pro, Chrome 155), which is how the a/devsite and k/ pages
show theirs. i/prefs-editor.html shows the three i/ SVGs through <object>
instead; that path is checked on the desktop only. compare_svg_renders.py
renders each changed row before and after in headless Chrome, through <img> and
through <object>, for a visual check: in Chrome 140 the two give the same
render PSNR on every row (the table's figures).

Pipeline position: on the copy that ships, after every step that writes SVG
rows (the media steps) and before the final dictionary re-mint, which then
trains on the new bytes. It touches only SVG rows, so its place against the
other ship-time steps does not otherwise matter.

Runs in one transaction (rolled back on any error), backs the database up
first, and VACUUMs afterwards. --dry-run does all the work, the checks
included, and writes nothing. Exit 1 always means nothing was written.

    uv run scripts/svg_images_to_webp.py documentation.db --dry-run
    uv run scripts/svg_images_to_webp.py documentation.db [--workers N] [--verbose]
"""
from __future__ import annotations

import argparse
import base64
import binascii
import hashlib
import io
import math
import re
import sqlite3
import sys
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from PIL import Image, ImageChops, ImageStat

from optimize_db_media import BrotliCodec, backup_database, human, is_continuation_path, load_dictionary, reassemble
from update_media_references import replace_row

SVG_TYPE = "image/svg+xml"
# A raster image embedded as a data: URI, as a quoted attribute value or a CSS url(...). Base64 may be wrapped across
# lines; the payload must run to that delimiter (the opening quote again, or the closing parenthesis), and whitespace
# before it is not part of the match and stays as it was. So nothing after a payload is ever taken in with it, and a
# data: URI anywhere else - in text, where nothing draws it - is left alone.
_PAYLOAD = rb"[A-Za-z0-9+/=]+(?:\s+[A-Za-z0-9+/=]+)*"
_RASTER = rb"data:image/(?:png|jpeg|jpg);base64,"
RASTER_URI = re.compile(rb'(?<=")' + _RASTER + rb"(?P<dq>" + _PAYLOAD + rb')(?=\s*")'
                        rb"|(?<=')" + _RASTER + rb"(?P<sq>" + _PAYLOAD + rb")(?=\s*')"
                        rb"|(?<=\()" + _RASTER + rb"(?P<pa>" + _PAYLOAD + rb")(?=\s*\))")
WEBP_PREFIX = b"data:image/webp;base64,"
LOSSY_QUALITY = 80
PALETTE_COLOURS = 256
# Lowest visible PSNR, in dB, a lossy candidate may have; see the docstring for why 35.
MIN_PSNR = 35.0
# Backgrounds a transparent image is judged against: a light page and a dark one.
BACKGROUNDS = ((255, 255, 255), (0, 0, 0))
# Pixel formats a WebP holds exactly. Pillow converts any other (16-bit, CMYK, I, F) lossily, and before the lossless
# check sees it, so such an image is left as it is.
WEBP_MODES = frozenset({"1", "L", "LA", "P", "PA", "RGB", "RGBA"})
# The PNG gAMA values that mean sRGB's 1/2.2, in the chunk's units of 1/100000: rounded, and as some encoders truncate.
SRGB_GAMMAS = (45455, 45454)
EXIF_ORIENTATION = 0x0112
LEFT = "left: "


class Refused(Exception):
    """Something this step cannot change safely; the run stops with nothing written."""


def payload_of(match: re.Match) -> bytes:
    return match.group("dq") or match.group("sq") or match.group("pa")


def decode_payload(payload: bytes) -> bytes:
    """The bytes a browser decodes from a data: URI's base64: whitespace dropped and padding optional, as forgiving
    base64 has it. Refused for base64 a browser could not decode either."""
    data = re.sub(rb"\s", b"", payload)
    if len(data) % 4 == 0:
        data = data[:-2] if data.endswith(b"==") else data[:-1] if data.endswith(b"=") else data
    if len(data) % 4 == 1 or b"=" in data:
        raise Refused("an embedded image's base64 is malformed")
    try:
        return base64.b64decode(data + b"=" * (-len(data) % 4), validate=True)
    except binascii.Error as exc:
        raise Refused(f"an embedded image's base64 is malformed ({exc})") from exc


def visible_psnr(original: Image.Image, candidate: Image.Image) -> float:
    """PSNR of what a reader sees: both images composited over each background, the lower figure."""
    worst = 99.0
    a, b = original.convert("RGBA"), candidate.convert("RGBA")
    for colour in BACKGROUNDS:
        page = Image.new("RGBA", a.size, colour + (255,))
        diff = ImageChops.difference(Image.alpha_composite(page, a).convert("RGB"),
                                     Image.alpha_composite(page, b).convert("RGB"))
        mse = sum(v * v for v in ImageStat.Stat(diff).rms) / 3
        worst = min(worst, 99.0 if mse == 0 else 10 * math.log10(255 * 255 / mse))
    return worst


def is_opaque(img: Image.Image) -> bool:
    return img.convert("RGBA").getchannel("A").getextrema() == (255, 255)


def webp(img: Image.Image, lossless: bool) -> bytes:
    buf = io.BytesIO()
    if lossless:
        img.save(buf, "WEBP", lossless=True, exact=True, quality=100, method=6)
    else:
        img.save(buf, "WEBP", quality=LOSSY_QUALITY, method=6)
    return buf.getvalue()


def candidates(img: Image.Image) -> list:
    """[(kind, webp bytes, visible PSNR)] for one image; PSNR 99 means identical pixels."""
    opaque = is_opaque(img)
    base = img.convert("RGB") if opaque else img.convert("RGBA")
    out = [("lossless", webp(base, lossless=True), 99.0), ("lossy", webp(base, lossless=False), None)]
    if opaque:
        palette = base.quantize(PALETTE_COLOURS, method=Image.Quantize.MEDIANCUT, dither=Image.Dither.NONE)
        out.append(("palette", webp(palette.convert("RGB"), lossless=True), None))
    checked = []
    for kind, data, quality in out:
        decoded = Image.open(io.BytesIO(data))
        decoded.load()
        if decoded.size != img.size:
            raise Refused(f"its {kind} WebP decodes to {decoded.size}, not {img.size}")
        if kind == "lossless":
            if decoded.convert(base.mode).tobytes() != base.tobytes():
                raise Refused("its lossless WebP does not give back the same pixels")
        else:
            quality = visible_psnr(base, decoded)
        checked.append((kind, data, quality))
    return checked


def choose(stored_size: int, options: list):
    """The smallest option that counts (lossless always; others at MIN_PSNR or better), if it is smaller than the
    image as stored; else None, meaning the image stays as it is."""
    counted = [o for o in options if o[0] == "lossless" or o[2] >= MIN_PSNR]
    if not counted:
        return None
    best = min(counted, key=lambda o: len(o[1]))
    return best if len(best[1]) < stored_size else None


def left_alone(img: Image.Image) -> str | None:
    """Why `img` has to stay as it is, or None. The checks compare decoded pixels only, and the WebP is written without
    what else a browser draws an image with, so an image that has any of it is not re-encoded."""
    if getattr(img, "is_animated", False):
        return "animated"
    if img.info.get("icc_profile"):
        return "an ICC colour profile"
    gamma = img.info.get("gamma")
    if gamma is not None and "srgb" not in img.info and round(gamma * 100000) not in SRGB_GAMMAS:
        return f"a gamma of {gamma} with no sRGB chunk"
    orientation = img.getexif().get(EXIF_ORIENTATION, 1)
    if orientation != 1:
        return f"EXIF orientation {orientation}"
    if img.mode not in WEBP_MODES:
        return f"pixel format {img.mode}"
    return None


def reencode(raw: bytes) -> tuple:
    """(new data URI or None, kind, PSNR) for one embedded image's bytes. A kind starting with LEFT says why the image
    was not even tried."""
    try:
        img = Image.open(io.BytesIO(raw))
        img.load()
    except Exception as exc:  # noqa: BLE001 - any decode failure is the same refusal
        raise Refused(f"an embedded image cannot be decoded ({exc})") from exc
    reason = left_alone(img)
    if reason:
        return None, LEFT + reason, None
    best = choose(len(raw), candidates(img))
    if best is None:
        return None, "kept", None
    kind, data, quality = best
    return WEBP_PREFIX + base64.b64encode(data), kind, quality


def rewrite(svg: bytes, cache: dict) -> tuple:
    """(new SVG bytes, [(kind, PSNR, old bytes, new bytes)] per embedded image). `cache` maps an image's sha256 to its
    result, so identical images get identical bytes."""
    images, pieces, last = [], [], 0
    for m in RASTER_URI.finditer(svg):
        raw = decode_payload(payload_of(m))
        key = hashlib.sha256(raw).hexdigest()
        if key not in cache:
            cache[key] = reencode(raw)
        uri, kind, quality = cache[key]
        images.append((kind, quality, len(raw), len(base64.b64decode(uri[len(WEBP_PREFIX):])) if uri else len(raw)))
        pieces.append(svg[last:m.start()])
        pieces.append(uri if uri is not None else m.group(0))
        last = m.end()
    pieces.append(svg[last:])
    return b"".join(pieces), images


def run(cfg: dict) -> int:
    db_path = cfg["db_path"]
    if not db_path.is_file():
        print(f"error: {db_path} does not exist", file=sys.stderr)
        return 1
    conn = sqlite3.connect(db_path)
    codec = None
    try:
        lengths = {path: n for path, n in conn.execute("SELECT path, LENGTH(content) FROM Content")}
        rows = [r for r in conn.execute(
            "SELECT c.path, c.content, c.languageID, c.contentTypeID, c.templateId, ct.compression "
            "FROM Content c JOIN ContentTypes ct ON ct.id = c.contentTypeID WHERE ct.value = ? ORDER BY c.path",
            (SVG_TYPE,)) if not is_continuation_path(lengths, r[0])]
        codec = BrotliCodec(load_dictionary(conn))
        work = [(path, reassemble(conn, path, content), lang, ctid, tid, comp)
                for path, content, lang, ctid, tid, comp in rows]
        problems, decoded = [], []
        for path, blob, lang, ctid, tid, comp in work:
            try:
                decoded.append((path, blob, codec.decompress(blob, comp), lang, ctid, tid, comp))
            except Exception as exc:  # noqa: BLE001 - one unreadable row stops the run
                problems.append(f"{path}: cannot be read ({exc})")
        if problems:
            for p in problems:
                print(f"  error: {p}", file=sys.stderr)
            print(f"error: {len(problems)} SVG row(s) cannot be read; nothing was written", file=sys.stderr)
            return 1
        with_images = [d for d in decoded if RASTER_URI.search(d[2])]
        print(f"{len(decoded)} SVG row(s); {len(with_images)} embed raster images.")

        # Every distinct image is re-encoded once, in parallel; each row is then rewritten from the shared results.
        # Its own list, so an unreadable row can never reach the end of the run reported as an image problem.
        cache, distinct, image_problems = {}, {}, []
        for path, _blob, svg, *_ in with_images:
            for m in RASTER_URI.finditer(svg):
                try:
                    raw = decode_payload(payload_of(m))
                except Refused as exc:
                    image_problems.append(f"{path}: {exc}")
                    continue
                distinct.setdefault(hashlib.sha256(raw).hexdigest(), raw)

        def one(item):
            key, raw = item
            try:
                return key, reencode(raw), None
            except Refused as exc:
                return key, None, str(exc)

        with ThreadPoolExecutor(max_workers=cfg["workers"]) as pool:
            for key, result, error in pool.map(one, distinct.items()):
                if error:
                    image_problems.append(error)
                else:
                    cache[key] = result
        if image_problems:
            for p in sorted(set(image_problems)):
                print(f"  error: {p}", file=sys.stderr)
            print(f"error: {len(image_problems)} embedded image(s) cannot be re-encoded safely; nothing was written",
                  file=sys.stderr)
            return 1

        changes = []
        for path, blob, svg, lang, ctid, tid, comp in with_images:
            new_svg, images = rewrite(svg, cache)
            left = [k[len(LEFT):] for k, *_ in images if k.startswith(LEFT)]
            if left:
                print(f"  {path}: left {len(left)} image(s) as they are: {', '.join(sorted(set(left)))}")
            if new_svg == svg:
                continue
            changes.append((path, lengths[path], len(blob), codec.compress(new_svg, comp), lang, ctid, tid, images))
        for path, _base, before, stored, *_rest, images in changes:
            kinds = ", ".join(sorted({k for k, *_ in images}))
            worst = min((q for _k, q, *_ in images if q is not None), default=None)
            print(f"  {path}: {human(before)} -> {human(len(stored))} stored; {len(images)} image(s): {kinds}"
                  + (f"; lowest PSNR {worst:.1f} dB" if worst is not None and worst < 99 else ""))
            if cfg["verbose"]:
                for kind, quality, old, new in images:
                    print(f"      {kind}: {human(old)} -> {human(new)} bytes"
                          + (f", {quality:.1f} dB" if quality is not None else ""))
        if not changes:
            print("Nothing to do: no embedded image gets smaller within the quality rule.")
            return 0
        saved = sum(before - len(stored) for _p, _b, before, stored, *_ in changes)
        print(f"{len(changes)} SVG row(s) to rewrite, saving {human(saved)} stored bytes.")

        if cfg["dry_run"]:
            print("Dry run: nothing written.")
            return 0
        print(f"Backup written to {backup_database(db_path)}")
        conn.execute("BEGIN")
        try:
            for path, base_len, _before, stored, lang, ctid, tid, _images in changes:
                replace_row(conn, path, base_len, stored, lang, ctid, tid, lambda m: print(m, file=sys.stderr))
            conn.commit()
        except Exception:
            conn.rollback()
            raise
    finally:
        if codec is not None:
            codec.close()
        conn.close()

    vac = sqlite3.connect(db_path)
    try:
        vac.execute("VACUUM")
    finally:
        vac.close()
    print(f"Done. Rewrote {len(changes)} SVG row(s).")
    return 0


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("db_path", type=Path, help="SQLite database to edit in place, e.g. documentation.db")
    p.add_argument("--dry-run", action="store_true", help="Do all the work and checks; write nothing")
    p.add_argument("--workers", type=int, default=8, help="Threads that re-encode images (default: 8)")
    p.add_argument("--verbose", action="store_true", help="Print every image's choice and sizes")
    return p


def main() -> None:
    args = build_parser().parse_args()
    sys.exit(run({"db_path": args.db_path, "dry_run": args.dry_run, "workers": args.workers,
                  "verbose": args.verbose}))


if __name__ == "__main__":
    main()
