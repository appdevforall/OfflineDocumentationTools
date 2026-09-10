#!/usr/bin/env python3
# /// script
# requires-python = ">=3.9"
# dependencies = [
#     "brotli",
#     "Pillow",
#     "scour",
#     "cairosvg",
# ]
# ///
"""
optimize_db_media.py

Applies the image optimization from PR #24 (the Kotlin website's
optimize_media.py) to *every* media file already stored in a
documentation.db, across all doc sets (a/, i/, k/, p/, j/, ...).

Two modes:

  * Default - optimize each image **in place**: same Content.path, same
    extension, same contentTypeID. Every reference to it keeps resolving,
    whatever form that reference takes, because nothing is renamed.

  * --webp - **convert** formats, mirroring insert_optimized_media.py's own
    --webp behavior: every static raster becomes WEBP, and an SVG still over
    --svg-rasterize-threshold after minifying is rasterized to WEBP. A
    converted file is written to a NEW path (same directory and stem, new
    extension), its content type set to the format actually produced, and the
    old row (plus its chunk chain) is deleted.

    NOTE: --webp does NOT rewrite the references that point at the old
    filenames, so pages will link to names that no longer exist until those
    references are fixed separately. insert_optimized_media.py can only do
    that rewriting for Kotlin, whose pages embed a literal
    "/k/html/images/<name>"; the other doc sets don't share that form (an
    Android page references media by absolute URL, e.g.
    "https://developer.android.com/images/foo.gif", with no literal link to
    the stored path "a/devsite/media/..._foo.gif"), so fixing them up is a
    separate job this tool deliberately leaves alone.

What it does, per media Content row (inside one transaction, rolled back on
any error), mirroring optimize_media.py's own encoders and defaults:
  - image/png : normalize mode, pngquant at full res, downscale to
    --max-width (never upscaled), pngquant again. Stored raw (no compression).
    With --webp: downscaled and re-encoded as WEBP instead (no pngquant,
    which only makes sense for PNG output).
  - image/jpeg: downscale, re-encode JPEG (--jpeg-quality, progressive). Raw.
    With --webp: WEBP instead.
  - image/gif : downscale. Animated GIFs get every frame resized (frame
    count/durations/loop preserved) and stay GIFs even under --webp, since
    animated-WEBP encoding isn't implemented here. Raw.
  - image/webp: downscale, re-encode WEBP (--webp-quality). Animated WEBP is
    left untouched. Brotli-compressed on store, matching the ContentTypes row.
  - image/svg+xml: Scour-optimized (metadata/comment/id stripping, numbers
    rounded to --svg-precision). With --webp, one still over
    --svg-rasterize-threshold is rasterized to WEBP; if that fails (e.g. no
    native cairo for cairosvg), the optimized SVG is kept and a note logged.
  - Every other content type (video/mp4, image/x-icon, fonts, html, ...) is
    left untouched.

Each row's bytes are decompressed per its ContentTypes.compression ("brotli"
-> plain, or against the shared CompressionDictionary when the database has
one; "none" -> raw), optimized, then compressed the way the *resulting*
format's content type says to. A row is only rewritten when the result is
actually smaller than what's already stored - an image is never made larger,
and never renamed for no benefit.

Reads/writes bytes straight from/to the DB; needs no source directory. Backs
up the database first (VACUUM INTO a timestamped sibling), VACUUMs at the end
to reclaim freed space, and supports --dry-run (do all the work, log what
would change, roll back).

Python dependencies (Pillow, scour, brotli) are declared inline above (PEP
723), so uv installs them on the fly - run it one-shot with no setup:

    uv run scripts/optimize_db_media.py documentation.db

The "pngquant" and "brotli" command-line tools must also be on PATH (e.g.
`brew install pngquant brotli`); those are system binaries, not pip packages,
so uv can't provide them.
"""
from __future__ import annotations

import argparse
import io
import re
import shutil
import subprocess
import sqlite3
import sys
import tempfile
import time
from pathlib import Path

import brotli
from PIL import Image


# ---------------------------------------------------------------------------
# Content-table chunking protocol (inlined so this optimizer is a single,
# self-contained file). Every tool that reads or rewrites a Content row has to
# agree with what WebServer.kt actually serves: read `path`; if that blob is
# exactly CHUNK_SIZE bytes, keep appending `path-1`, `path-2`, ... in suffix
# order, stopping at the first fragment shorter than CHUNK_SIZE (or the first
# one missing). Two rules fall out of that:
#   1. A "<base>-<N>" path is a continuation only if the base row is exactly
#      CHUNK_SIZE bytes - the base merely existing proves nothing.
#   2. A short fragment (or a gap in the numbering) terminates the chain.
# Suffix discovery is deliberately suffix-agnostic (it does not assume the
# chain starts at -1), because real chains numbered from -2 exist in the
# production database (ADFA-5171).
# ---------------------------------------------------------------------------
CHUNK_SIZE = 1024 * 1024  # must match WebServer.kt's contentChunkSize exactly
_FRAGMENT_SUFFIX_RE = re.compile(r"^(.*)-(\d+)$")


