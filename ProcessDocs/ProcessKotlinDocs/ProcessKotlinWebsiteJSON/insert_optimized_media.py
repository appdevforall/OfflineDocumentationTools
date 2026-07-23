#!/usr/bin/env python3
"""
insert_optimized_media.py

Runs optimize_media.py's image optimizer over a directory of raw media,
then updates an existing documentation.db-schema database (as
populate_db.py produces) with the optimized results, fixing up every page
that referenced a file under its old name.

What this does, inside a single transaction (rolled back on any error):
  1. Backs up <db_path> first, same as populate_db.py (VACUUM INTO a
     timestamped sibling file).
  2. Optimizes every file under <media_dir> into a staging directory
     (--work-dir, or a temporary one removed afterwards) via
     optimize_media.py's own pipeline - see its own module docstring for
     what "optimized" means (resize, pngquant, Scour, optional WEBP
     conversion / SVG rasterization). Aborts before touching the database
     if any file fails to optimize.
  3. Replaces every "k/html/images/<name>" Content row with the optimized
     bytes, deleting the old row (and any leftover chunked fragments) first
     - Content.path is UNIQUE, so a stale row has to go before its
     replacement can be inserted. Images are addressed by bare filename
     only, matching populate_db.py's own flat "k/html/images/*" convention:
     <media_dir> subdirectories are flattened to their basename, and a
     basename collision across two different subdirectories is a warning
     (keeping the first, sorted, skipping the rest), not an error.
  4. Wherever optimization renamed a file (webp conversion, or an oversized
     SVG rasterized to PNG/WEBP), rewrites every "/k/html/images/<old-name>"
     reference still pointing at the old name, in every k/html/*.html page
     and the nav row, to the new name - so a page doesn't end up linking to
     a filename that no longer exists.
  5. Deletes every remaining "k/html/images/<name>" row (base row and any
     chunked fragments) that, after the rename rewriting above, no
     k/html/*.html page or the nav row references even once - not just ones
     touched by this run's rename_map, but every currently-stored image,
     so media that fell out of use in an earlier run (e.g. a topic's .md
     was deleted, or an <img> reference was removed by hand) gets cleaned
     up too, not just this run's renames.
  6. VACUUMs the database afterwards (outside the transaction - SQLite
     refuses to VACUUM inside one), same as populate_db.py.

Usage:
    python3 insert_optimized_media.py <media_dir> <db_path> [work_dir] [options]
    python3 insert_optimized_media.py --config myjob.config

<options> are optimize_media.py's own tuning flags (--max-width,
--jpeg-quality, --webp, --webp-quality, --pngquant-speed, --svg-precision,
--svg-rasterize-threshold, --verbose, --log-file, --config) - see
optimize_media.py's own docstring for what each one does. media-dir/db-path/
work-dir can also be set via --config (as "input-dir"/"db-path"/
"output-dir"), the same as optimize_media.py's own options.

Note: --webp requires this database's ContentTypes table to already have an
"image/webp" row (checked up front, before any optimization work starts) -
this project's documentation.db doesn't ship with one.
"""
import argparse
import re
import shutil
import sqlite3
import sys
import tempfile
from pathlib import Path

import brotli

from optimize_media import (
    BUILTIN_DEFAULTS, Logger, OPTION_SPECS, add_optimize_arguments, find_pngquant, optimize_directory,
    resolve_config,
)
from populate_db import (
    CHUNK_SIZE, EXTENSION_TO_CONTENT_TYPE, IMAGES_DB_PATH_PREFIX, IMAGES_URL_PREFIX, LANGUAGE, PAGE_CONTENT_TYPE,
    backup_database, get_content_type, get_id, insert_chunked_content,
)

WEBP_CONTENT_TYPE = "image/webp"
# populate_db.py's own EXTENSION_TO_CONTENT_TYPE has no ".webp" entry - its
# image source (a Writerside export) never produces one, but
# optimize_media.py's --webp does, so it's added here rather than touching
# that shared dict.
IMAGE_EXTENSION_TO_CONTENT_TYPE = {**EXTENSION_TO_CONTENT_TYPE, ".webp": WEBP_CONTENT_TYPE}

