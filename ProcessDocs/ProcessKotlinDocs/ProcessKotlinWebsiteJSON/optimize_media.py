#!/usr/bin/env python3
"""
optimize_media.py

Recursively mirrors an input directory into an output directory, optimizing
image files along the way and copying everything else unchanged.

  - Raster images (png, jpg/jpeg, gif, bmp, tif/tiff, webp) are downscaled to
    a maximum width (default 500px, preserving aspect ratio, never upscaled),
    then optimized: PNGs through pngquant (at a configurable --pngquant-speed
    trade-off - see https://pngquant.org/), other formats through Pillow's
    own encoder (quality/optimize flags). With --webp, the resized image is
    saved as WEBP instead of its original format. Animated GIFs get every
    frame resized the same way (preserving frame count/duration/loop count)
    rather than being copied through unchanged; --webp is not applied to
    them, since animated WEBP re-encoding isn't implemented here. Any other
    animated format (e.g. an animated WEBP given as input) is still copied
    through unchanged, since per-frame resizing isn't implemented for it.
  - SVGs (.svg) are aggressively optimized with the Scour library: metadata,
    comments and editor cruft stripped, ids shortened, whitespace collapsed,
    and every number rounded to --svg-precision decimal places. If the
    optimized SVG still exceeds --svg-rasterize-threshold bytes, it's
    rasterized (via cairosvg) and run through the same raster pipeline above
    instead of being kept as a vector. If rasterizing fails for any reason
    (e.g. cairosvg isn't installed), that's logged as a warning and the
    optimized (still oversized) SVG is written instead - every input file
    always ends up with something at its mirrored output path.
  - Every other file is copied through unchanged.

Usage:
    python3 optimize_media.py <input_dir> <output_dir> [options]
    python3 optimize_media.py --config myjob.config

All options may instead be set in a .config file (one `key = value` per
line, '#' for comments) passed via --config; see OPTION_SPECS below for the
recognized keys, which are the same as the long-form CLI flags. Values
explicitly given on the command line always take precedence over the config
file.

Every media file processed logs a line with its original and optimized
locations, regardless of --verbose (which adds byte sizes to that line and
prints all resolved config parameters up front). --log-file redirects all
log output (those per-file lines, warnings, and errors) to that file
instead of stdout/stderr.

Requires the "pngquant" binary on PATH, plus the Pillow, scour and cairosvg
Python packages listed in requirements.txt - run via
`uv run --with-requirements <repo-root>/requirements.txt optimize_media.py ...`.
"""
import argparse
import io
import shutil
import subprocess
import sys
from pathlib import Path
from typing import NamedTuple, Optional

from PIL import Image

try:
    RESAMPLE = Image.Resampling.LANCZOS
except AttributeError:  # Pillow < 9.1
    RESAMPLE = Image.LANCZOS

RASTER_EXTENSIONS = {".png", ".jpg", ".jpeg", ".gif", ".bmp", ".tif", ".tiff", ".webp"}
SVG_EXTENSION = ".svg"


class Logger:
    """Routes both info-level and error-level messages to a single
    destination: the --log-file path if one was given (so a redirected run
    still has one coherent, chronological log to inspect afterwards), or
    stdout/stderr otherwise (so a normal terminal run keeps its usual
    split - errors are still visible even if stdout is piped elsewhere)."""

    def __init__(self, file_handle):
        self._fh = file_handle

    def info(self, msg: str) -> None:
        print(msg, file=self._fh if self._fh is not None else sys.stdout, flush=self._fh is not None)

    def error(self, msg: str) -> None:
        print(msg, file=self._fh if self._fh is not None else sys.stderr, flush=self._fh is not None)


def parse_bool(value: str) -> bool:
    v = value.strip().lower()
    if v in ("1", "true", "yes", "on", "y", "t"):
        return True
    if v in ("0", "false", "no", "off", "n", "f"):
        return False
    raise ValueError(f"not a boolean: {value!r}")


# Maps a config-file key (and, with dashes, a --long-form CLI flag) to the
# argparse dest it corresponds to and the converter used to parse its value
# out of the config file's plain-text "key = value" form. Order here is also
# the order config parameters get listed in when --verbose is on.
OPTION_SPECS = {
    "input-dir": ("input_dir", Path),
    "output-dir": ("output_dir", Path),
    "max-width": ("max_width", int),
    "jpeg-quality": ("jpeg_quality", int),
    "webp": ("webp", parse_bool),
    "webp-quality": ("webp_quality", int),
    "pngquant-speed": ("pngquant_speed", int),
    "svg-precision": ("svg_precision", int),
    "svg-rasterize-threshold": ("svg_rasterize_threshold", int),
    "verbose": ("verbose", parse_bool),
    "log-file": ("log_file", Path),
}

# Used for any option left unset by both the CLI and (if given) --config.
BUILTIN_DEFAULTS = {
    "max_width": 500,
    "jpeg_quality": 82,
    "webp": False,
    "webp_quality": 80,
    "pngquant_speed": 4,  # pngquant's own default; 1 = slow/best, 11 = fast/rough
    "svg_precision": 4,
    "svg_rasterize_threshold": 300 * 1024,  # 300KB
    "verbose": False,
}