def _split_fragment_path(path):
    """("k/html/a.html", 2) for "k/html/a.html-2", or None if `path` has no
    numeric "-<N>" suffix. Purely syntactic."""
    match = _FRAGMENT_SUFFIX_RE.match(path)
    return (match.group(1), int(match.group(2))) if match else None


def _is_chunked_base(lengths: dict, base_path: str) -> bool:
    """Rule 1: whether `base_path` heads a chunked item, given a {path: length}
    map. Anything shorter than CHUNK_SIZE owns no continuations."""
    return lengths.get(base_path) == CHUNK_SIZE


def is_continuation_path(lengths: dict, path: str) -> bool:
    """Whether `path` is a continuation row of some chunked base rather than a
    page of its own (rule 1 from the fragment's side) - what a scan over every
    row needs to skip fragments without also skipping ordinary pages that
    merely look like one."""
    split = _split_fragment_path(path)
    if split is None:
        return False
    return _is_chunked_base(lengths, split[0])


def _discover(conn, base_path: str):
    """[(n, path, length)] for every existing "<base_path>-<N>" row, ordered by
    N. The LIKE pattern over-matches (`_`/`%` are wildcards, `-%` doesn't
    constrain the tail to digits); the regex re-check makes the result exact."""
    found = []
    for path, length in conn.execute(
        "SELECT path, LENGTH(content) FROM Content WHERE path LIKE ?", (f"{base_path}-%",)
    ).fetchall():
        split = _split_fragment_path(path)
        if split is not None and split[0] == base_path:
            found.append((split[1], path, length))
    found.sort(key=lambda item: item[0])
    return found


def owned_fragment_paths(conn, base_path: str, base_length: int = None) -> list:
    """Every continuation row belonging to `base_path`, in suffix order - the
    *ownership* answer (includes fragments past a short one, since a delete or
    replace must take the whole tail rather than orphaning it). Empty unless
    the base is genuinely chunked (rule 1)."""
    if base_length is None:
        row = conn.execute("SELECT LENGTH(content) FROM Content WHERE path = ?", (base_path,)).fetchone()
        if row is None:
            return []
        base_length = row[0]
    if not _is_chunked_base({base_path: base_length}, base_path):
        return []
    return [path for _n, path, _length in _discover(conn, base_path)]


def _served_fragment_paths(conn, base_path: str, base_length: int) -> list:
    """The continuation rows WebServer.kt would actually concatenate: rule 1 to
    decide there's a chain, then rule 2 - stop at the first fragment shorter
    than CHUNK_SIZE and at the first gap in the numbering. Contiguity is
    enforced from whatever suffix the chain begins at (not from 1), so an
    ADFA-5171 chain numbered from -2 still reads whole. The *reassembly*
    answer; use owned_fragment_paths when deleting or replacing instead."""
    if not _is_chunked_base({base_path: base_length}, base_path):
        return []
    served = []
    expected = None
    for number, path, length in _discover(conn, base_path):
        if expected is not None and number != expected:
            break  # a gap: the server would have stopped at the missing suffix
        served.append(path)
        if length < CHUNK_SIZE:
            break
        expected = number + 1
    return served


def reassemble(conn, base_path: str, first_content: bytes) -> bytes:
    """The full bytes the server would serve for `base_path`, given its
    already-read base blob."""
    if len(first_content) < CHUNK_SIZE:
        return first_content
    parts = [first_content]
    for path in _served_fragment_paths(conn, base_path, len(first_content)):
        row = conn.execute("SELECT content FROM Content WHERE path = ?", (path,)).fetchone()
        if row is None:  # raced with a concurrent delete; serve what we have
            break
        parts.append(row[0])
    return b"".join(parts)


try:
    RESAMPLE = Image.Resampling.LANCZOS
except AttributeError:  # Pillow < 9.1
    RESAMPLE = Image.LANCZOS

DEFAULTS = {
    "max_width": 500,
    "jpeg_quality": 82,
    "webp_quality": 80,
    "pngquant_speed": 4,
    "svg_precision": 4,
    "svg_rasterize_threshold": 300 * 1024,  # 300KB
}


# --- primitives, mirroring optimize_media.py ---------------------------------

def find_pngquant() -> str:
    path = shutil.which("pngquant")
    if path is None:
        raise RuntimeError("pngquant not found on PATH; install it (e.g. `brew install pngquant`) and retry")
    return path


