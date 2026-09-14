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

Re-running over a database someone else has edited (--manifest-out/--manifest-in):
  This database gets handed to authors who cannot run these scripts. Running
  the tool again over their work must not re-encode what it already encoded -
  lossy output re-encoded from lossy input loses quality every time - but must
  still pick up whatever they changed. --manifest-out writes a text record of
  every media file this tool manages (stored size, sha256, and the path it was
  converted from); passing it back as --manifest-in on the next run makes the
  three things an author can do come out right:

    * A file they ADDED is absent from the manifest, so it is optimized.
    * A file they OVERWROTE in place no longer matches its recorded digest,
      so it is optimized again - the size is checked too, but the digest is
      what decides, since an edit can easily land on the same byte count.
    * A source image they RE-INSERTED in place of one converted earlier (a
      .png whose .webp this tool produced) is recognised through the
      manifest's provenance column and converted back over that same .webp,
      replacing it. Without that column the new .png would convert to a
      de-conflicted name like foo-png.webp, which nothing links to, while the
      stale foo.webp every page still points at stayed put.

  Everything else in the manifest is skipped untouched. Rows that errored are
  deliberately left out, so a later run retries them.

Keeping the originals (--save-originals / --restore-originals):
  Optimizing is lossy, and under --webp it renames files too, so once this has
  run the database no longer holds what it started from. --save-originals
  <bundle> writes the way back: every media file's ORIGINAL decoded bytes, laid
  out under <bundle>/originals/<Content.path> as real .png/.svg/... files, plus
  <bundle>/originals.tsv recording for each one its digest and size, the
  content type / languageID / templateId its row carried, where in the bundle
  the bytes sit, and - filled in after the fact - the path it was converted to.
  That last column is what makes the bundle a rollback rather than just a copy:
  it names the row that has to go when the old one comes back.

  Every image row is archived, not only the ones this run rewrites and not only
  the optimizable types, so the bundle really is all of the originals and can
  be re-optimized from later at different settings instead of re-encoding this
  run's lossy output. The single exception is a row --manifest-in vouches for:
  those bytes are this tool's own output from an earlier run, and filing them
  as "the original" would overwrite the real original with a lossy copy of
  itself. They are left out and counted. Refilling a bundle without passing
  that run's manifest back is the one way to get optimized media archived as
  though it were original, so doing so warns.

  Archiving is a precondition, not a side effect: an image whose original
  cannot be written to the bundle - a full disk, a permission problem, a
  Content.path that is not a usable filename - is left UNOPTIMIZED, and the run
  exits non-zero. Rewriting it would be precisely the loss this flag was asked
  to prevent. For the same reason the index is written inside the transaction,
  before the commit: bytes under originals/ are unusable without the paths and
  content types that name them, so a failure to write it rolls the whole run
  back rather than leaving a conversion nothing can undo.

  The bundle is additive, so several runs can share one: a file already
  archived byte for byte is left alone, and one whose bytes have CHANGED since
  (an author overwrote it) is filed under <bundle>/revisions/<sha>/ instead of
  replacing the original already sitting there. "Already archived" is checked
  against the file, not just the index - originals/ is the bulk of a bundle's
  size and so the obvious thing to delete to reclaim space, and an index entry
  whose bytes have gone is written again rather than trusted.

  --restore-originals <bundle> is the inverse - each archived original goes
  back at its own path with its own content type and its own languageID and
  templateId, and whatever it had been converted into is deleted. It first
  checks the bundle plausibly describes the database it is pointed at, the way
  update_media_references.py checks --before, since several same-named
  databases sit side by side and restoring the wrong bundle into one would
  inject every archived asset into it; --force-restore overrides that when
  media really was deleted between the two runs. Media is only half the
  picture: run
  update_media_references.py --restore-originals over the same bundle to undo
  the reference rewriting as well, and between them the original documentation
  is reconstructed.

  --save-originals and --dry-run are mutually exclusive. A dry run changes
  nothing, so there is nothing to undo, and a bundle whose converted-to column
  described conversions that never happened would be worse than no bundle.

Python dependencies (Pillow, scour, brotli) are declared inline above (PEP
723), so uv installs them on the fly - run it one-shot with no setup:

    uv run scripts/optimize_db_media.py documentation.db