# This script's own options, layered on top of optimize_media.py's (db-path
# has no equivalent there) - passed to resolve_config/load_config_file so
# --config can set any of them, the same mechanism optimize_media.py uses
# for its own options.
OWN_OPTION_SPECS = {**OPTION_SPECS, "db-path": ("db_path", Path)}


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("input_dir", type=Path, nargs="?", default=None, metavar="media_dir",
                         help="Directory of raw media to optimize, recursively (or set input-dir in --config)")
    parser.add_argument("db_path", type=Path, nargs="?", default=None,
                         help="SQLite database to update, e.g. documentation.db (or set db-path in --config)")
    parser.add_argument("output_dir", type=Path, nargs="?", default=None, metavar="work_dir",
                         help="Staging directory for optimized files; default: a temporary directory removed "
                              "afterwards (or set output-dir in --config)")
    add_optimize_arguments(parser)
    return parser


def delete_content(conn, path: str) -> None:
    """Deletes a Content row and any chunked continuation fragments for it
    (see insert_chunked_content/CHUNK_SIZE) - safe to call even if nothing
    exists yet at that path. Content.path is UNIQUE, so this has to run
    before any re-insert at the same path."""
    conn.execute("DELETE FROM Content WHERE path = ? OR path LIKE ?", (path, f"{path}-%"))


def insert_optimized_file(conn, data: bytes, name: str, db_path: str, language_id: int, content_type_cache: dict,
                           chunked_log: list) -> bool:
    """Inserts one already-optimized file's bytes as-is. Unlike
    populate_db.py's own insert_file, this does not run pngquant itself -
    optimize_media.py already did, and running it again here would just
    re-quantize an already-quantized image for no benefit. Returns False
    (skipping the file, with a warning) for an extension with no known
    content type."""
    content_type_value = IMAGE_EXTENSION_TO_CONTENT_TYPE.get(Path(name).suffix.lower())
    if content_type_value is None:
        print(f"warning: no known content type for {name!r}; skipping", file=sys.stderr)
        return False
    if content_type_value not in content_type_cache:
        content_type_cache[content_type_value] = get_content_type(conn, content_type_value)
    content_type_id, compress = content_type_cache[content_type_value]

    if compress:
        data = brotli.compress(data)
    delete_content(conn, db_path)
    insert_chunked_content(conn, db_path, language_id, content_type_id, 0, data, chunked_log)
    return True


def build_rename_map(manifest: dict, logger: Logger) -> dict:
    """Flattens optimize_directory's {relative_src: relative_dst} manifest
    to {old_basename: new_basename}, matching k/html/images/*'s bare-filename
    addressing. Warns (keeping the first) if two different renames collide
    on the same old basename - e.g. two identically-named files in
    different subdirectories of media_dir."""
    rename_map = {}
    for old_rel, new_rel in sorted(manifest.items()):
        old_name = Path(old_rel).name
        new_name = Path(new_rel).name
        if old_name == new_name:
            continue
        if old_name in rename_map and rename_map[old_name] != new_name:
            logger.error(
                f"warning: {old_rel!r} and an earlier file both renamed from {old_name!r}, to different names "
                f"({rename_map[old_name]!r} vs {new_name!r}); keeping the first"
            )
            continue
        rename_map[old_name] = new_name
    return rename_map


def reassemble_content(conn, path: str, first_content: bytes) -> bytes:
    """Reassembles a possibly-chunked row's full bytes - mirrors
    WebServer.kt's own reassembly protocol (see CHUNK_SIZE's docstring in
    populate_db.py): a row is fragmented purely when its content is exactly
    CHUNK_SIZE bytes, in which case "<path>-1", "<path>-2", ... are
    concatenated until a missing or shorter-than-CHUNK_SIZE row is hit."""
    if len(first_content) < CHUNK_SIZE:
        return first_content
    parts = [first_content]
    n = 1
    while True:
        row = conn.execute("SELECT content FROM Content WHERE path = ?", (f"{path}-{n}",)).fetchone()
        if row is None:
            break
        parts.append(row[0])
        if len(row[0]) < CHUNK_SIZE:
            break
        n += 1
    return b"".join(parts)