def quantize_png_bytes(data: bytes, pngquant_path: str, speed: int, name: str) -> bytes:
    """pngquant over raw PNG bytes (stdin->stdout). Falls back to the input
    bytes if pngquant declines (e.g. exit 99: would fall below the quality
    floor) - a slightly larger PNG beats a broken one."""
    result = subprocess.run(
        [pngquant_path, "--quality", "65-95", "--speed", str(speed), "--strip", "--force", "--output", "-", "-"],
        input=data, stdout=subprocess.PIPE, stderr=subprocess.PIPE, check=False,
    )
    if result.returncode != 0 or not result.stdout:
        return data
    return result.stdout


def resize_if_needed(img: Image.Image, max_width: int) -> Image.Image:
    if img.width <= max_width:
        return img
    new_height = max(1, round(img.height * (max_width / img.width)))
    return img.resize((max_width, new_height), RESAMPLE)


def normalize_mode(img: Image.Image) -> Image.Image:
    if img.mode == "P":
        return img.convert("RGBA") if img.info.get("transparency") is not None else img.convert("RGB")
    if img.mode == "CMYK":
        return img.convert("RGB")
    return img


# --- per-format, in-memory optimizers (bytes -> bytes, format preserved) -----

def encode_png(img: Image.Image, pngquant_path: str, speed: int, max_width: int, name: str) -> bytes:
    """Quantize/resize an already-open static PNG. pngquant runs at full
    resolution first (its palette selection sees the original color detail),
    then the image is resized and quantized again at the delivered size - the
    same two-pass shape optimize_media.py uses."""
    img = normalize_mode(img)
    buf = io.BytesIO()
    img.save(buf, "PNG", optimize=True)
    quantized = quantize_png_bytes(buf.getvalue(), pngquant_path, speed, name)
    img = Image.open(io.BytesIO(quantized))
    img.load()
    img = normalize_mode(img)
    img = resize_if_needed(img, max_width)
    buf = io.BytesIO()
    img.save(buf, "PNG", optimize=True)
    return quantize_png_bytes(buf.getvalue(), pngquant_path, speed, name)


def encode_jpeg(img: Image.Image, quality: int, max_width: int) -> bytes:
    img = normalize_mode(img)
    img = resize_if_needed(img, max_width)
    if img.mode != "RGB":
        img = img.convert("RGB")
    buf = io.BytesIO()
    img.save(buf, "JPEG", quality=quality, optimize=True, progressive=True)
    return buf.getvalue()


def encode_static_gif(img: Image.Image, max_width: int) -> bytes:
    img = resize_if_needed(img, max_width)
    buf = io.BytesIO()
    img.save(buf, "GIF", optimize=True)
    return buf.getvalue()


def optimize_animated_gif(img: Image.Image, max_width: int) -> bytes:
    """Resizes every frame of an already-open animated GIF, preserving frame
    count, each frame's own duration, and the loop count."""
    n_frames = getattr(img, "n_frames", 1)
    loop = img.info.get("loop", 0)
    frames, durations = [], []
    for i in range(n_frames):
        img.seek(i)
        frames.append(resize_if_needed(img.convert("RGBA"), max_width))
        durations.append(img.info.get("duration", 100))
    buf = io.BytesIO()
    frames[0].save(buf, "GIF", save_all=True, append_images=frames[1:], duration=durations,
                   loop=loop, disposal=2, optimize=True)
    return buf.getvalue()


def encode_webp(img: Image.Image, quality: int, max_width: int) -> bytes:
    """Resize and encode any already-loaded image as WEBP."""
    img = normalize_mode(img)
    img = resize_if_needed(img, max_width)
    buf = io.BytesIO()
    img.save(buf, "WEBP", quality=quality, method=6)
    return buf.getvalue()


def rasterize_svg(svg_text: str, max_width: int) -> Image.Image:
    """Renders SVG markup to a raster at exactly max_width px wide (cairosvg
    derives the height from the SVG's own viewBox), for SVGs too large to keep
    as vector. Imported lazily: cairosvg needs the native cairo library, and a
    machine without it should fall back to keeping the optimized SVG rather
    than failing the whole run."""
    import cairosvg

    png_bytes = cairosvg.svg2png(bytestring=svg_text.encode("utf-8"), output_width=max_width)
    img = Image.open(io.BytesIO(png_bytes))
    img.load()
    return img


def optimize_svg(data: bytes, precision: int) -> bytes:
    """Scour-optimize with optimize_media.py's own aggressive settings."""
    from scour import scour

    options = scour.generateDefaultOptions()
    options.remove_metadata = True
    options.remove_descriptive_elements = True
    options.remove_titles = True
    options.remove_descriptions = True
    options.strip_comments = True
    options.strip_ids = True
    options.shorten_ids = True
    options.keep_editor_data = False
    options.strip_xml_prolog = True
    options.enable_viewboxing = True
    options.simple_colors = True
    options.style_to_xml = True
    options.group_collapse = True
    options.group_create = True
    options.indent_type = "none"
    options.newlines = False
    options.digits = precision

    return scour.scourString(data.decode("utf-8"), options).encode("utf-8")