def load_config_file(path: Path, option_specs: dict = OPTION_SPECS) -> dict:
    """Parses a simple `key = value` (or `key: value`) config file, one
    option per line; blank lines and lines starting with '#' or ';' are
    ignored. A bare key with no value means true (for boolean options).
    Keys match `option_specs` (case-insensitive, dashes or underscores) -
    defaulting to this module's own OPTION_SPECS, but overridable so a
    caller layering its own options on top (e.g. insert_optimized_media.py
    adding "db-path") can reuse this same parser for its extended key set.
    Returns {dest: converted_value}."""
    if not path.is_file():
        raise RuntimeError(f"config file not found: {path}")

    overrides = {}
    for lineno, raw_line in enumerate(path.read_text(encoding="utf-8").splitlines(), start=1):
        line = raw_line.strip()
        if not line or line.startswith("#") or line.startswith(";"):
            continue
        if "=" in line:
            key, _, value = line.partition("=")
        elif ":" in line:
            key, _, value = line.partition(":")
        else:
            key, value = line, "true"
        key = key.strip().lower().replace("_", "-")
        value = value.strip()

        spec = option_specs.get(key)
        if spec is None:
            raise RuntimeError(f"{path}:{lineno}: unknown config option {key!r}")
        dest, converter = spec
        try:
            overrides[dest] = converter(value)
        except ValueError as exc:
            raise RuntimeError(f"{path}:{lineno}: invalid value for {key!r}: {exc}") from exc
    return overrides


def find_pngquant() -> str:
    path = shutil.which("pngquant")
    if path is None:
        raise RuntimeError("pngquant not found on PATH; install it (e.g. `apt install pngquant`) and retry")
    return path


def quantize_png_bytes(data: bytes, pngquant_path: str, speed: int, name: str, logger: Logger) -> bytes:
    """Runs pngquant on raw PNG bytes (stdin -> stdout, no temp files) at the
    given --speed trade-off (1 = slow/best quality, 11 = fast/rough - see
    https://pngquant.org/), returning the compressed bytes. Falls back to
    the original bytes if pngquant declines (e.g. exit 99: result would fall
    below --quality's floor) or otherwise fails - a slightly larger PNG
    beats a missing one."""
    result = subprocess.run(
        [pngquant_path, "--quality", "65-95", "--speed", str(speed), "--strip", "--force", "--output", "-", "-"],
        input=data, stdout=subprocess.PIPE, stderr=subprocess.PIPE, check=False,
    )
    if result.returncode != 0 or not result.stdout:
        logger.error(
            f"warning: pngquant declined to compress {name!r} "
            f"(exit {result.returncode}: {result.stderr.decode(errors='replace').strip()}); keeping original"
        )
        return data
    return result.stdout


def resize_if_needed(img: Image.Image, max_width: int) -> Image.Image:
    if img.width <= max_width:
        return img
    new_height = max(1, round(img.height * (max_width / img.width)))
    return img.resize((max_width, new_height), RESAMPLE)


def normalize_mode(img: Image.Image) -> Image.Image:
    """Flattens palette/CMYK modes to something every downstream encoder
    (JPEG, WEBP, PNG) can handle directly, preserving alpha where present."""
    if img.mode == "P":
        return img.convert("RGBA") if img.info.get("transparency") is not None else img.convert("RGB")
    if img.mode == "CMYK":
        return img.convert("RGB")
    return img


def encode_raster(img: Image.Image, dst: Path, *, suffix: str, max_width: int, jpeg_quality: int, webp: bool,
                   webp_quality: int, pngquant_path: str, pngquant_speed: int, logger: Logger) -> Path:
    """Resizes an already-loaded image and writes it under dst (whose suffix
    may be swapped to .webp), choosing the encoder by `suffix` (the source
    file's extension, or ".png" for a freshly rasterized SVG). Returns the
    path actually written. Shared by optimize_raster and optimize_svg's
    rasterize-on-oversize fallback so both go through identical resize/
    encode logic."""
    img = normalize_mode(img)

    if suffix == ".png" and not webp:
        # pngquant runs against the full-resolution pixels here, before any
        # downscaling - its palette selection and dithering have the whole
        # original image's color detail to work from, rather than the
        # coarser, already-blended pixels a resize would leave it with. The
        # quantized result is then decoded back and resized down below, same
        # as any other image.
        buf = io.BytesIO()
        img.save(buf, "PNG", optimize=True)
        quantized = quantize_png_bytes(buf.getvalue(), pngquant_path, pngquant_speed, str(dst), logger)
        img = Image.open(io.BytesIO(quantized))
        img.load()
        img = normalize_mode(img)  # pngquant's output PNG is palette ("P") mode

    img = resize_if_needed(img, max_width)

    if webp:
        dst = dst.with_suffix(".webp")
        img.save(dst, "WEBP", quality=webp_quality, method=6)
        return dst

    if suffix == ".png":
        # Resizing the first quantize pass's palette image back down blended
        # it back into full RGB(A) - re-quantize at the final size to
        # restore a compact palette PNG, now informed by both the full-
        # resolution pass above and the actual delivered dimensions.
        buf = io.BytesIO()
        img.save(buf, "PNG", optimize=True)
        dst.write_bytes(quantize_png_bytes(buf.getvalue(), pngquant_path, pngquant_speed, str(dst), logger))
    elif suffix in (".jpg", ".jpeg"):
        if img.mode != "RGB":
            img = img.convert("RGB")
        img.save(dst, "JPEG", quality=jpeg_quality, optimize=True, progressive=True)
    else:
        img.save(dst, optimize=True)
    return dst