The "pngquant" and "brotli" command-line tools must also be on PATH (e.g.
`brew install pngquant brotli`); those are system binaries, not pip packages,
so uv can't provide them.
"""
from __future__ import annotations

import argparse
import hashlib
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


# --- media manifest ---------------------------------------------------------
#
# A record of what this tool has already optimized, so a later run can leave
# that work alone. It exists because the database is handed to authors who
# cannot run these scripts: they add new files, overwrite existing ones, and
# sometimes re-insert a source image (a .png) whose optimized form (.webp) is
# already here. Re-optimizing an image that is already optimized is not a
# no-op - it re-encodes lossy output from lossy input and loses quality every
# time - so "have I done this one already?" has to be answerable.
#
# Plain text, one record per media file, sorted by path so diffs are readable:
#
#     <sha256 of stored bytes>\t<stored bytes>\t<path>\t<source path or ->
#
# The size is what the operator asked for and what makes the file skimmable;
# the digest is what makes the decision safe, since an edited file can easily
# land on its predecessor's byte count. The fourth field is provenance: the
# path this file was converted FROM, which is what lets a re-inserted source
# replace the stale output it once produced instead of piling up beside it.
MANIFEST_HEADER = "# optimize_db_media media manifest v1"
MANIFEST_COLUMNS = "# sha256\tbytes\tpath\tconverted-from"


def digest(stored: bytes) -> str:
    return hashlib.sha256(stored).hexdigest()


def read_manifest(path: Path) -> dict:
    """{stored path: (sha256, size, source path or None)} from a manifest file.

    Unparseable lines are a hard error rather than a shrug: a manifest that is
    silently half-read looks exactly like one describing a database where half
    the work was never done, and the run would redo - and re-degrade - every
    file it failed to read a line for."""
    entries = {}
    for number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), start=1):
        if not line.strip() or line.startswith("#"):
            continue
        fields = line.split("\t")
        if len(fields) != 4:
            raise RuntimeError(f"{path}:{number}: expected 4 tab-separated fields, got {len(fields)}")
        sha, size, stored_path, source = fields
        if not size.isdigit():
            raise RuntimeError(f"{path}:{number}: size {size!r} is not a number")
        entries[stored_path] = (sha, int(size), None if source == "-" else source)
    return entries


def write_manifest(path: Path, entries: dict) -> None:
    """Writes {path: (sha, size, source)} out, sorted by path."""
    lines = [MANIFEST_HEADER, MANIFEST_COLUMNS,
             f"# written {time.strftime('%Y-%m-%dT%H:%M:%S')} - {len(entries)} media file(s)"]
    for stored_path in sorted(entries):
        sha, size, source = entries[stored_path]
        lines.append(f"{sha}\t{size}\t{stored_path}\t{source or '-'}")
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


# --- original-asset archive --------------------------------------------------
#
# The rollback bundle --save-originals writes and --restore-originals reads
# (see the module docstring). update_media_references.py writes its own two
# files into the same directory, so one bundle describes the whole conversion:
#
#     <bundle>/originals.tsv            index of every archived asset
#     <bundle>/originals/<path>         its original decoded bytes
#     <bundle>/revisions/<sha12>/<path> a later original for an already-archived
#                                       path, kept rather than overwriting it
#     <bundle>/renames.tsv              old filename -> new filename
#     <bundle>/references.tsv           where each rewritten reference sat
#
# Decoded bytes are archived, not the stored blob: a real .png on disk is
# usable on its own, and re-compressing it on restore reproduces the same
# content even though Brotli need not emit byte-identical output.
BUNDLE_ORIGINALS_INDEX = "originals.tsv"
BUNDLE_ORIGINALS_DIR = "originals"
BUNDLE_REVISIONS_DIR = "revisions"
ORIGINALS_HEADER = "# optimize_db_media original-asset archive v1"
ORIGINALS_COLUMNS = ("# sha256\tbytes\tpath\tcontent-type\tlanguageID\ttemplateId\tarchived-as"
                     "\tconverted-to")


def write_atomically(path: Path, text: str) -> Path:
    """Writes through a temporary file and renames it into place, so a failing
    or interrupted write leaves the previous contents rather than a truncated
    file the reader would reject - taking the whole bundle with it. The
    temporary is removed on failure so a half-written one is not left beside
    the real file for someone to mistake for it.

    Lives here rather than in update_media_references.py because both halves of
    the bundle need it and the import runs in this direction."""
    temporary = path.with_name(path.name + ".tmp")
    try:
        temporary.write_text(text, encoding="utf-8")
        temporary.replace(path)
    except OSError:
        temporary.unlink(missing_ok=True)
        raise
    return path


def bundle_relative(stored_path: str):
    """`stored_path` as a location safe to join onto the bundle directory, or
    None if it cannot be one. Content.path is data from the database, and this
    is the one place it becomes a filesystem path, so an absolute path or a
    ".." segment is refused rather than allowed to write outside the bundle."""
    if stored_path.startswith("/"):
        return None
    parts = [part for part in stored_path.split("/") if part not in ("", ".")]
    if not parts or ".." in parts:
        return None
    return "/".join(parts)


def read_originals_index(path: Path) -> list:
    """The archive index as a list of mutable 8-field records, in file order.

    Unparseable lines are fatal for the same reason read_manifest treats them
    that way: a half-read index is indistinguishable from one describing a
    smaller archive, and acting on it would either skip archiving originals
    that are not really there or restore only part of a database."""
    entries = []
    for number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), start=1):
        if not line.strip() or line.startswith("#"):
            continue
        fields = line.split("\t")
        if len(fields) != 8:
            raise RuntimeError(f"{path}:{number}: expected 8 tab-separated fields, got {len(fields)}")
        sha, size, stored_path, content_type, language_id, template_id, location, converted = fields
        for label, number_text in (("bytes", size), ("languageID", language_id), ("templateId", template_id)):
            if not number_text.isdigit():
                raise RuntimeError(f"{path}:{number}: {label} {number_text!r} is not a number")
        # The location is joined onto the bundle directory and read back, so it
        # gets the same check the write side applies to a Content.path: an
        # index naming "../../etc/hosts" would otherwise have restore read that
        # file and, on a digest match, store it as a media row. Requiring one of
        # the two known prefixes also keeps a hand-edited index from pointing
        # anywhere else inside the bundle.
        if (bundle_relative(location) != location
                or not location.startswith((f"{BUNDLE_ORIGINALS_DIR}/", f"{BUNDLE_REVISIONS_DIR}/"))):
            raise RuntimeError(f"{path}:{number}: archived-as {location!r} is not a location inside the "
                               f"bundle's {BUNDLE_ORIGINALS_DIR}/ or {BUNDLE_REVISIONS_DIR}/ directory")
        entries.append([sha, int(size), stored_path, content_type, int(language_id), int(template_id),
                        location, None if converted == "-" else converted])
    return entries


class OriginalsArchive:
    """Collects original asset bytes on disk and the index describing them.

    Bytes are written as each file is archived (so a long run is not holding
    the whole database in memory), while the index is written once at the end,
    because a record is only complete after the optimizer has said what - if
    anything - that file was converted into."""

    def __init__(self, root: Path, logger):
        self.root = root
        self._logger = logger
        self._entries = []        # every record, in the order archived
        self._by_path = {}        # stored path -> its records, primary first
        # Casefolded bundle location -> the Content.path that owns it. Two
        # different Content.path values can want the same file: SQLite's UNIQUE
        # is case-sensitive but macOS's default filesystem is not, and
        # bundle_relative collapses "a//b.png" and "a/./b.png" onto one name.
        # Whoever gets there first keeps it; see add().
        self._locations = {}
        self.saved = 0
        self.reused = 0
        self.revisions = 0
        self.skipped = 0
        self.preloaded = 0
        self.replaced_missing = 0
        index_path = root / BUNDLE_ORIGINALS_INDEX
        if index_path.is_file():
            # Re-pointed at a bundle an earlier run filled in. Loading it is
            # what makes the bundle additive: a file already in here keeps the
            # original it already has instead of being handed this run's input,
            # which by then may be the previous run's output.
            for entry in read_originals_index(index_path):
                self._record(entry)
            self.preloaded = len(self._entries)
            self._logger(f"Archive {index_path} already describes {self.preloaded} original(s); "
                         "adding to it.")

    def _record(self, entry: list) -> None:
        self._by_path.setdefault(entry[2], []).append(entry)
        self._locations.setdefault(entry[6].lower(), entry[2])
        self._entries.append(entry)

    def add(self, stored_path: str, content_type: str, language_id: int, template_id: int,
            media_bytes: bytes) -> bool:
        """Archives one asset's original, decoded bytes, and reports whether
        this file's original is now safely in the bundle. True also covers
        "these exact bytes were already archived under this path"; False means
        the caller must not go on to rewrite the row, because there is no copy
        of what it is about to destroy."""
        sha = digest(media_bytes)
        existing = self._by_path.get(stored_path, [])
        match = next((entry for entry in existing if entry[0] == sha), None)
        if match is not None:
            # An index entry is not a copy. The bundle's originals/ directory is
            # the bulk of its size (136 MB against a 300 KB index on the
            # production database), so it is exactly what someone prunes to
            # reclaim space - and trusting the index alone would then report
            # every pruned asset as safely archived and let the caller destroy
            # it. Check the file is really there, and put it back if it is not.
            if (self.root / match[6]).is_file():
                self.reused += 1
                return True
            self._logger(f"  warning: {match[6]} is in the index but missing from the bundle; "
                         "writing it again")
            if not self._write(self.root / match[6], media_bytes, stored_path):
                return False
            self.replaced_missing += 1
            return True
        relative = bundle_relative(stored_path)
        if relative is None:
            self._logger(f"  warning: not archiving {stored_path!r}: it is not a safe relative path")
            self.skipped += 1
            return False
        primary = not existing
        location = (f"{BUNDLE_ORIGINALS_DIR}/{relative}" if primary
                    else f"{BUNDLE_REVISIONS_DIR}/{sha[:12]}/{relative}")
        owner = self._locations.get(location.lower())
        if owner is not None and owner != stored_path:
            # Another Content.path already owns that file in the bundle (see
            # _locations). Give this one a subdirectory of its own rather than
            # overwriting an original that belongs to a different row - which
            # would leave the loser unrestorable, reported only as a digest
            # mismatch much later.
            marker = hashlib.sha256(stored_path.encode("utf-8")).hexdigest()[:8]
            head, _, tail = location.partition("/")
            location = f"{head}/{marker}/{tail}"
            self._logger(f"  note: {stored_path} and {owner} map to the same file in the bundle; "
                         f"filing this one under {location}")
        if not self._write(self.root / location, media_bytes, stored_path):
            return False
        if primary:
            self.saved += 1
        else:
            self._logger(f"  note: {stored_path} differs from the original already archived for it; "
                         f"keeping that one and filing this copy under {location}")
            self.revisions += 1
        self._record([sha, len(media_bytes), stored_path, content_type, language_id, template_id,
                      location, None])
        return True

    def _write(self, destination: Path, media_bytes: bytes, stored_path: str) -> bool:
        """Puts one asset's bytes on disk, counting a refusal rather than
        raising: the caller turns False into "do not rewrite this row"."""
        try:
            destination.parent.mkdir(parents=True, exist_ok=True)
            destination.write_bytes(media_bytes)
        except OSError as exc:
            self._logger(f"  warning: could not archive {stored_path}: {exc}")
            self.skipped += 1
            return False
        return True

    def mark_converted(self, stored_path: str, new_path: str) -> None:
        """Records that the file archived from `stored_path` now lives at
        `new_path`, so restoring the original knows which row to remove. Every
        record for the path is marked, not just the primary, so each line of
        the index reads correctly on its own."""
        for entry in self._by_path.get(stored_path, []):
            entry[7] = new_path

    def write_index(self) -> Path:
        """Writes the index, atomically (see write_atomically)."""
        index_path = self.root / BUNDLE_ORIGINALS_INDEX
        lines = [ORIGINALS_HEADER, ORIGINALS_COLUMNS,
                 f"# written {time.strftime('%Y-%m-%dT%H:%M:%S')} - {len(self._entries)} archived original(s)"]
        # Sorted by path, then by location, so a path's primary ("originals/")
        # always precedes its revisions ("revisions/") and diffs stay readable.
        for entry in sorted(self._entries, key=lambda e: (e[2], e[6])):
            sha, size, stored_path, content_type, language_id, template_id, location, converted = entry
            lines.append(f"{sha}\t{size}\t{stored_path}\t{content_type}\t{language_id}\t{template_id}\t"
                         f"{location}\t{converted or '-'}")
        index_path.parent.mkdir(parents=True, exist_ok=True)
        return write_atomically(index_path, "\n".join(lines) + "\n")


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

    # Checked here and not only in main(): archiving writes asset bytes during
    # the loop and an index before the commit, so a caller that set both would
    # get a populated bundle whose converted-to column described a run that was
    # rolled back. main() still rejects the combination for a nicer message.
    if cfg["save_originals"] is not None and cfg["dry_run"]:
        print("error: --save-originals cannot be combined with a dry run: a dry run makes no changes to "
              "undo, and the bundle would describe conversions that never happened", file=sys.stderr)
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

    known = {}
    if cfg["manifest_in"] is not None:
        if not cfg["manifest_in"].is_file():
            print(f"error: --manifest-in {cfg['manifest_in']} does not exist", file=sys.stderr)
            return 1
        try:
            known = read_manifest(cfg["manifest_in"])
        except RuntimeError as exc:
            print(f"error: {exc}", file=sys.stderr)
            return 1
        print(f"Manifest {cfg['manifest_in']} lists {len(known)} already-optimized media file(s).")
    # {source path: path it was converted to}, so a re-inserted source can
    # replace the stale output it produced last time instead of landing beside it.
    produced_from_source = {source: stored for stored, (_sha, _size, source) in known.items() if source}

    archive = None
    index_path = None
    if cfg["save_originals"] is not None:
        try:
            cfg["save_originals"].mkdir(parents=True, exist_ok=True)
            archive = OriginalsArchive(cfg["save_originals"], lambda message: print(message, file=sys.stderr))
        except (OSError, RuntimeError) as exc:
            print(f"error: cannot use {cfg['save_originals']} as an originals archive: {exc}", file=sys.stderr)
            return 1
        if archive.preloaded and cfg["manifest_in"] is None:
            # --manifest-in is the only thing that tells this run's input apart
            # from an earlier run's output, and the two flags are independent.
            # Refilling a bundle without one files optimized media under
            # originals/ as though it were the original it was made from.
            print("warning: this bundle already describes an earlier run, but no --manifest-in was given. "
                  "Media that run already optimized cannot be recognised, so it will be archived as though "
                  "it were original. Pass the --manifest-out from that run as --manifest-in.",
                  file=sys.stderr)

    if cfg["dry_run"]:
        print(f"Dry run: no backup, no changes committed. Optimizing media in {db_path} ...")
    else:
        print(f"Backing up {db_path} ...")
        backup_path = backup_database(db_path)
        print(f"Backup written to {backup_path}")

    conn = sqlite3.connect(db_path)
    stats = {"optimized": 0, "converted": 0, "no_gain": 0, "skipped_type": 0, "skipped_animated": 0,
             "skipped_manifest": 0, "skipped_fragment": 0, "replaced_stale": 0, "errors": 0,
             "archive_errors": 0, "archive_not_original": 0, "archive_blocked": 0,
             "before": 0, "after": 0, "saved": 0}
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
            "SELECT c.path, c.content, c.contentTypeID, ct.value, ct.compression, c.templateId, "
            "c.languageID FROM Content c JOIN ContentTypes ct ON c.contentTypeID = ct.id "
            "WHERE ct.value LIKE 'image/%' ORDER BY c.path"
        ).fetchall()
        # {path: stored length} over every image row, so a "<base>-<N>" row can
        # be recognized as a continuation only when its base is exactly
        # CHUNK_SIZE bytes (see content_chunking) rather than by its name alone.
        lengths = {path: len(blob) for path, blob, *_rest in rows}
        print(f"Scanning {len(rows)} image row(s) ({sum(1 for p in lengths if is_continuation_path(lengths, p))} "
              "chunk-continuation row(s) will be folded into their base)...")
        warn = lambda message: print(message, file=sys.stderr)  # noqa: E731
        # What the manifest will say about this database afterwards. Every row
        # of a type this tool optimizes gets an entry, whether it was rewritten,
        # left as already-minimal, skipped as animated, or carried over from an
        # incoming manifest - so a later run can tell "done" from "new". Rows
        # that ERRORED are deliberately absent: a later run should retry them.
        manifest_out = {}
        # Paths this run has already rewritten as some OTHER row's target (see
        # the re-inserted-source case below). `rows` is a snapshot taken before
        # the loop, so such a path is still sitting in it with its pre-run bytes;
        # reaching it later would either record those stale bytes in the
        # manifest or - worse - re-optimize them and write them back over the
        # replacement, silently undoing the author's update.
        superseded = set()

        for path, blob, content_type_id, value, compression, template_id, row_language_id in rows:
            if is_continuation_path(lengths, path):
                stats["skipped_fragment"] += 1  # handled as part of its base row
                continue

            if path in superseded:
                continue  # already rewritten this run as another row's target

            optimizable = value in OPTIMIZABLE
            if not optimizable and archive is None:
                # Nothing to do with it and nothing to keep a copy of: skip it
                # without paying for a reassemble.
                stats["skipped_type"] += 1
                continue

            name = path.rsplit("/", 1)[-1]
            stored_full = reassemble(conn, path, blob)  # base + its served chunks
            original_stored = len(stored_full)

            # Already optimized by an earlier run and untouched since? Leave it
            # alone. Both the size and the digest have to match: an author who
            # overwrote this file may well have produced something the same
            # length, and re-encoding their new image as though it were our own
            # output would quietly degrade it.
            recorded = known.get(path)
            already_ours = bool(recorded and recorded[1] == original_stored
                                and recorded[0] == digest(stored_full))

            # Archived before a single byte is rewritten, and decompressed at
            # most once per row - the archive and the optimizer want the same
            # bytes, and on a dictionary-compressed database each decompress is
            # a brotli subprocess.
            media_bytes = None
            archived = True
            if archive is not None:
                if already_ours:
                    # The manifest vouches for these bytes as this tool's own
                    # output from an earlier run. Archiving them as "the
                    # original" would replace the real original with a lossy
                    # copy of itself, so they are counted and left out.
                    stats["archive_not_original"] += 1
                else:
                    try:
                        media_bytes = codec.decompress(stored_full, compression)
                    except Exception as exc:  # noqa: BLE001 - one bad row is not the run
                        stats["archive_errors"] += 1
                        archived = False
                        print(f"  error: could not archive {path}: {exc}", file=sys.stderr)
                    else:
                        archived = archive.add(path, value, row_language_id, template_id, media_bytes)

            if not optimizable:
                stats["skipped_type"] += 1
                continue
            if not archived:
                # The whole point of --save-originals is that nothing is
                # destroyed without a copy of it surviving. A failed archive
                # write - a full disk, a permission problem - makes rewriting
                # this row exactly the data loss the flag was asked to prevent,
                # so the row is left alone and the run ends non-zero.
                stats["archive_blocked"] += 1
                warn(f"  skipping {path}: its original could not be archived, so it must not be rewritten")
                continue
            if already_ours:
                stats["skipped_manifest"] += 1
                manifest_out[path] = recorded
                continue

            try:
                if media_bytes is None:
                    media_bytes = codec.decompress(stored_full, compression)
                result = optimize_media(media_bytes, value, name, cfg, pngquant_path, warn)
                if result is None:  # animated WEBP/APNG: deliberately left alone
                    stats["skipped_animated"] += 1
                    manifest_out[path] = (digest(stored_full), original_stored, None)
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
                # Never grow a file, and never rename one for no benefit. It is
                # still recorded: it is in its final state, and a later run
                # should not spend the work discovering that again.
                stats["no_gain"] += 1
                stats["after"] += original_stored
                manifest_out[path] = (digest(stored_full), original_stored, recorded[2] if recorded else None)
                continue

            # Only a genuine format change renames anything: a file already
            # stored under the type it was re-encoded to keeps its own path,
            # whatever extension that path happens to use.
            replaces_stale = None
            if new_type == value:
                new_path = path
            else:
                # An author who re-inserts a source image (a .png whose .webp
                # this tool produced last run) means it as a replacement. The
                # manifest says which output came from this path, so reclaim
                # exactly that name: the pages already link to it, and
                # disambiguating instead would strand the author's new image
                # under a name nothing references while the stale one is still
                # served.
                natural = f"{path.rsplit('.', 1)[0]}{OPTIMIZABLE[new_type]}"
                if produced_from_source.get(path) == natural:
                    new_path, replaces_stale = natural, natural
                    # Marked whether or not we are writing, so a --dry-run
                    # prediction matches what a real run would do.
                    superseded.add(natural)
                else:
                    new_path = target_path(path, OPTIMIZABLE[new_type], claimed)
            converted = new_path != path
            if not cfg["dry_run"]:
                if replaces_stale:
                    row = conn.execute("SELECT LENGTH(content) FROM Content WHERE path = ?",
                                       (replaces_stale,)).fetchone()
                    if row:
                        warn(f"  note: {path} was converted to {replaces_stale} before and has been "
                             "re-inserted; replacing that output rather than adding a second copy")
                        delete_media_row(conn, replaces_stale, row[0])
                        stats["replaced_stale"] += 1
                if converted:
                    convert_media_row(conn, path, len(blob), new_path, new_stored, language_id,
                                      new_type_id, template_id, warn)
                else:
                    write_media_row(conn, path, len(blob), new_stored, language_id, new_type_id,
                                    template_id, warn)
            manifest_out[new_path] = (digest(new_stored), len(new_stored), path if converted else None)
            if converted:
                claimed.add(new_path.lower())
                claimed.discard(path.lower())
                stats["converted"] += 1
                if archive is not None:
                    # The archived original is only a rollback once it knows
                    # which row replaced it and therefore has to be deleted.
                    archive.mark_converted(path, new_path)
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

        if archive is not None:
            # Written before the commit, not after it. An index that cannot be
            # written makes the bundle useless as a rollback - the bytes under
            # originals/ are unusable without the paths and content types that
            # name them - so the conversion it would have described must not be
            # committed either. Reported as an error rather than a traceback:
            # an unwritable bundle directory is an operator mistake, not a bug.
            try:
                index_path = archive.write_index()
            except OSError as exc:
                print(f"error: could not write the archive index: {exc}. Rolling back - the conversion "
                      "it would have described must not be committed without it.", file=sys.stderr)
                conn.rollback()
                return 1

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

    if archive is not None:
        print(f"Archived {archive.saved} original asset(s) under {cfg['save_originals']}: "
              f"{archive.reused} were already archived byte for byte, {archive.revisions} filed as a later "
              f"revision, {archive.skipped} skipped, {stats['archive_errors']} unreadable.")
        if archive.replaced_missing:
            print(f"  {archive.replaced_missing} asset(s) were recorded in the index but missing from the "
                  "bundle, and have been written again.", file=sys.stderr)
        if stats["archive_blocked"]:
            print(f"  {stats['archive_blocked']} image(s) left UNOPTIMIZED because their original could not "
                  "be archived; fix the archive directory and re-run to pick them up.", file=sys.stderr)
        if stats["archive_not_original"]:
            print(f"  {stats['archive_not_original']} row(s) not archived: --manifest-in says they hold this "
                  "tool's own output from an earlier run, not originals.")
        print(f"Index written to {index_path}. Undo this run with "
              f"--restore-originals {cfg['save_originals']} (plus update_media_references.py "
              "--restore-originals over the same bundle, for the references).")

    if cfg["manifest_out"] is not None:
        if cfg["dry_run"]:
            print(f"Dry run: not writing {cfg['manifest_out']} (it would describe a database "
                  "this run did not actually change).")
        else:
            write_manifest(cfg["manifest_out"], manifest_out)
            print(f"Manifest written to {cfg['manifest_out']} ({len(manifest_out)} media file(s)).")

    pct = stats["saved"] / stats["before"] * 100 if stats["before"] else 0.0
    verb = "would optimize" if cfg["dry_run"] else "optimized"
    print()
    print(f"{'Dry run complete. ' if cfg['dry_run'] else 'Done. '}"
          f"{verb} {stats['optimized']} image(s), of which {stats['converted']} changed format "
          f"(new extension, old row deleted); {stats['no_gain']} already minimal, "
          f"{stats['skipped_type']} untouched (non-optimizable type), "
          f"{stats['skipped_animated']} animated WEBP/APNG left alone, "
          f"{stats['skipped_manifest']} already optimized per the manifest, "
          f"{stats['skipped_fragment']} chunk-fragment row(s) folded into their base, "
          f"{stats['archive_blocked']} left alone because archiving them failed, "
          f"{stats['errors']} error(s).")
    if stats["replaced_stale"]:
        print(f"  {stats['replaced_stale']} re-inserted source image(s) replaced the stale output "
              "they had produced in an earlier run.")
    for value in sorted(per_type):
        b = per_type[value]
        print(f"  {value}: {b['n']} optimized ({b['converted']} converted), saved {human(b['saved'])} bytes")
    print(f"Image bytes {'that would go' if cfg['dry_run'] else 'gone'} from "
          f"{human(stats['before'])} -> {human(stats['after'])} "
          f"(saved {human(stats['saved'])}, {pct:.1f}%).")
    # An archive that silently lost originals must not look like a clean run to
    # whatever chained this command, so every archiving failure counts here too.
    return 1 if (stats["errors"] or stats["archive_errors"] or stats["archive_blocked"]
                 or (archive is not None and archive.skipped)) else 0


def check_bundle_belongs(conn, primaries: list) -> str:
    """Returns a complaint if these archived originals plainly did not come from
    the database `conn` is open on, or "" if they did.

    The same cheap insurance update_media_references.check_same_lineage buys for
    --before, and for the same reason: several same-named databases sit side by
    side, and a mistyped or tab-completed path would otherwise have this inject
    every archived asset into an unrelated database - INSERTing rows whose
    languageID need not even exist there, since SQLite leaves foreign keys off
    by default - while reporting a successful restore.

    An archived asset should be recognisable: either its own path is still in
    the database, or the path it was converted into is. Some drift is expected
    (media really can be deleted between the two runs), so this only refuses
    when the overlap is so small that the bundle cannot plausibly describe this
    database."""
    if not primaries:
        return ""
    stored = {row[0] for row in conn.execute("SELECT path FROM Content")}
    recognised = sum(1 for entry in primaries if entry[2] in stored or (entry[7] and entry[7] in stored))
    if recognised * 2 >= len(primaries):
        return ""
    return (f"only {recognised} of its {len(primaries)} archived path(s) appear in this database, under "
            "either their original or their converted name")


def restore_originals(cfg: dict) -> int:
    """Puts every archived original back, undoing the media half of a
    --save-originals run: the original bytes return to their own path under
    their own content type, and whatever that file had been converted into is
    deleted.

    Only the primary copy of each path is restored. A revision is a LATER
    original - what an author put there after this tool had already archived
    the file - kept so it is not lost, but "the original" is the first one
    recorded. A row whose stored bytes already match the archive is left
    untouched rather than rewritten, which keeps a restore over a database that
    was only partly converted from churning every asset in it.

    This restores media only. The pages still link to the converted filenames
    until update_media_references.py --restore-originals runs over the same
    bundle."""
    db_path = cfg["db_path"]
    root = cfg["restore_originals"]
    if not db_path.is_file():
        print(f"error: {db_path} does not exist", file=sys.stderr)
        return 1
    index_path = root / BUNDLE_ORIGINALS_INDEX
    if not index_path.is_file():
        print(f"error: {root} holds no {BUNDLE_ORIGINALS_INDEX}, so it is not an archive written by "
              "--save-originals", file=sys.stderr)
        return 1
    try:
        entries = read_originals_index(index_path)
    except (OSError, RuntimeError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1

    primaries = [entry for entry in entries if entry[6].startswith(f"{BUNDLE_ORIGINALS_DIR}/")]
    revisions = len(entries) - len(primaries)
    print(f"Archive {index_path} describes {len(primaries)} original(s)"
          f"{f' and {revisions} later revision(s), which are not restored' if revisions else ''}.")

    warn = lambda message: print(message, file=sys.stderr)  # noqa: E731
    stats = {"restored": 0, "unchanged": 0, "removed": 0, "missing": 0, "corrupt": 0, "errors": 0}
    conn = sqlite3.connect(db_path)
    codec = None
    try:
        complaint = check_bundle_belongs(conn, primaries)
        if complaint and not cfg.get("force_restore"):
            print(f"error: {index_path} does not look like an archive of {db_path}: {complaint}. "
                  "Refusing to restore an unrelated bundle into this database. If this really is the "
                  "right bundle - media can legitimately have been deleted since it was made - pass "
                  "--force-restore.", file=sys.stderr)
            return 1
        if complaint:
            warn(f"warning: {complaint}, but --force-restore was given; restoring anyway.")
        try:
            codec = BrotliCodec(load_dictionary(conn))
        except RuntimeError as exc:
            print(f"error: {exc}", file=sys.stderr)
            return 1
        # value -> (id, compression) for every content type this database knows,
        # so an archived asset is stored the way its own type says to.
        type_info = {value: (type_id, compression) for type_id, value, compression in
                     conn.execute("SELECT id, value, compression FROM ContentTypes")}

        if not cfg["dry_run"]:
            print(f"Backing up {db_path} ...")
            print(f"Backup written to {backup_database(db_path)}")
        conn.execute("BEGIN")
        try:
            for sha, _size, path, content_type, language_id, template_id, location, converted in primaries:
                source = root / location
                if not source.is_file():
                    stats["missing"] += 1
                    warn(f"  error: {source} is missing; {path} cannot be restored")
                    continue
                data = source.read_bytes()
                if digest(data) != sha:
                    stats["corrupt"] += 1
                    warn(f"  error: {location} no longer matches the digest recorded for {path}; skipping")
                    continue
                info = type_info.get(content_type)
                if info is None:
                    stats["errors"] += 1
                    warn(f"  error: this database has no {content_type!r} row in ContentTypes; "
                         f"{path} cannot be restored")
                    continue
                content_type_id, compression = info

                row = conn.execute("SELECT LENGTH(content), content, languageID, templateId "
                                   "FROM Content WHERE path = ?", (path,)).fetchone()
                converted_row = None
                if converted:
                    converted_row = conn.execute("SELECT LENGTH(content) FROM Content WHERE path = ?",
                                                 (converted,)).fetchone()

                # Nothing to do when the original is already back AND the row it
                # had become is already gone. Both halves matter: content alone
                # would call a file restored while its converted twin was still
                # in the database being served, and a recorded conversion alone
                # would rewrite every converted file on every re-run.
                if row is not None and converted_row is None:
                    try:
                        current = codec.decompress(reassemble(conn, path, row[1]), compression)
                    except Exception:  # noqa: BLE001 - unreadable means "not what we archived"
                        current = None
                    # Every column the index carries has to match, not just the
                    # bytes. Comparing content alone let a row whose languageID
                    # or templateId had changed take this shortcut and keep the
                    # newer values, while an otherwise identical row whose
                    # content also differed had both put back below.
                    if current == data and (row[2], row[3]) == (language_id, template_id):
                        stats["unchanged"] += 1
                        continue

                # The row this file became has to go before the original comes
                # back: with --webp both can exist at once, and leaving the
                # converted copy behind would keep serving it to every reference
                # update_media_references.py has not yet put back.
                if converted_row is not None:
                    delete_media_row(conn, converted, converted_row[0])
                    stats["removed"] += 1

                stored = codec.compress(data, compression)
                if row is None:
                    write_chunks(conn, path, stored, language_id, content_type_id, template_id, warn,
                                 update_base=False)
                else:
                    write_media_row(conn, path, row[0], stored, language_id, content_type_id,
                                    template_id, warn)
                    # write_media_row updates content and contentTypeID only -
                    # correct for the optimizer, which is rewriting a row's own
                    # bytes in place, but a restore is putting a recorded row
                    # back and owns every column the index carries. Without
                    # this, the same archived record produced two different
                    # rows depending on whether the path still existed.
                    conn.execute("UPDATE Content SET languageID = ?, templateId = ? WHERE path = ?",
                                 (language_id, template_id, path))
                stats["restored"] += 1
                if cfg["verbose"]:
                    print(f"  [RESTORE] {path} <- {location}"
                          f"{f' (removed {converted})' if converted else ''}")
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

    if not cfg["dry_run"] and stats["restored"]:
        print("Vacuuming database to reclaim freed space...")
        vac = sqlite3.connect(db_path)
        try:
            vac.execute("VACUUM")
        finally:
            vac.close()

    verb = "would restore" if cfg["dry_run"] else "restored"
    print()
    print(f"{'Dry run complete. ' if cfg['dry_run'] else 'Done. '}{verb} {stats['restored']} original(s), "
          f"removing {stats['removed']} converted row(s); {stats['unchanged']} already matched the archive, "
          f"{stats['missing']} missing from the bundle, {stats['corrupt']} failed their digest, "
          f"{stats['errors']} error(s).")
    if stats["restored"] or stats["removed"]:
        print("Media is back; run update_media_references.py --restore-originals over the same bundle to "
              "put the references back too.")
    return 1 if stats["missing"] or stats["corrupt"] or stats["errors"] else 0


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
    p.add_argument("--manifest-out", type=Path, default=None, metavar="PATH",
                   help="After a successful run, write a text manifest of every media file this tool "
                        "manages - its stored size, a digest, and what it was converted from. Feed it "
                        "back as --manifest-in on a later run")
    p.add_argument("--manifest-in", type=Path, default=None, metavar="PATH",
                   help="A manifest from an earlier run. Media matching it byte for byte is left alone "
                        "instead of being re-encoded (which would lose quality each time), while new or "
                        "edited files are optimized normally")
    originals = p.add_mutually_exclusive_group()
    originals.add_argument("--save-originals", type=Path, default=None, metavar="BUNDLE",
                           help="Before changing anything, archive every media file's original decoded bytes "
                                "into BUNDLE, with an index recording what each row carried and what it was "
                                "converted into - everything --restore-originals needs to undo this run. "
                                "Additive: pointing several runs at one bundle keeps the first original of "
                                "each file. Cannot be combined with --dry-run")
    originals.add_argument("--restore-originals", type=Path, default=None, metavar="BUNDLE",
                           help="Undo a --save-originals run instead of optimizing: put every archived "
                                "original back and delete the row it had been converted into. The tuning "
                                "flags above are ignored. Run update_media_references.py "
                                "--restore-originals over the same bundle to put the references back too")
    p.add_argument("--force-restore", action="store_true",
                   help="With --restore-originals, proceed even though the bundle does not look like an "
                        "archive of this database. Only for when media was legitimately deleted since the "
                        "bundle was made - it is otherwise the guard against restoring into the wrong one")
    p.add_argument("--verbose", action="store_true", help="Log every optimized image, with byte sizes")
    return p


def main() -> None:
    parser = build_parser()
    args = parser.parse_args()
    # A dry run changes nothing, so a bundle written from one would claim
    # conversions that never happened - worse than no bundle, since restoring
    # from it would delete rows this run never created.
    if args.save_originals is not None and args.dry_run:
        parser.error("--save-originals cannot be combined with --dry-run: a dry run makes no changes to undo")
    cfg = {
        "db_path": args.db_path, "dry_run": args.dry_run, "max_width": args.max_width,
        "jpeg_quality": args.jpeg_quality, "webp_quality": args.webp_quality,
        "pngquant_speed": args.pngquant_speed, "svg_precision": args.svg_precision, "verbose": args.verbose,
        "webp": args.webp, "svg_rasterize_threshold": args.svg_rasterize_threshold,
        "manifest_out": args.manifest_out, "manifest_in": args.manifest_in,
        "save_originals": args.save_originals, "restore_originals": args.restore_originals,
        "force_restore": args.force_restore,
    }
    sys.exit(restore_originals(cfg) if args.restore_originals is not None else run(cfg))


if __name__ == "__main__":
    main()