WEBP_TYPE = "image/webp"
# Content types this tool knows how to optimize. Anything absent (video/mp4,
# image/x-icon, fonts, text, ...) is left untouched. The value is the extension
# a file of that type is *renamed to* when this run converts something into it -
# it is never used to "correct" a file already stored under that type, so a
# `.jpeg` stays `.jpeg`.
OPTIMIZABLE = {
    "image/png": ".png",
    "image/jpeg": ".jpg",
    "image/gif": ".gif",
    WEBP_TYPE: ".webp",
    "image/svg+xml": ".svg",
}


def optimize_media(data: bytes, value: str, name: str, cfg: dict, pngquant_path: str, logger) -> tuple:
    """Optimizes one media file's bytes. Returns (new_bytes, produced_type) -
    the CONTENT TYPE actually produced, which differs from `value` only when
    --webp converts a raster or rasterizes an oversized SVG - or None when this
    file is deliberately left alone (animated WEBP/APNG).

    Returning the produced content type rather than a canonical extension is
    what keeps the default mode's promise that nothing is renamed. Returning an
    extension meant a file stored as "photo.jpeg" was compared against the
    canonical ".jpg" for image/jpeg, came out "different", and was renamed to
    photo.jpg - deleting the old row and breaking every link to it, in the mode
    documented as never renaming anything. A type is equal to itself, so the
    only thing that can now trigger a rename is a genuine format change."""
    if value == "image/svg+xml":
        out = optimize_svg(data, cfg["svg_precision"])
        if cfg["webp"] and len(out) > cfg["svg_rasterize_threshold"]:
            try:
                img = rasterize_svg(out.decode("utf-8"), cfg["max_width"])
                try:
                    return encode_webp(img, cfg["webp_quality"], cfg["max_width"]), WEBP_TYPE
                finally:
                    img.close()
            except Exception as exc:  # noqa: BLE001 - keep the vector instead
                logger(f"  note: could not rasterize {name} ({type(exc).__name__}); keeping optimized SVG")
        return out, "image/svg+xml"

    # Opened once and handed to the per-format encoders: testing is_animated
    # here and then letting each optimize_* re-open the same bytes decoded every
    # image twice, on the slowest path in the tool.
    with Image.open(io.BytesIO(data)) as img:
        if getattr(img, "is_animated", False):
            # Animated GIFs are resized frame by frame and stay GIFs; every
            # other animated format (APNG, animated WEBP) is left untouched,
            # since re-encoding those animations isn't implemented and
            # flattening them to one frame would silently break them.
            if value == "image/gif":
                return optimize_animated_gif(img, cfg["max_width"]), "image/gif"
            return None
        if cfg["webp"]:
            # --webp: every static raster becomes WEBP regardless of its source
            # format (pngquant is skipped - it only makes sense for PNG output).
            return encode_webp(img, cfg["webp_quality"], cfg["max_width"]), WEBP_TYPE
        if value == "image/png":
            return encode_png(img, pngquant_path, cfg["pngquant_speed"], cfg["max_width"], name), "image/png"
        if value == "image/jpeg":
            return encode_jpeg(img, cfg["jpeg_quality"], cfg["max_width"]), "image/jpeg"
        if value == "image/gif":
            return encode_static_gif(img, cfg["max_width"]), "image/gif"
        return encode_webp(img, cfg["webp_quality"], cfg["max_width"]), WEBP_TYPE


def target_path(old_path: str, new_ext: str, claimed: set) -> str:
    """The Content.path a converted file should be stored under: same directory
    and stem, new extension. Content.path is UNIQUE, so a target that's already
    spoken for - by a row that's staying put (an existing foo.webp), or by
    another file converting to the same name (foo.png and foo.jpg both wanting
    foo.webp) - is disambiguated by folding the source extension into the stem.

    The extension is split off the FILENAME, not the whole path: rpartition(".")
    over the full path finds a dot in a *directory* when the filename has no
    extension of its own, so "a/v1.2/logo" yielded the target "a/v1.webp" -
    a row in the wrong directory, while the real one was deleted."""
    directory, slash, filename = old_path.rpartition("/")
    stem, dot, old_ext = filename.rpartition(".")
    if not dot:  # no extension to replace
        return old_path
    prefix = f"{directory}{slash}{stem}"
    candidate = f"{prefix}{new_ext}"
    if candidate.lower() not in claimed:
        return candidate
    candidate = f"{prefix}-{old_ext.lower()}{new_ext}"
    attempt = 1
    while candidate.lower() in claimed:
        attempt += 1
        candidate = f"{prefix}-{old_ext.lower()}-{attempt}{new_ext}"
    return candidate