def resize_animated_gif(img: Image.Image, dst: Path, max_width: int) -> Path:
    """Resizes every frame of an animated GIF down to max_width, preserving
    frame count, each frame's own duration, and the loop count - a naive
    single-frame resize (or the old copy-through-unchanged behavior) would
    otherwise silently drop the animation entirely. seek()+convert("RGBA")
    composites each frame the way Pillow's GIF plugin normally displays it
    (accounting for the previous frame's disposal method), so every frame
    saved below is a complete, standalone image rather than a partial
    update relying on its predecessor - hence disposal=2 (restore to
    background) on save, rather than trying to preserve each original
    frame's own disposal method. --webp is intentionally not honored here:
    animated WEBP re-encoding is a separate feature this doesn't attempt."""
    n_frames = getattr(img, "n_frames", 1)
    frames = []
    durations = []
    for i in range(n_frames):
        img.seek(i)
        frames.append(resize_if_needed(img.convert("RGBA"), max_width))
        durations.append(img.info.get("duration", 100))
    # loop is passed through only when the source carried one. Pillow reports a
    # GIF with no NETSCAPE application-extension block - i.e. one that plays
    # once - by *omitting* the "loop" key, and 0 is the value that means "loop
    # forever". So info.get("loop", 0) reads "play once" and writes "loop
    # forever", adding a NETSCAPE block the source never had, which is the
    # opposite of the loop count this function's docstring says it preserves.
    save_kwargs = {"duration": durations, "disposal": 2, "optimize": True}
    if "loop" in img.info:
        save_kwargs["loop"] = img.info["loop"]
    frames[0].save(dst, save_all=True, append_images=frames[1:], **save_kwargs)
    return dst


def optimize_raster(src: Path, dst: Path, *, animated: Optional[bool], **encode_kwargs) -> Path:
    """`animated` is decided once, by _is_animated_raster during the
    name-planning pass, and passed in rather than re-derived here.

    This used to repeat the planner's `getattr(img, "is_animated", False)`
    a few lines after the planner ran it, which is two answers to one
    question - and the possibility of them differing is the whole reason
    optimize_directory had to carry a set of sources it had "declined to
    predict" and special-case them when checking what got written. Both
    answers came from the same access on the same file, so in practice they
    could only differ on a transient (the file changed or was locked between
    the two opens); taking the planner's answer as authoritative removes the
    case rather than handling it.

    None means the planner could not tell. It refuses rather than guessing:
    the planner has already recorded that this source is expected to produce
    nothing, and process_file turns this into one file's counted error."""
    if animated is None:
        raise ValueError("could not determine whether this file is animated (see the earlier warning); "
                         "skipping it rather than guessing at its output format")
    with Image.open(src) as img:
        if animated:
            if src.suffix.lower() == ".gif":
                return resize_animated_gif(img, dst, encode_kwargs["max_width"])
            # Animated non-GIF: per-frame resizing/re-encoding is out of
            # scope here - copy through unchanged rather than flattening it to
            # a single frame and silently breaking the animation. Reaches
            # animated WEBP, APNG and multi-frame TIFF.
            #
            # It also reaches an MPO stored as ".jpg" - a phone stereo or
            # burst capture, which Pillow opens as a multi-frame image and
            # reports as animated. Copying it through unoptimized is the
            # deliberate choice: flattening it to its primary frame would be
            # defensible for a documentation image, but this branch is not the
            # place to decide that silently, and the source is a rarity worth
            # noticing in the log rather than quietly rewriting.
            shutil.copy2(src, dst)
            return dst
        return encode_raster(img, dst, suffix=src.suffix.lower(), **encode_kwargs)


def rasterize_svg(svg_text: str, max_width: int) -> Image.Image:
    """Renders SVG markup to a raster image at exactly `max_width` pixels
    wide (cairosvg computes the proportional height from the SVG's own
    viewBox/aspect ratio), for SVGs too large to keep as vector output."""
    try:
        import cairosvg
    except ImportError as exc:
        raise RuntimeError(
            "cairosvg is required to rasterize oversized SVGs; it's in requirements.txt - "
            "run this script via `uv run --with-requirements <repo-root>/requirements.txt`"
        ) from exc
    png_bytes = cairosvg.svg2png(bytestring=svg_text.encode("utf-8"), output_width=max_width)
    img = Image.open(io.BytesIO(png_bytes))
    img.load()
    return img