def rewrite_pages(conn, rename_map: dict, language_id: int, page_content_type_id: int, logger: Logger,
                   chunked_log: list) -> int:
    """Rewrites every k/html/*.html page (and the nav row) that references a
    renamed image, replacing "/k/html/images/<old-name>" with
    "/k/html/images/<new-name>" wherever it appears. Operates directly on
    each row's decompressed JSON text rather than parsing it: every image
    reference is a literal IMAGES_URL_PREFIX+filename substring, baked in at
    conversion time by md_to_json.py's Converter (resolve_image_src), so a
    plain text substitution finds it correctly regardless of which block
    type it ends up nested inside - no need to understand that nested block
    schema here. The match is anchored on the escaped quote (\\") that
    always immediately follows a rewritten src="..." attribute in the
    stored JSON (see resolve_image_src/rewrite_urls - image references are
    only ever embedded as HTML attributes, never as a bare JSON field on
    their own), so a renamed file's name can't accidentally match as a
    prefix of some other, unrelated, longer filename. Returns the number of
    rows changed.

    ".html" is the exact literal suffix populate_db.py gives every base
    page/nav row; fragment continuation rows are named "<path>-<N>" (the
    "-N" appended after the ".html" already in path), so the path filter
    below naturally excludes them without needing to detect chunking up
    front.

    Substitutes in a single pass over each row's original text (one regex
    covering every old_name, dispatched through `replacements` by exact
    match) rather than N sequential str.replace calls on a mutating buffer.
    Sequential replaces would risk a chain rename: if one rename's new_name
    equals another rename's old_name (e.g. foo.png -> foo.webp and,
    unrelated, foo.webp -> foo-2.webp), a later replace could re-match text
    an earlier replace just wrote, sending an original foo.png reference to
    foo-2.webp instead of foo.webp. Scanning the untouched original text
    once makes that impossible."""
    if not rename_map:
        return 0

    rows = conn.execute(
        "SELECT path, content, templateId FROM Content WHERE path LIKE 'k/html/%.html' AND contentTypeID = ? "
        "AND templateId != 0",
        (page_content_type_id,),
    ).fetchall()

    replacements = {
        f'{IMAGES_URL_PREFIX}{old_name}\\"': f'{IMAGES_URL_PREFIX}{new_name}\\"'
        for old_name, new_name in rename_map.items()
    }
    old_ref_pattern = re.compile("|".join(re.escape(old_ref) for old_ref in replacements))

    changed = 0
    for path, first_content, template_id in rows:
        full = reassemble_content(conn, path, first_content)
        text = brotli.decompress(full).decode("utf-8")
        hits = len(old_ref_pattern.findall(text))
        if not hits:
            continue
        new_text = old_ref_pattern.sub(lambda m: replacements[m.group(0)], text)
        blob = brotli.compress(new_text.encode("utf-8"))
        delete_content(conn, path)
        insert_chunked_content(conn, path, language_id, page_content_type_id, template_id, blob, chunked_log)
        changed += 1
        logger.info(f"[URL FIX] {path}: updated {hits} image reference(s)")
    return changed


# Matches a rewritten image src's filename, anchored the same way
# rewrite_pages' own known-rename substitutions are: resolve_image_src/
# rewrite_urls only ever embed an image reference as an HTML src="..."
# attribute, which - JSON-encoded - always has the escaped quote (\") right
# after it, so this can't accidentally swallow past the end of the filename.
IMAGE_REF_RE = re.compile(re.escape(IMAGES_URL_PREFIX) + r'([^\\"]+)\\"')


def collect_referenced_media(conn, page_content_type_id: int) -> set:
    """Bare filenames (e.g. "mascot.png") referenced by at least one
    src="/k/html/images/<name>" anywhere across current k/html/*.html page
    content and the nav row - the same row selection/reassembly
    rewrite_pages uses, just extracting every image reference found instead
    of only substituting the ones in a known rename_map."""
    rows = conn.execute(
        "SELECT path, content FROM Content WHERE path LIKE 'k/html/%.html' AND contentTypeID = ? AND templateId != 0",
        (page_content_type_id,),
    ).fetchall()
    referenced = set()
    for path, first_content in rows:
        full = reassemble_content(conn, path, first_content)
        text = brotli.decompress(full).decode("utf-8")
        referenced.update(IMAGE_REF_RE.findall(text))
    return referenced