# --- DB plumbing -------------------------------------------------------------

def backup_database(db_path: Path) -> Path:
    """VACUUM INTO a timestamped sibling, same approach as populate_db.py.

    VACUUM INTO refuses to write a file that already exists, so a second-
    resolution name made two runs inside the same second fail on the backup
    before doing any work. Take the next free suffix instead of dying."""
    stamp = time.strftime("%Y%m%d-%H%M%S")
    backup_path = db_path.with_name(f"{db_path.name}.backup-{stamp}")
    attempt = 1
    while backup_path.exists():
        attempt += 1
        backup_path = db_path.with_name(f"{db_path.name}.backup-{stamp}-{attempt}")
    conn = sqlite3.connect(db_path)
    try:
        conn.execute("VACUUM INTO ?", (str(backup_path),))
    finally:
        conn.close()
    return backup_path


class BrotliCodec:
    """Brotli (de)compression for a Content row's "brotli" bytes.

    Two databases exist in this repo's lineage: older ones store plain Brotli
    (handled by the Python `brotli` package), and ones populate_db.py has
    touched store Brotli against a fixed 256 KiB shared dictionary from the
    CompressionDictionary table (ADFA-5153). Those two streams are NOT
    interchangeable, and the Python package has no dictionary parameter at
    all, so a dictionary database is handled by shelling out to the `brotli`
    CLI with `-D <dict>` - exactly as populate_db.py's DictionaryCompressor
    does. Every row must be decompressed with the same dictionary it was
    compressed against, so the dictionary is read straight from the database
    being optimized; there is no other copy to get out of sync with."""

    def __init__(self, dictionary: bytes | None):
        self._dictionary = dictionary
        self._work_dir = None
        if dictionary is not None:
            self._brotli_path = shutil.which("brotli")
            if self._brotli_path is None:
                raise RuntimeError(
                    "this database dictionary-compresses its brotli content, which needs the `brotli` CLI "
                    "on PATH (e.g. `brew install brotli`); install it and retry"
                )
            self._work_dir = Path(tempfile.mkdtemp(prefix="optimize_db_media_brotli_"))
            self._dict_path = self._work_dir / "dictionary.bin"
            self._dict_path.write_bytes(dictionary)

    def _run(self, *extra_args: str, data: bytes) -> bytes:
        result = subprocess.run(
            [self._brotli_path, "-D", str(self._dict_path), *extra_args, "-c"],
            input=data, stdout=subprocess.PIPE, stderr=subprocess.PIPE, check=False,
        )
        if result.returncode != 0:
            raise RuntimeError(f"brotli failed: {result.stderr.decode(errors='replace').strip()}")
        return result.stdout

    def decompress(self, blob: bytes, compression: str) -> bytes:
        if compression != "brotli":
            return bytes(blob)
        if self._dictionary is None:
            return brotli.decompress(blob)
        return self._run("-d", data=blob)

    def compress(self, data: bytes, compression: str) -> bytes:
        if compression != "brotli":
            return data
        if self._dictionary is None:
            return brotli.compress(data)
        return self._run(data=data)

    def close(self) -> None:
        if self._work_dir is not None:
            shutil.rmtree(self._work_dir, ignore_errors=True)


def load_dictionary(conn) -> bytes | None:
    """The shared Brotli dictionary bytes from CompressionDictionary if this
    database has that table populated, else None (an older plain-Brotli
    database). Never trains one - a database that needs a dictionary already
    has the exact bytes every existing row was compressed against."""
    has_table = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name='CompressionDictionary'"
    ).fetchone()
    if not has_table:
        return None
    row = conn.execute("SELECT data FROM CompressionDictionary WHERE id = 1").fetchone()
    return row[0] if row else None


def human(n: int) -> str:
    return f"{n:,}"


def delete_media_row(conn, base_path: str, base_length: int) -> None:
    """Removes a media item entirely: its base row and every continuation row
    it owns (see owned_fragment_paths - ownership, not just what the server
    would serve, so a gapped chain's tail isn't orphaned)."""
    for fragment_path in owned_fragment_paths(conn, base_path, base_length):
        conn.execute("DELETE FROM Content WHERE path = ?", (fragment_path,))
    conn.execute("DELETE FROM Content WHERE path = ?", (base_path,))