def optimize_svg(src: Path, dst: Path, *, precision: int, rasterize_threshold: int, max_width: int,
                  jpeg_quality: int, webp: bool, webp_quality: int, pngquant_path: str, pngquant_speed: int,
                  logger: Logger) -> tuple:
    """Optimizes one SVG with Scour, rounding numbers to `precision` decimal
    places. If the optimized markup is still over `rasterize_threshold`
    bytes, rasterizes it and runs it through the raster pipeline instead of
    writing it as a (still large) vector. Returns (final_path, was_rasterized)."""
    from scour import scour

    options = scour.generateDefaultOptions()
    # Aggressive settings, roughly equivalent to:
    #   scour --enable-viewboxing --enable-id-stripping --enable-comment-stripping
    #         --shorten-ids --indent=none --strip-xml-prolog --set-precision=<precision>
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

    in_string = src.read_text(encoding="utf-8")
    out_string = scour.scourString(in_string, options)
    out_bytes = out_string.encode("utf-8")

    if len(out_bytes) > rasterize_threshold:
        try:
            img = rasterize_svg(out_string, max_width)
            final = encode_raster(
                img, dst.with_suffix(".png"), suffix=".png", max_width=max_width, jpeg_quality=jpeg_quality,
                webp=webp, webp_quality=webp_quality, pngquant_path=pngquant_path, pngquant_speed=pngquant_speed,
                logger=logger,
            )
            logger.info(
                f"note: rasterized {src} -> {final} (optimized SVG was {len(out_bytes):,} bytes, "
                f"over the {rasterize_threshold:,} byte threshold)"
            )
            return final, True
        except Exception as exc:  # noqa: BLE001 - fall through to writing the vector below instead
            logger.error(f"warning: failed to rasterize {src} ({exc}); keeping optimized SVG instead")

    dst.write_bytes(out_bytes)
    return dst, False


def process_file(src: Path, dst: Path, *, cfg: dict, pngquant_path: str, stats: dict, logger: Logger,
                  animated: Optional[bool]) -> Path:
    """Optimizes (or copies through) one file. Returns the path actually
    written on success (which may differ from `dst` - webp conversion or
    SVG rasterization changes the extension), or None on error (already
    logged; `stats["errors"]` is incremented so callers can tell without
    inspecting the return value).

    `animated` is required, not defaulted: it is the name-planning pass's
    answer for this source (see optimize_raster), and a default would let a
    caller silently skip the planning this function's output is checked
    against.

    It governs the raster branch alone, and it governs it for every raster -
    an animated one is copied through (or resized as a GIF) under its own
    extension, a static one is re-encoded. The caller passes False for SVG and
    passthrough files, which have no animation to honour; it must not pass a
    hard-coded False for a raster it simply chose not to probe, which is how
    every animated WEBP came to be flattened."""
    dst.parent.mkdir(parents=True, exist_ok=True)
    suffix = src.suffix.lower()

    try:
        # Inside the try: a source that vanishes or becomes unreadable between
        # collection and now is one file's error, not the whole run's.
        original_size = src.stat().st_size
        if suffix == SVG_EXTENSION:
            dst_final, rasterized = optimize_svg(
                src, dst, precision=cfg["svg_precision"], rasterize_threshold=cfg["svg_rasterize_threshold"],
                max_width=cfg["max_width"], jpeg_quality=cfg["jpeg_quality"], webp=cfg["webp"],
                webp_quality=cfg["webp_quality"], pngquant_path=pngquant_path, pngquant_speed=cfg["pngquant_speed"],
                logger=logger,
            )
            if rasterized:
                stats["svg_rasterized"] += 1
                kind = "svg, rasterized"
            else:
                stats["svg"] += 1
                kind = "svg"
        elif suffix in RASTER_EXTENSIONS:
            dst_final = optimize_raster(
                src, dst, animated=animated,
                max_width=cfg["max_width"], jpeg_quality=cfg["jpeg_quality"], webp=cfg["webp"],
                webp_quality=cfg["webp_quality"], pngquant_path=pngquant_path, pngquant_speed=cfg["pngquant_speed"],
                logger=logger,
            )
            stats["raster"] += 1
            kind = "raster"
        else:
            shutil.copy2(src, dst)
            dst_final = dst
            stats["copied"] += 1
            kind = "copied"
    except Exception as exc:  # noqa: BLE001 - keep processing the rest of the tree
        stats["errors"] += 1
        logger.error(f"error: failed to process {src}: {exc}")
        return None

    optimized_size = dst_final.stat().st_size
    stats["original_bytes"] += original_size
    stats["optimized_bytes"] += optimized_size
    if kind != "copied":
        # Always shown (not just under --verbose) - a per-file record of
        # where the optimized copy of each media file actually landed,
        # since that's not otherwise derivable once optimization has
        # renamed a file (webp conversion, SVG rasterization).
        message = f"Optimized {src} -> {dst_final}"
        if cfg["verbose"]:
            saved = original_size - optimized_size
            pct = (saved / original_size * 100) if original_size else 0.0
            message = (
                f"[OK][{kind}] {src} -> {dst_final}: {original_size:,} -> {optimized_size:,} bytes "
                f"(saved {saved:,} bytes, {pct:.1f}%)"
            )
        logger.info(message)
    return dst_final


class Plan(NamedTuple):
    """What the name-planning pass decided for one source: where it will be
    written, every basename it holds against the other sources, and the
    animation answer process_file is to use. One record rather than a dict
    per field - the three are decided together, in one loop, and every
    parallel structure this pass has grown so far ended up disagreeing with
    the one beside it."""

    dst_rel: Path
    names: set
    animated: Optional[bool]


class OptimizeResult(NamedTuple):
    """What optimize_directory did.

    `written` is every output path this run actually produced, which is not
    the same as "everything in output_dir": a caller reusing a work directory
    between runs (a documented usage) would otherwise pick up files left by an
    earlier run whose sources have since been deleted, and insert them as if
    this run had produced them. `renamed` maps {relative_src: relative_dst}
    for the subset whose output path differed from its input path, which is
    what callers maintaining references elsewhere need in order to follow
    them."""

    renamed: dict
    written: list