def list_stored_media(conn) -> dict:
    """Bare filename -> Content.path (e.g. "mascot.png" -> "k/html/images/
    mascot.png") for every image currently stored under IMAGES_DB_PATH_PREFIX,
    collapsing chunked continuation fragments ("<path>-1", "<path>-2", ...)
    back into their base row, since deleting the base via delete_content
    already takes its fragments with it (see CHUNK_SIZE's docstring in
    populate_db.py for that fragmentation convention). A path is treated as
    a fragment when stripping a trailing "-<digits>" yields another path
    that's also present - the same convention this whole pipeline already
    relies on elsewhere, ambiguous only for a base filename that itself
    looks like "<other-existing-file>-<digits>", which no real optimized
    media filename does."""
    paths = {row[0] for row in conn.execute(
        "SELECT path FROM Content WHERE path LIKE ?", (f"{IMAGES_DB_PATH_PREFIX}%",)
    )}

    def is_fragment(path: str) -> bool:
        prefix, sep, suffix = path.rpartition("-")
        return sep == "-" and suffix.isdigit() and prefix in paths

    return {path[len(IMAGES_DB_PATH_PREFIX):]: path for path in paths if not is_fragment(path)}


def delete_unreferenced_media(conn, page_content_type_id: int, logger: Logger) -> int:
    """Deletes every currently-stored k/html/images/<name> row (base row and
    any chunked fragments) that no page or the nav row references even once.
    Must run after insertion and rename-rewriting, so it sees the final,
    up-to-date state of both stored media and in-content references - a file
    renamed this run is only "unreferenced" under its stale old name, which
    rewrite_pages will have already fixed up by the time this runs. Returns
    the number of images removed."""
    stored = list_stored_media(conn)
    referenced = collect_referenced_media(conn, page_content_type_id)
    removed = 0
    for name, path in sorted(stored.items()):
        if name in referenced:
            continue
        delete_content(conn, path)
        removed += 1
        logger.info(f"[UNUSED] removed {path} (not referenced by any page)")
    return removed