def clear_fragment_slots(conn, base_path: str, count: int, logger) -> None:
    """Frees the "<base_path>-1..count" paths this write is about to insert.

    They are normally already gone (delete_media_row / owned_fragment_paths took
    the old chain), but a row can sit at one of those names without being an
    owned fragment: content_chunking notes "guide.html" and "guide.html-1" are
    legal as two unrelated pages, and owned_fragment_paths deliberately returns
    nothing when the base was not previously CHUNK_SIZE. Inserting over that row
    raised a UNIQUE violation which propagated out and rolled back the ENTIRE
    run - every image optimized so far, lost to one filename coincidence.

    Such a row is unreachable anyway once this item is chunked: the server
    concatenates "<base>-1", "-2", ... to serve base_path, so it would be read
    as this file's bytes, not as itself. Removing it is what makes the database
    consistent; it is logged because it is destructive."""
    for number in range(1, count + 1):
        fragment_path = f"{base_path}-{number}"
        if conn.execute("SELECT 1 FROM Content WHERE path = ?", (fragment_path,)).fetchone():
            logger(f"  warning: removing existing row at {fragment_path}; it collides with a chunk "
                   f"continuation of {base_path} and the server would serve it as part of that file")
            conn.execute("DELETE FROM Content WHERE path = ?", (fragment_path,))


def write_chunks(conn, path: str, stored: bytes, language_id: int, content_type_id: int,
                 template_id: int, logger, update_base: bool) -> None:
    """Writes `stored` at `path`, splitting anything over CHUNK_SIZE across
    "<path>-1", "-2", ... Every fragment reuses the base row's languageID,
    contentTypeID and templateId, matching populate_db.insert_chunked_content's
    documented convention ("Every fragment reuses the first row's
    languageID/contentTypeID/templateId for consistency")."""
    chunks = [stored[i:i + CHUNK_SIZE] for i in range(0, len(stored), CHUNK_SIZE)] or [b""]
    clear_fragment_slots(conn, path, len(chunks) - 1, logger)
    if update_base:
        conn.execute("UPDATE Content SET content = ?, contentTypeID = ? WHERE path = ?",
                     (chunks[0], content_type_id, path))
    else:
        conn.execute(
            "INSERT INTO Content (path, languageID, content, contentTypeID, templateId) VALUES (?, ?, ?, ?, ?)",
            (path, language_id, chunks[0], content_type_id, template_id),
        )
    for number, chunk in enumerate(chunks[1:], start=1):
        conn.execute(
            "INSERT INTO Content (path, languageID, content, contentTypeID, templateId) VALUES (?, ?, ?, ?, ?)",
            (f"{path}-{number}", language_id, chunk, content_type_id, template_id),
        )


def convert_media_row(conn, old_path: str, old_base_length: int, new_path: str, new_stored: bytes,
                      language_id: int, new_content_type_id: int, template_id: int, logger) -> None:
    """Replaces a media item with a converted one stored under a different
    path/extension: the old row (and its chunk chain) is deleted and the new
    bytes are inserted at the new path with the new content type. Deleting
    first keeps Content.path's UNIQUE constraint satisfied even in the corner
    case where the new path equals some other row this run already removed."""
    delete_media_row(conn, old_path, old_base_length)
    write_chunks(conn, new_path, new_stored, language_id, new_content_type_id, template_id,
                 logger, update_base=False)


def write_media_row(conn, base_path: str, base_length: int, new_stored: bytes,
                    language_id: int, content_type_id: int, template_id: int, logger) -> None:
    """Replaces a media item's stored bytes in place, deleting whatever chunk
    chain it used to have and re-chunking the new bytes if they still exceed
    CHUNK_SIZE (they almost never do after downscaling, but a correct write
    can't assume that)."""
    for fragment_path in owned_fragment_paths(conn, base_path, base_length):
        conn.execute("DELETE FROM Content WHERE path = ?", (fragment_path,))
    write_chunks(conn, base_path, new_stored, language_id, content_type_id, template_id,
                 logger, update_base=True)