def possible_output_names(stem: str, suffix: str, cfg: dict, animated: Optional[bool] = None) -> set:
    """Every basename process_file could write for a source named
    `stem + suffix`.

    A set rather than one name because one branch is genuinely not predictable
    up front: optimize_svg only rasterizes when the *optimized* SVG comes out
    over --svg-rasterize-threshold, which isn't known until the work is done.
    Treating both possibilities as claimed is the conservative choice - it can
    de-conflict a pair that would not in fact have collided, which costs a
    rename, where the reverse costs an image.

    `animated` lets the one *knowable* exception be resolved exactly instead
    of conservatively: an animated raster is written back under its own
    extension even under --webp (resize_animated_gif does not honour it, and
    optimize_raster copies an animated non-GIF through untouched), so
    predicting ".webp" for every raster mispredicts exactly those. It
    mispredicts them in the damaging direction, too: an animated logo.gif
    claims logo.webp it never writes, a static logo.gif elsewhere is
    de-conflicted against that phantom, and rename_map then repoints every
    stored /k/html/images/logo.gif at the static one - so the animated row
    goes unreferenced and delete_unreferenced_media removes it. Same shape as
    the collision this pre-pass exists to prevent, reached through the single
    output name the suffix alone cannot determine.

    Pass True/False from _is_animated_raster. None means "could not be
    determined", and a multi-frame-capable extension then claims *nothing*
    rather than claiming both.

    That is the opposite of the SVG branch's conservatism, and deliberately
    so: the two ambiguities are different. An SVG genuinely might write
    either name, so both have to be held. A raster whose animation could not
    be determined is one optimize_raster is about to fail on for the same
    reason - it repeats this exact `Image.open` plus `is_animated` access, so
    whatever raised here raises there, process_file counts an error and
    returns None, and the source writes no file at all. Holding two names for
    it would de-conflict an unrelated source against an output that is never
    produced, renaming a real image and rewriting every stored URL that
    pointed at it. Claiming nothing is the accurate prediction; if it is ever
    wrong, optimize_directory's written-vs-claimed check says so out loud.

    Taking the answer rather than the path keeps this function free of I/O:
    it is called once per de-confliction attempt, and re-opening the source
    on each attempt bought nothing - the answer is a property of the file,
    not of the candidate name being tried."""
    if animated is not None and not isinstance(animated, bool):
        # This parameter took a Path one revision ago. A Path is truthy and is
        # not None, so a call site left on the old signature would silently
        # report "animated" for every file - no conversion, no error, and
        # nothing in the output to suggest why.
        raise TypeError(f"animated must be True, False or None, not {type(animated).__name__}")
    if suffix.lower() == SVG_EXTENSION:
        # `suffix`, not the SVG_EXTENSION literal: optimize_svg's
        # non-rasterized path writes `dst`, which carries the source's own
        # spelling, so a "Diagram.SVG" claimed as "Diagram.svg" is a name this
        # never actually produces. Harmless while it only fed the casefolded
        # `claimed` map, fatal once optimize_directory started checking what
        # was written against what was claimed. Every other branch below
        # already echoes `suffix`; this one was the exception.
        rasterized = ".webp" if cfg["webp"] else ".png"
        return {f"{stem}{suffix}", f"{stem}{rasterized}"}
    if suffix.lower() in RASTER_EXTENSIONS:
        if not cfg["webp"]:
            return {f"{stem}{suffix}"}
        if animated is None:  # undetermined: this source writes nothing
            return set()
        # Uniform, and no longer keyed on a list of extensions: under --webp a
        # raster keeps its own extension exactly when it is animated, because
        # that is when optimize_raster copies it through (or, for a GIF,
        # resizes it as a GIF) instead of re-encoding it. That covers the
        # cases a curated set kept getting wrong - an APNG, a multi-frame
        # TIFF, and an MPO stored as .jpg, which Pillow reports as animated.
        return {f"{stem}{suffix}"} if animated else {f"{stem}.webp"}
    return {f"{stem}{suffix}"}  # passthrough copy, extension never changes


def _is_animated_raster(src: Path, logger: Optional[Logger] = None) -> Optional[bool]:
    """True/False for a readable raster, or None when it can't be determined.
    The same `Image.open` + `is_animated` test optimize_raster makes later,
    just made early enough for the name-planning pass to predict the output
    extension.

    A failure here is reported rather than swallowed. It is not fatal -
    process_file repeats the same access and counts the error properly - but
    it does change what this source claims (see possible_output_names), and
    it now runs for every PNG and TIFF in the tree rather than only the
    handful of GIFs. A systematic probe failure - a broken Pillow plugin, an
    unreadable work tree - would otherwise shift the whole de-confliction
    pass with nothing in the log to explain the renames that follow."""
    if src is None:
        return None
    try:
        with Image.open(src) as img:
            return bool(getattr(img, "is_animated", False))
    except Exception as exc:  # noqa: BLE001 - one file's problem, not the run's
        if logger is not None:
            logger.error(f"warning: could not determine whether {src} is animated ({exc}); "
                         "planning for it to produce no output")
        return None