def main() -> None:
    parser = build_parser()
    args = parser.parse_args()

    try:
        cfg = resolve_config(args, OWN_OPTION_SPECS, BUILTIN_DEFAULTS)
    except RuntimeError as exc:
        parser.error(str(exc))
        return

    if cfg["input_dir"] is None or cfg["db_path"] is None:
        parser.error("media_dir and db_path must be given either as positional arguments or in --config")

    log_file_handle = open(cfg["log_file"], "w", encoding="utf-8") if cfg["log_file"] else None
    logger = Logger(log_file_handle)
    work_dir_is_temp = cfg["output_dir"] is None
    work_dir = cfg["output_dir"] or Path(tempfile.mkdtemp(prefix="insert_optimized_media_"))

    try:
        if not cfg["input_dir"].is_dir():
            logger.error(f"error: {cfg['input_dir']} is not a directory")
            sys.exit(1)
        if not cfg["db_path"].is_file():
            logger.error(f"error: {cfg['db_path']} does not exist")
            sys.exit(1)

        if cfg["verbose"]:
            logger.info("Config parameters:")
            for key, (dest, _converter) in OWN_OPTION_SPECS.items():
                logger.info(f"  {key} = {cfg.get(dest)}")
            logger.info(f"  work-dir = {work_dir}{' (temporary)' if work_dir_is_temp else ''}")
            if args.config:
                logger.info(f"  (loaded from {args.config})")

        try:
            pngquant_path = find_pngquant()
        except RuntimeError as exc:
            logger.error(f"error: {exc}")
            sys.exit(1)

        # Fail fast on a schema this database doesn't support - before
        # spending time optimizing every file - rather than discovering it
        # partway through the (rolled-back, but still wasted) DB transaction.
        preflight_conn = sqlite3.connect(cfg["db_path"])
        try:
            get_id(preflight_conn, "Languages", LANGUAGE)
            get_id(preflight_conn, "ContentTypes", PAGE_CONTENT_TYPE)
            if cfg["webp"]:
                get_content_type(preflight_conn, WEBP_CONTENT_TYPE)
        except RuntimeError as exc:
            logger.error(f"error: {exc}")
            sys.exit(1)
        finally:
            preflight_conn.close()

        work_dir.mkdir(parents=True, exist_ok=True)
        stats = {"raster": 0, "svg": 0, "svg_rasterized": 0, "copied": 0, "errors": 0, "original_bytes": 0,
                  "optimized_bytes": 0}
        logger.info(f"Optimizing media from {cfg['input_dir']} into {work_dir}...")
        manifest = optimize_directory(cfg["input_dir"], work_dir, cfg=cfg, pngquant_path=pngquant_path,
                                       logger=logger, stats=stats)
        if stats["errors"]:
            logger.error(
                f"error: {stats['errors']} file(s) failed to optimize; aborting before touching the database"
            )
            sys.exit(1)
        rename_map = build_rename_map(manifest, logger)

        logger.info(f"Backing up {cfg['db_path']}...")
        backup_path = backup_database(cfg["db_path"])
        logger.info(f"Backup written to {backup_path}")

        conn = sqlite3.connect(cfg["db_path"])
        try:
            conn.execute("BEGIN")
            language_id = get_id(conn, "Languages", LANGUAGE)
            page_content_type_id = get_id(conn, "ContentTypes", PAGE_CONTENT_TYPE)

            content_type_cache = {}
            chunked_log = []
            inserted = 0
            seen_names = {}
            for out_path in sorted(work_dir.rglob("*")):
                if out_path.is_dir():
                    continue
                name = out_path.name
                if name in seen_names:
                    logger.error(
                        f"warning: {out_path} has the same filename as {seen_names[name]}; keeping the first, "
                        "skipping this one"
                    )
                    continue
                seen_names[name] = out_path
                db_path = f"{IMAGES_DB_PATH_PREFIX}{name}"
                if insert_optimized_file(conn, out_path.read_bytes(), name, db_path, language_id, content_type_cache,
                                          chunked_log):
                    inserted += 1
                    if cfg["verbose"]:
                        logger.info(f"[OK] {out_path} -> {db_path}")

            # A renamed file's old basename no longer appears anywhere under
            # work_dir (that's what makes it a rename), so the loop above
            # never visits its old db_path to replace it - it'd otherwise
            # linger forever as an orphaned, no-longer-referenced row.
            removed = 0
            for old_name in rename_map:
                old_db_path = f"{IMAGES_DB_PATH_PREFIX}{old_name}"
                delete_content(conn, old_db_path)
                removed += 1
                if cfg["verbose"]:
                    logger.info(f"[REMOVED] {old_db_path} (renamed to {IMAGES_DB_PATH_PREFIX}{rename_map[old_name]})")

            changed_pages = rewrite_pages(conn, rename_map, language_id, page_content_type_id, logger, chunked_log)

            unreferenced_removed = delete_unreferenced_media(conn, page_content_type_id, logger)

            conn.commit()
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()

        logger.info("Vacuuming database to reclaim freed space...")
        vacuum_conn = sqlite3.connect(cfg["db_path"])
        try:
            vacuum_conn.execute("VACUUM")
        finally:
            vacuum_conn.close()

        logger.info(
            f"Done: inserted/updated {inserted} image(s) in {cfg['db_path']}, {removed} stale renamed-away row(s) "
            f"removed, {changed_pages} page(s)/nav row(s) updated to match {len(rename_map)} renamed file(s), "
            f"{unreferenced_removed} unreferenced image(s) deleted."
        )
        if chunked_log:
            logger.info(f"Chunked {len(chunked_log)} file(s) over {CHUNK_SIZE:,} bytes:")
            for path, total_size, chunk_count in chunked_log:
                logger.info(f"  {path}: {total_size:,} bytes -> {chunk_count} chunks")
    finally:
        if log_file_handle is not None:
            log_file_handle.close()
        if work_dir_is_temp:
            shutil.rmtree(work_dir, ignore_errors=True)


if __name__ == "__main__":
    main()