def run(cfg: dict) -> int:
    db_path = cfg["db_path"]
    if not db_path.is_file():
        print(f"error: {db_path} does not exist", file=sys.stderr)
        return 1

    # pngquant is only ever invoked to quantize PNG *output*. Under --webp no
    # PNG is written at all (every static raster becomes WEBP), so requiring
    # the binary up front would refuse a run that never needs it.
    if cfg["webp"]:
        # Under --webp no PNG or JPEG is ever written, so these tuning knobs
        # silently do nothing; say so rather than letting someone believe a
        # quality setting took effect.
        ignored = [flag for flag, key, default in
                   (("--pngquant-speed", "pngquant_speed", DEFAULTS["pngquant_speed"]),
                    ("--jpeg-quality", "jpeg_quality", DEFAULTS["jpeg_quality"]))
                   if cfg[key] != default]
        if ignored:
            print(f"warning: {', '.join(ignored)} has no effect with --webp (no PNG/JPEG is written); "
                  "use --webp-quality instead", file=sys.stderr)

    pngquant_path = None
    if not cfg["webp"]:
        try:
            pngquant_path = find_pngquant()
        except RuntimeError as exc:
            print(f"error: {exc}", file=sys.stderr)
            return 1

    if cfg["dry_run"]:
        print(f"Dry run: no backup, no changes committed. Optimizing media in {db_path} ...")
    else:
        print(f"Backing up {db_path} ...")
        backup_path = backup_database(db_path)
        print(f"Backup written to {backup_path}")

    conn = sqlite3.connect(db_path)
    stats = {"optimized": 0, "converted": 0, "no_gain": 0, "skipped_type": 0, "skipped_animated": 0,
             "skipped_fragment": 0, "errors": 0, "before": 0, "after": 0, "saved": 0}
    per_type = {}
    codec = None
    try:
        conn.execute("BEGIN")
        # languageID/contentTypeID are needed only if a re-chunk write inserts
        # new continuation rows; read the language once (this DB has one).
        language_id = conn.execute("SELECT id FROM Languages LIMIT 1").fetchone()[0]
        # Content type id + compression for every format a conversion can
        # produce, looked up once. Missing here is fatal for that format only
        # when something actually converts to it (checked at use).
        type_info = {}
        for type_value in OPTIMIZABLE:
            row = conn.execute("SELECT id, compression FROM ContentTypes WHERE value = ?", (type_value,)).fetchone()
            if row:
                type_info[type_value] = (row[0], row[1])
        if cfg["webp"] and WEBP_TYPE not in type_info:
            print("error: --webp needs an 'image/webp' row in ContentTypes; this database has none",
                  file=sys.stderr)
            conn.rollback()
            return 1
        # Every path already in use, so a converted file never collides with a
        # row that's staying put. Casefolded: two paths differing only in case
        # would still be one UNIQUE key clash risk on a case-insensitive read.
        claimed = {row[0].lower() for row in conn.execute("SELECT path FROM Content")}
        codec = BrotliCodec(load_dictionary(conn))
        if codec._dictionary is not None:
            print(f"Using the database's {len(codec._dictionary):,}-byte shared Brotli dictionary "
                  "for image/svg+xml and image/webp rows.")

        rows = conn.execute(
            "SELECT c.path, c.content, c.contentTypeID, ct.value, ct.compression, c.templateId "
            "FROM Content c JOIN ContentTypes ct ON c.contentTypeID = ct.id "
            "WHERE ct.value LIKE 'image/%' ORDER BY c.path"
        ).fetchall()
        # {path: stored length} over every image row, so a "<base>-<N>" row can
        # be recognized as a continuation only when its base is exactly
        # CHUNK_SIZE bytes (see content_chunking) rather than by its name alone.
        lengths = {path: len(blob) for path, blob, _c, _v, _cmp, _t in rows}
        print(f"Scanning {len(rows)} image row(s) ({sum(1 for p in lengths if is_continuation_path(lengths, p))} "
              "chunk-continuation row(s) will be folded into their base)...")
        warn = lambda message: print(message, file=sys.stderr)  # noqa: E731

        for path, blob, content_type_id, value, compression, template_id in rows:
            if is_continuation_path(lengths, path):
                stats["skipped_fragment"] += 1  # handled as part of its base row
                continue

            if value not in OPTIMIZABLE:
                stats["skipped_type"] += 1
                continue

            name = path.rsplit("/", 1)[-1]
            stored_full = reassemble(conn, path, blob)  # base + its served chunks
            original_stored = len(stored_full)
            try:
                media_bytes = codec.decompress(stored_full, compression)
                result = optimize_media(media_bytes, value, name, cfg, pngquant_path, warn)
                if result is None:  # animated WEBP/APNG: deliberately left alone
                    stats["skipped_animated"] += 1
                    continue
                new_media, new_type = result
                # A converted file is stored under the content type of the
                # format actually produced, and compressed the way *that* type
                # says to - not however the source happened to be stored.
                new_type_id, new_compression = type_info.get(new_type, (content_type_id, compression))
                new_stored = codec.compress(new_media, new_compression)
            except Exception as exc:  # noqa: BLE001 - one bad image is not the whole run
                stats["errors"] += 1
                print(f"  error: failed to optimize {path}: {exc}", file=sys.stderr)
                continue

            stats["before"] += original_stored
            if len(new_stored) >= original_stored:
                # Never grow a file, and never rename one for no benefit.
                stats["no_gain"] += 1
                stats["after"] += original_stored
                continue

            # Only a genuine format change renames anything: a file already
            # stored under the type it was re-encoded to keeps its own path,
            # whatever extension that path happens to use.
            new_path = path if new_type == value else target_path(path, OPTIMIZABLE[new_type], claimed)
            converted = new_path != path
            if not cfg["dry_run"]:
                if converted:
                    convert_media_row(conn, path, len(blob), new_path, new_stored, language_id,
                                      new_type_id, template_id, warn)
                else:
                    write_media_row(conn, path, len(blob), new_stored, language_id, new_type_id,
                                    template_id, warn)
            if converted:
                claimed.add(new_path.lower())
                claimed.discard(path.lower())
                stats["converted"] += 1
            stats["optimized"] += 1
            stats["after"] += len(new_stored)
            saved = original_stored - len(new_stored)
            stats["saved"] += saved
            bucket = per_type.setdefault(value, {"n": 0, "saved": 0, "converted": 0})
            bucket["n"] += 1
            bucket["saved"] += saved
            bucket["converted"] += 1 if converted else 0
            if cfg["verbose"]:
                pct = saved / original_stored * 100 if original_stored else 0.0
                arrow = f" -> {new_path}" if converted else ""
                print(f"  [OPT] {path}{arrow}: {human(original_stored)} -> {human(len(new_stored))} "
                      f"(saved {human(saved)}, {pct:.1f}%)")

        if cfg["dry_run"]:
            conn.rollback()
        else:
            conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        if codec is not None:
            codec.close()
        conn.close()

    if not cfg["dry_run"] and stats["optimized"]:
        print("Vacuuming database to reclaim freed space...")
        vac = sqlite3.connect(db_path)
        try:
            vac.execute("VACUUM")
        finally:
            vac.close()

    pct = stats["saved"] / stats["before"] * 100 if stats["before"] else 0.0
    verb = "would optimize" if cfg["dry_run"] else "optimized"
    print()
    print(f"{'Dry run complete. ' if cfg['dry_run'] else 'Done. '}"
          f"{verb} {stats['optimized']} image(s), of which {stats['converted']} changed format "
          f"(new extension, old row deleted); {stats['no_gain']} already minimal, "
          f"{stats['skipped_type']} untouched (non-optimizable type), "
          f"{stats['skipped_animated']} animated WEBP/APNG left alone, "
          f"{stats['skipped_fragment']} chunk-fragment row(s) folded into their base, "
          f"{stats['errors']} error(s).")
    for value in sorted(per_type):
        b = per_type[value]
        print(f"  {value}: {b['n']} optimized ({b['converted']} converted), saved {human(b['saved'])} bytes")
    print(f"Image bytes {'that would go' if cfg['dry_run'] else 'gone'} from "
          f"{human(stats['before'])} -> {human(stats['after'])} "
          f"(saved {human(stats['saved'])}, {pct:.1f}%).")
    return 1 if stats["errors"] else 0


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("db_path", type=Path, help="SQLite database to optimize in place, e.g. documentation.db")
    p.add_argument("--dry-run", action="store_true",
                   help="Do all the work and report savings, then roll back without writing or backing up")
    p.add_argument("--webp", action="store_true",
                   help="Convert every static raster (PNG/JPEG/GIF) to WEBP, and rasterize an SVG still over "
                        "--svg-rasterize-threshold after minifying. Converted files are stored under a new "
                        "path ending .webp and their old row is deleted; references to the old name are NOT "
                        "rewritten")
    p.add_argument("--svg-rasterize-threshold", type=int, default=DEFAULTS["svg_rasterize_threshold"],
                   help="With --webp, rasterize a minified SVG still larger than this many bytes "
                        f"(default: {DEFAULTS['svg_rasterize_threshold']:,})")
    p.add_argument("--max-width", type=int, default=DEFAULTS["max_width"],
                   help=f"Max raster width in px, never upscaled (default: {DEFAULTS['max_width']})")
    p.add_argument("--jpeg-quality", type=int, default=DEFAULTS["jpeg_quality"],
                   help=f"JPEG quality 0-95 (default: {DEFAULTS['jpeg_quality']})")
    p.add_argument("--webp-quality", type=int, default=DEFAULTS["webp_quality"],
                   help=f"WEBP quality 0-100 (default: {DEFAULTS['webp_quality']})")
    p.add_argument("--pngquant-speed", type=int, default=DEFAULTS["pngquant_speed"],
                   help=f"pngquant speed/quality 1(best)-11(rough) (default: {DEFAULTS['pngquant_speed']})")
    p.add_argument("--svg-precision", type=int, default=DEFAULTS["svg_precision"],
                   help=f"Decimal places Scour rounds SVG numbers to (default: {DEFAULTS['svg_precision']})")
    p.add_argument("--verbose", action="store_true", help="Log every optimized image, with byte sizes")
    return p


def main() -> None:
    args = build_parser().parse_args()
    cfg = {
        "db_path": args.db_path, "dry_run": args.dry_run, "max_width": args.max_width,
        "jpeg_quality": args.jpeg_quality, "webp_quality": args.webp_quality,
        "pngquant_speed": args.pngquant_speed, "svg_precision": args.svg_precision, "verbose": args.verbose,
        "webp": args.webp, "svg_rasterize_threshold": args.svg_rasterize_threshold,
    }
    sys.exit(run(cfg))


if __name__ == "__main__":
    main()