def collect_sources(input_dir: Path, logger: Logger, stats: dict) -> list:
    """Every file under input_dir worth processing, sorted.

    `not p.is_dir()` alone is not a "this is a file" test: it is False for a
    **broken symlink** too, and process_file's first act is to stat the
    source. That stat raised out of the whole run rather than being counted
    as one file's error, and insert_optimized_media only catches ValueError -
    so a single dangling link killed the job with an unhandled traceback
    instead of the intended "N file(s) failed to optimize" abort."""
    sources = []
    for path in sorted(input_dir.rglob("*")):
        if path.is_dir():
            continue
        if not path.is_file():  # broken symlink, socket, fifo, ...
            stats["errors"] += 1
            logger.error(f"error: {path} is not a readable regular file; skipping")
            continue
        sources.append(path)
    return sources


def optimize_directory(input_dir: Path, output_dir: Path, *, cfg: dict, pngquant_path: str, logger: Logger,
                        stats: dict) -> OptimizeResult:
    """Walks input_dir recursively, optimizing every file into the mirrored
    location under output_dir (see process_file). Returns an OptimizeResult -
    `written`, every output path this run produced, and `renamed`, the subset
    whose output path differed from its input path (webp conversion, or an SVG
    rasterized to PNG/WEBP), which callers maintaining references to these
    files elsewhere (e.g. insert_optimized_media.py, fixing up image URLs
    stored in a database) use to know what changed."""
    # Writing into the directory being read destroys the originals: every image
    # is optimized straight over its own source, and the only error raised is a
    # copy2 SameFileError on the first *non*-image file, long after the damage.
    # There is no in-place mode, so treat this as a caller bug rather than
    # something to muddle through.
    if input_dir.resolve() == output_dir.resolve():
        raise ValueError(
            f"output directory {output_dir} is the input directory {input_dir}; "
            "optimizing in place would overwrite the originals"
        )

    # Two sources that end up writing the same output file: the second
    # silently clobbers the first, that source image is gone for good, and
    # every page referencing it renders the survivor instead - at exit 0.
    # Neither the loop below nor insert_optimized_media.py's own seen_names
    # guard can catch it, because by the time either runs the collision has
    # already happened here in the work dir.
    #
    # Two things decide whether that happens, and both have to be modelled:
    #
    #  * **The namespace is flat.** insert_optimized_media addresses every
    #    image by bare basename ("k/html/images/<name>"), so "sub-a/logo.png"
    #    and "sub-b/logo.jpg" collide even though they sit in different source
    #    directories. Keying per-directory missed exactly that case, while
    #    populate_db.py was already flattening and warning on it - the two
    #    halves of the pipeline have to model the same namespace.
    #
    #  * **Only the *output* name collides.** Without --webp, "logo.png" and
    #    "logo.jpg" are written unchanged and never collide at all; renaming
    #    one of them anyway put a bogus entry in `renamed`, which made
    #    rewrite_pages rewrite every stored URL and delete/reinsert the row for
    #    a collision that could not occur.
    #
    # So de-conflict on the *predicted output basenames* (see
    # possible_output_names), keyed by basename alone. A source whose possible
    # outputs are all unclaimed keeps its stem untouched.
    sources = collect_sources(input_dir, logger, stats)

    # Two sources that share a *basename* are a different problem from two that
    # merely produce the same output, and they need the opposite treatment.
    #
    # The consumer namespace is flat, so "A/logo.gif" and "B/logo.gif" are one
    # name once inserted, and a page referencing "logo.gif" cannot say which it
    # meant - populate_db already resolves that by keeping the first sorted and
    # warning. De-conflicting them here would instead rename the second to
    # something distinct, which puts an entry in `renamed` for the *shared*
    # name: build_rename_map then repoints every stored /k/html/images/logo.gif
    # at the second file, the first goes unreferenced, and
    # delete_unreferenced_media removes it. That is how an animated GIF got
    # replaced by an unrelated static one - the rename made the collision
    # invisible to the seen_names guard that was supposed to catch it.
    #
    # So: skip the later duplicates outright, which is exactly the "keeping the
    # first, sorted, skipping the rest" this pipeline already documents. Not
    # counted as an error - it is a warned-about condition, not a failure.
    #
    # Keyed on the exact basename, NOT a casefolded one, because the index
    # this defers to is exact: populate_db builds image_index_db with
    # `if base in image_entry_by_name` over the raw names, so "a/Logo.png"
    # and "b/logo.png" are two addressable images there and a page may
    # reference either. Skipping one of them here would leave that page's
    # reference resolving to a row nothing ever inserts - a broken image,
    # exit 0, one warning. Case-differing names are a *collision*, not a
    # duplicate, and `claimed` below already handles them by de-conflicting
    # the second, which rename_map then follows.
    by_basename = {}
    deduped = []
    for src in sources:
        if src.name in by_basename:
            logger.error(
                f"warning: {src} has the same filename as {by_basename[src.name]}; the output namespace is "
                "flat, so only the first can be addressed - keeping the first, skipping this one"
            )
            continue
        by_basename[src.name] = src
        deduped.append(src)
    sources = deduped

    plan_for = {}
    # Keyed casefolded: the work dir is written on whatever filesystem the run
    # happens to use, and on a case-insensitive one (APFS, NTFS) "logo.PNG" and
    # "logo.png" are the same file - so comparing the names as written would
    # miss a collision that still costs an image. insert_optimized_media's own
    # seen_names guard is case-sensitive too and would not catch it either.
    claimed = {}
    for src in sources:
        rel = src.relative_to(input_dir)
        stem, suffix = rel.stem, rel.suffix
        # Probed here rather than inside the loop: the answer depends on the
        # file, not on the candidate name, so re-opening it per de-confliction
        # attempt was pure waste. False without a probe - an extension that
        # cannot carry a second frame is never animated, and opening every
        # JPEG to learn that would cost an extra pass over the whole corpus.
        # Probed for every raster, not for a hand-picked subset of extensions.
        # There used to be an ANIMATABLE_EXTENSIONS set here, chosen to answer
        # "which extensions need a probe to predict an output *name*" - .webp
        # was excluded because an animated one is copied through under the
        # same .webp name a converted one gets, so the answer could not change
        # the name. Sound for naming; wrong the moment optimize_raster started
        # consuming the same value, because it decides whether to re-encode.
        # Every animated WEBP in the tree was flattened to a single frame,
        # silently, with the output name exactly as predicted so the
        # written-vs-claimed check below could not see it either.
        #
        # One question - "is this file animated" - deserves one answer, taken
        # from the file. optimize_raster opens it regardless, so the extra
        # header read is the only cost, and it is measurable at neither end.
        animated = _is_animated_raster(src, logger) if suffix.lower() in RASTER_EXTENSIONS else False
        candidate = stem
        attempt = 0
        while True:
            names = possible_output_names(candidate, suffix, cfg, animated)
            clash = next((n for n in sorted(names) if n.lower() in claimed), None)
            if clash is None:
                break
            attempt += 1
            ext = suffix.lstrip(".").lower() or "file"
            candidate = f"{stem}-{ext}" if attempt == 1 else f"{stem}-{ext}-{attempt}"
            if attempt == 1:
                logger.error(
                    f"warning: {src} would overwrite the output of {claimed[clash.lower()]} "
                    f"(both produce {clash}); writing it under the stem {candidate!r} instead"
                )
        for name in names:
            claimed[name.lower()] = src
        plan_for[src] = Plan(dst_rel=rel.parent / f"{candidate}{suffix}", names=names, animated=animated)

    renamed = {}
    written = []
    for src in sources:
        rel = src.relative_to(input_dir)
        plan = plan_for[src]
        dst = output_dir / plan.dst_rel
        dst_final = process_file(src, dst, cfg=cfg, pngquant_path=pngquant_path, stats=stats, logger=logger,
                                  animated=plan.animated)
        if dst_final is None:
            continue
        # The name this actually wrote has to be one the planning pass held
        # for it. possible_output_names predicts what process_file ->
        # optimize_raster/optimize_svg -> encode_raster will do, in a second
        # place, and that prediction has already drifted twice: it said .webp
        # for animated GIFs while optimize_raster wrote .gif, and then again
        # for animated PNG/TIFF once the first fix special-cased .gif rather
        # than the branch optimize_raster really takes. Both times the symptom
        # was silent - a name claimed but never written, an unrelated image
        # de-conflicted against the phantom and its stored URLs rewritten, or
        # a name written but never claimed, free to overwrite another source's
        # output. Nothing connected the two halves, so nothing noticed.
        #
        # This is that connection. It costs one lookup per file and turns a
        # future drift into a named failure instead of a corrupted image
        # corpus that still exits 0.
        #
        # Asked of `claimed`, and so casefolded like every other lookup in
        # this pass: de-confliction guarantees no two sources hold the same
        # lowercased name, so "is this the source that claimed this name" is
        # exactly `claimed[name] == src`. Comparing exact case instead made
        # the check stricter than the invariant it protects, and turned an SVG
        # spelled ".SVG" - which the planner claimed as ".svg" - into an
        # aborted run.
        #
        # No exemption for a source the planner could not predict: it does not
        # arise any more. optimize_raster takes the planner's answer instead
        # of re-deriving it, so a source whose animation was undetermined
        # raises there, is counted as one file's error, and never reaches
        # here. The exemption that used to sit in this branch keyed off "the
        # probe failed" rather than "the planner claimed nothing" - which
        # differ under --webp false, where the planner claims the source's own
        # name regardless - and so waved genuine drift through as a warning.
        if claimed.get(dst_final.name.lower()) != src:
            # ValueError, not RuntimeError: that is what insert_optimized_media's
            # `except ValueError` around this call turns into a clean
            # "error: ..." line, the same as the input_dir == output_dir
            # refusal above. `plan.names` rather than `claimed`'s keys, so the
            # message shows the claim as the planner wrote it - reporting a
            # casefolded "diagram.svg" for a source that claimed "Diagram.SVG"
            # points the reader at a case mismatch that isn't the problem.
            raise ValueError(
                f"{src} wrote {dst_final.name}, which possible_output_names did not predict "
                f"(it claimed {sorted(plan.names) or 'nothing'}). The output-name planning pass and "
                "process_file have drifted apart - see possible_output_names."
            )
        written.append(dst_final)
        rel_final = dst_final.relative_to(output_dir)
        if rel_final != rel:
            renamed[str(rel)] = str(rel_final)
    return OptimizeResult(renamed=renamed, written=written)


