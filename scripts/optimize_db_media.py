#!/usr/bin/env python3
# /// script
# requires-python = ">=3.9"
# dependencies = [
#     "brotli",
#     "Pillow",
#     "scour",
# ]
# ///
"""
optimize_db_media.py

Applies the image optimization from PR #24 (the Kotlin website's
optimize_media.py) to *every* media file already stored in a
documentation.db, across all doc sets (a/, i/, k/, p/, j/, ...), optimizing
each image in place - same Content.path, same extension, same contentTypeID.

Why in place (no format changes), unlike insert_optimized_media.py:
  insert_optimized_media.py optimizes a *directory* of raw Kotlin media and
  reinserts it, and with --webp it renames files (png -> webp, oversized svg
  -> png), then rewrites the "/k/html/images/<name>" references baked into
  Kotlin's stored JSON pages. That rename+rewrite step only understands
  Kotlin's page format. The other doc sets don't share it: an Android page,
  for instance, references its media by absolute URL
  ("https://developer.android.com/images/foo.gif"), with no literal link to
  the stored path ("a/devsite/media/..._foo.gif") that a text substitution
  could follow. So renaming media anywhere outside Kotlin can't be done
  safely by this kind of tooling. Optimizing in place sidesteps the whole
  problem: the path and extension never change, so every reference - however
  it's written - keeps resolving to the same, now-smaller, file. That's why
  this tool never converts to WEBP or rasterizes an SVG.

What it does, per media Content row (inside one transaction, rolled back on
any error), mirroring optimize_media.py's own encoders and defaults:
  - image/png : normalize mode, pngquant at full res, downscale to
    --max-width (never upscaled), pngquant again. Stored raw (no compression).
  - image/jpeg: downscale, re-encode JPEG (--jpeg-quality, progressive). Raw.
  - image/gif : downscale; animated GIFs get every frame resized (frame
    count/durations/loop preserved). Raw.
  - image/webp: downscale, re-encode WEBP (--webp-quality). Kept as WEBP
    (never a rename). Animated WEBP is left untouched. Brotli-compressed on
    store, matching the ContentTypes row.
  - image/svg+xml: Scour-optimized (metadata/comment/id stripping, numbers
    rounded to --svg-precision) - but never rasterized to PNG, since that
    would rename it. Brotli-compressed on store.
  - Every other content type (video/mp4, image/x-icon, fonts, html, ...) is
    left untouched.

Each row's bytes are decompressed per its ContentTypes.compression ("brotli"
-> plain brotli, as this database stores it; "none" -> raw), optimized, then
recompressed the same way. A row is only rewritten when the result is
actually smaller than what's already stored - an image is never made larger.

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

def optimize_png(data: bytes, pngquant_path: str, speed: int, max_width: int, name: str) -> bytes:
    with Image.open(io.BytesIO(data)) as img:
        if getattr(img, "is_animated", False):
            # APNG: per-frame handling isn't implemented; leave it untouched
            # rather than silently flattening the animation to one frame.
            return data
        img = normalize_mode(img)
        # pngquant at full resolution first (its palette selection sees the
        # original color detail), then resize, then pngquant again at the
        # delivered size - the same two-pass shape optimize_media.py uses.
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


def optimize_jpeg(data: bytes, quality: int, max_width: int) -> bytes:
    with Image.open(io.BytesIO(data)) as img:
        img = normalize_mode(img)
        img = resize_if_needed(img, max_width)
        if img.mode != "RGB":
            img = img.convert("RGB")
        buf = io.BytesIO()
        img.save(buf, "JPEG", quality=quality, optimize=True, progressive=True)
        return buf.getvalue()


def optimize_gif(data: bytes, max_width: int) -> bytes:
    with Image.open(io.BytesIO(data)) as img:
        if getattr(img, "is_animated", False):
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
        img = resize_if_needed(img, max_width)
        buf = io.BytesIO()
        img.save(buf, "GIF", optimize=True)
        return buf.getvalue()


def optimize_webp(data: bytes, quality: int, max_width: int) -> bytes:
    with Image.open(io.BytesIO(data)) as img:
        if getattr(img, "is_animated", False):
            # Animated WEBP re-encoding isn't implemented (optimize_media.py
            # doesn't attempt it either); leave it untouched.
            return data
        img = normalize_mode(img)
        img = resize_if_needed(img, max_width)
        buf = io.BytesIO()
        img.save(buf, "WEBP", quality=quality, method=6)
        return buf.getvalue()


def optimize_svg(data: bytes, precision: int) -> bytes:
    """Scour-optimize, with optimize_media.py's own aggressive settings, but
    never rasterize (that would rename the file)."""
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


# Content-type value -> the optimizer to call. Anything not here is left
# untouched (video/mp4, image/x-icon, fonts, text, ...).
def build_optimizers(cfg: dict, pngquant_path: str) -> dict:
    return {
        "image/png": lambda d, name: optimize_png(d, pngquant_path, cfg["pngquant_speed"], cfg["max_width"], name),
        "image/jpeg": lambda d, name: optimize_jpeg(d, cfg["jpeg_quality"], cfg["max_width"]),
        "image/gif": lambda d, name: optimize_gif(d, cfg["max_width"]),
        "image/webp": lambda d, name: optimize_webp(d, cfg["webp_quality"], cfg["max_width"]),
        "image/svg+xml": lambda d, name: optimize_svg(d, cfg["svg_precision"]),
    }


# --- DB plumbing -------------------------------------------------------------

def backup_database(db_path: Path) -> Path:
    """VACUUM INTO a timestamped sibling, same approach as populate_db.py."""
    backup_path = db_path.with_name(f"{db_path.name}.backup-{time.strftime('%Y%m%d-%H%M%S')}")
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


def write_media_row(conn, base_path: str, base_length: int, new_stored: bytes,
                    language_id: int, content_type_id: int) -> None:
    """Replaces a media item's stored bytes, deleting whatever chunk chain it
    used to have and re-chunking the new bytes if they still exceed
    CHUNK_SIZE (they almost never do after downscaling, but a correct write
    can't assume that). Fragments are written as a clean chain from -1,
    matching what WebServer.kt probes for."""
    for fragment_path in owned_fragment_paths(conn, base_path, base_length):
        conn.execute("DELETE FROM Content WHERE path = ?", (fragment_path,))

    if len(new_stored) <= CHUNK_SIZE:
        conn.execute("UPDATE Content SET content = ? WHERE path = ?", (new_stored, base_path))
        return

    # Rare: still over a chunk after optimizing. Split into CHUNK_SIZE pieces;
    # the base holds the first, continuations are "<base>-1", "-2", ...
    chunks = [new_stored[i:i + CHUNK_SIZE] for i in range(0, len(new_stored), CHUNK_SIZE)]
    conn.execute("UPDATE Content SET content = ? WHERE path = ?", (chunks[0], base_path))
    for number, chunk in enumerate(chunks[1:], start=1):
        conn.execute(
            "INSERT INTO Content (path, languageID, content, contentTypeID, templateId) VALUES (?, ?, ?, ?, 0)",
            (f"{base_path}-{number}", language_id, chunk, content_type_id),
        )


def run(cfg: dict) -> int:
    db_path = cfg["db_path"]
    if not db_path.is_file():
        print(f"error: {db_path} does not exist", file=sys.stderr)
        return 1

    try:
        pngquant_path = find_pngquant()
    except RuntimeError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1

    optimizers = build_optimizers(cfg, pngquant_path)

    if cfg["dry_run"]:
        print(f"Dry run: no backup, no changes committed. Optimizing media in {db_path} ...")
    else:
        print(f"Backing up {db_path} ...")
        backup_path = backup_database(db_path)
        print(f"Backup written to {backup_path}")

    conn = sqlite3.connect(db_path)
    stats = {"optimized": 0, "no_gain": 0, "skipped_type": 0, "skipped_fragment": 0, "errors": 0,
             "before": 0, "after": 0, "saved": 0}
    per_type = {}
    codec = None
    try:
        conn.execute("BEGIN")
        # languageID/contentTypeID are needed only if a re-chunk write inserts
        # new continuation rows; read the language once (this DB has one).
        language_id = conn.execute("SELECT id FROM Languages LIMIT 1").fetchone()[0]
        codec = BrotliCodec(load_dictionary(conn))
        if codec._dictionary is not None:
            print(f"Using the database's {len(codec._dictionary):,}-byte shared Brotli dictionary "
                  "for image/svg+xml and image/webp rows.")

        rows = conn.execute(
            "SELECT c.path, c.content, c.contentTypeID, ct.value, ct.compression "
            "FROM Content c JOIN ContentTypes ct ON c.contentTypeID = ct.id "
            "WHERE ct.value LIKE 'image/%' ORDER BY c.path"
        ).fetchall()
        # {path: stored length} over every image row, so a "<base>-<N>" row can
        # be recognized as a continuation only when its base is exactly
        # CHUNK_SIZE bytes (see content_chunking) rather than by its name alone.
        lengths = {path: len(blob) for path, blob, _c, _v, _cmp in rows}
        print(f"Scanning {len(rows)} image row(s) ({sum(1 for p in lengths if is_continuation_path(lengths, p))} "
              "chunk-continuation row(s) will be folded into their base)...")

        for path, blob, content_type_id, value, compression in rows:
            if is_continuation_path(lengths, path):
                stats["skipped_fragment"] += 1  # handled as part of its base row
                continue

            optimizer = optimizers.get(value)
            if optimizer is None:
                stats["skipped_type"] += 1
                continue

            name = path.rsplit("/", 1)[-1]
            stored_full = reassemble(conn, path, blob)  # base + its served chunks
            original_stored = len(stored_full)
            try:
                media_bytes = codec.decompress(stored_full, compression)
                new_media = optimizer(media_bytes, name)
                new_stored = codec.compress(new_media, compression)
            except Exception as exc:  # noqa: BLE001 - one bad image is not the whole run
                stats["errors"] += 1
                print(f"  error: failed to optimize {path}: {exc}", file=sys.stderr)
                continue

            stats["before"] += original_stored
            if len(new_stored) < original_stored:
                if not cfg["dry_run"]:
                    write_media_row(conn, path, len(blob), new_stored, language_id, content_type_id)
                stats["optimized"] += 1
                stats["after"] += len(new_stored)
                saved = original_stored - len(new_stored)
                stats["saved"] += saved
                bucket = per_type.setdefault(value, {"n": 0, "saved": 0})
                bucket["n"] += 1
                bucket["saved"] += saved
                if cfg["verbose"]:
                    pct = saved / original_stored * 100 if original_stored else 0.0
                    print(f"  [OPT] {path}: {human(original_stored)} -> {human(len(new_stored))} "
                          f"(saved {human(saved)}, {pct:.1f}%)")
            else:
                stats["no_gain"] += 1
                stats["after"] += original_stored

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
          f"{verb} {stats['optimized']} image(s); {stats['no_gain']} already minimal, "
          f"{stats['skipped_type']} untouched (non-optimizable type), "
          f"{stats['skipped_fragment']} chunk-fragment row(s) folded into their base, "
          f"{stats['errors']} error(s).")
    for value in sorted(per_type):
        b = per_type[value]
        print(f"  {value}: {b['n']} optimized, saved {human(b['saved'])} bytes")
    print(f"Image bytes {'that would go' if cfg['dry_run'] else 'gone'} from "
          f"{human(stats['before'])} -> {human(stats['after'])} "
          f"(saved {human(stats['saved'])}, {pct:.1f}%).")
    return 1 if stats["errors"] else 0


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("db_path", type=Path, help="SQLite database to optimize in place, e.g. documentation.db")
    p.add_argument("--dry-run", action="store_true",
                   help="Do all the work and report savings, then roll back without writing or backing up")
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
    }
    sys.exit(run(cfg))


if __name__ == "__main__":
    main()