def add_optimize_arguments(parser: argparse.ArgumentParser) -> None:
    """Adds every --tuning-flag (everything except the input/output
    positionals) to `parser` - split out from build_parser() so a caller
    with its own positional arguments (e.g. insert_optimized_media.py, which
    also needs a database path) can still get these for free instead of
    redeclaring them."""
    parser.add_argument("--config", type=Path, default=None,
                         help="Path to a .config file providing any of the options below")
    parser.add_argument("--max-width", type=int, default=None,
                         help=f"Max width in pixels for raster images (default: {BUILTIN_DEFAULTS['max_width']})")
    parser.add_argument("--jpeg-quality", type=int, default=None,
                         help=f"JPEG output quality, 0-95 (default: {BUILTIN_DEFAULTS['jpeg_quality']})")
    parser.add_argument("--webp", action="store_true", default=None,
                         help="Convert optimized raster images (and rasterized SVGs) to WEBP")
    parser.add_argument("--webp-quality", type=int, default=None,
                         help=f"WEBP output quality, 0-100 (default: {BUILTIN_DEFAULTS['webp_quality']})")
    parser.add_argument("--pngquant-speed", type=int, default=None,
                         help="pngquant speed/quality trade-off, 1 (slow/best) - 11 (fast/rough); "
                              f"see https://pngquant.org/ (default: {BUILTIN_DEFAULTS['pngquant_speed']})")
    parser.add_argument("--svg-precision", type=int, default=None,
                         help="Decimal places to round SVG numbers to via Scour "
                              f"(default: {BUILTIN_DEFAULTS['svg_precision']})")
    parser.add_argument("--svg-rasterize-threshold", type=int, default=None,
                         help="Rasterize an optimized SVG if it's still over this many bytes "
                              f"(default: {BUILTIN_DEFAULTS['svg_rasterize_threshold']:,})")
    parser.add_argument("--verbose", action="store_true", default=None,
                         help="Add original/optimized byte sizes to the per-file log line every media file "
                              "already gets, plus all resolved config parameters up front")
    parser.add_argument("--log-file", type=Path, default=None,
                         help="Write all log output here instead of stdout/stderr")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("input_dir", type=Path, nargs="?", default=None,
                         help="Directory to read files from, recursively (or set input-dir in --config)")
    parser.add_argument("output_dir", type=Path, nargs="?", default=None,
                         help="Directory to mirror optimized output into (or set output-dir in --config)")
    add_optimize_arguments(parser)
    return parser


def resolve_config(args: argparse.Namespace, option_specs: dict = OPTION_SPECS,
                    builtin_defaults: dict = BUILTIN_DEFAULTS) -> dict:
    """Merges CLI args over --config file values over builtin_defaults (in
    that precedence order) into one dict keyed by dest name. option_specs/
    builtin_defaults default to this module's own, but are overridable for a
    caller (e.g. insert_optimized_media.py) extending them with its own
    extra options (like "db-path")."""
    file_overrides = load_config_file(args.config, option_specs) if args.config else {}

    cfg = {}
    for dest, _converter in option_specs.values():
        cli_value = getattr(args, dest, None)
        if cli_value is not None:
            cfg[dest] = cli_value
        elif dest in file_overrides:
            cfg[dest] = file_overrides[dest]
        elif dest in builtin_defaults:
            cfg[dest] = builtin_defaults[dest]
        else:
            cfg[dest] = None  # input_dir/output_dir: no built-in default
    return cfg


def main() -> None:
    parser = build_parser()
    args = parser.parse_args()

    try:
        cfg = resolve_config(args)
    except RuntimeError as exc:
        parser.error(str(exc))
        return

    if cfg["input_dir"] is None or cfg["output_dir"] is None:
        parser.error("input_dir and output_dir must be given either as positional arguments or in --config")

    log_file_handle = open(cfg["log_file"], "w", encoding="utf-8") if cfg["log_file"] else None
    logger = Logger(log_file_handle)
    try:
        if not cfg["input_dir"].is_dir():
            logger.error(f"error: {cfg['input_dir']} is not a directory")
            sys.exit(1)

        if cfg["verbose"]:
            logger.info("Config parameters:")
            for key, (dest, _converter) in OPTION_SPECS.items():
                logger.info(f"  {key} = {cfg[dest]}")
            if args.config:
                logger.info(f"  (loaded from {args.config})")

        try:
            pngquant_path = find_pngquant()
        except RuntimeError as exc:
            logger.error(f"error: {exc}")
            sys.exit(1)

        stats = {"raster": 0, "svg": 0, "svg_rasterized": 0, "copied": 0, "errors": 0, "original_bytes": 0,
                  "optimized_bytes": 0}
        try:
            optimize_directory(cfg["input_dir"], cfg["output_dir"], cfg=cfg, pngquant_path=pngquant_path,
                                logger=logger, stats=stats)
        except ValueError as exc:
            logger.error(f"error: {exc}")
            sys.exit(1)

        saved = stats["original_bytes"] - stats["optimized_bytes"]
        pct = (saved / stats["original_bytes"] * 100) if stats["original_bytes"] else 0.0
        logger.info(
            f"Done: {stats['raster']} raster image(s) optimized, {stats['svg']} SVG(s) optimized, "
            f"{stats['svg_rasterized']} SVG(s) rasterized, {stats['copied']} other file(s) copied, "
            f"{stats['errors']} error(s). Total size: {stats['original_bytes']:,} -> {stats['optimized_bytes']:,} "
            f"bytes (saved {saved:,} bytes, {pct:.1f}%)."
        )
        if stats["errors"]:
            sys.exit(1)
    finally:
        if log_file_handle is not None:
            log_file_handle.close()


if __name__ == "__main__":
    main()
