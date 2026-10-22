#!/usr/bin/env python3
# /// script
# requires-python = ">=3.9"
# dependencies = [
#     "brotli",
#     "Pillow",
# ]
# ///
"""
set_content_type_compression.py

Changes how every Content row of one content type is stored - Brotli or
uncompressed - and records the change in ContentTypes, in one transaction:

    uv run scripts/set_content_type_compression.py documentation.db image/webp none
    uv run scripts/set_content_type_compression.py documentation.db video/webm none --create

Why the two happen together: a row's compression is not stored with the row.
WebServer.kt, and every tool here, looks it up in ContentTypes.compression for
the row's type, so changing that flag without re-storing the rows would make
every existing row of the type unreadable - and re-storing them without
changing it would do the same.

Why it exists (ADFA-3305): image/webp was marked brotli. WebP is already
entropy-coded; over a production database Brotli saved 0.48% on its 921 rows,
left 654 of them no smaller, and every fetch paid for a full decompress.

The re-store is lossless: each row is decoded with the old setting, stored with
the new one, and decoded again before the commit to check it reads back as the
same content. Running the script again with the old setting reverses it.

--create adds the type when the database has no row for it, as video/webm needs
before the first optimize_db_media.py --webm run. No row can carry a type that
did not exist, so there is nothing to re-store.

--manifest PATH: optimize_db_media.py's manifest records each media file's
STORED size and sha256. Re-storing a row changes both, so an older manifest
would see every re-stored row as edited, and the next optimizer run would
re-encode them - lossy WebP from lossy WebP. Passing the manifest here updates
the entry of every row this run re-stores, provided the entry still described
the row; an entry that already did not match was stale before this run and is
left as it is. The manifest is written before the commit, so a manifest that
cannot be written takes the change down with it.

Backs the database up first (VACUUM INTO a timestamped sibling, like the other
tools), VACUUMs at the end, and supports --dry-run.
"""
from __future__ import annotations

import argparse
import sqlite3
import sys
from pathlib import Path

from optimize_db_media import (
    BrotliCodec, backup_database, digest, is_continuation_path, load_dictionary, read_manifest,
    reassemble, write_manifest, write_media_row,
)

COMPRESSIONS = ("none", "brotli")


def run(cfg: dict) -> int:
    db_path, value, target = cfg["db_path"], cfg["content_type"], cfg["compression"]
    if target not in COMPRESSIONS:
        print(f"error: compression must be one of {', '.join(COMPRESSIONS)}, not {target!r}", file=sys.stderr)
        return 1
    if not db_path.is_file():
        print(f"error: {db_path} does not exist", file=sys.stderr)
        return 1
    manifest_path = cfg.get("manifest")
    manifest = None
    if manifest_path is not None:
        try:
            manifest = read_manifest(manifest_path)
        except (OSError, RuntimeError) as exc:
            print(f"error: cannot read --manifest {manifest_path}: {exc}", file=sys.stderr)
            return 1

    conn = sqlite3.connect(db_path)
    try:
        row = conn.execute("SELECT id, compression FROM ContentTypes WHERE value = ?", (value,)).fetchone()
        if row is None and not cfg.get("create"):
            print(f"error: {db_path} has no {value!r} content type; pass --create to add it", file=sys.stderr)
            return 1
        if row is not None and row[1] == target:
            print(f"{value} is already stored as {target}; nothing to do.")
            return 0
    finally:
        conn.close()

    if cfg["dry_run"]:
        print(f"Dry run: no backup, no changes committed. Re-storing {value} in {db_path} as {target} ...")
    else:
        print(f"Backing up {db_path} ...")
        print(f"Backup written to {backup_database(db_path)}")

    warn = lambda message: print(message, file=sys.stderr)  # noqa: E731
    conn = sqlite3.connect(db_path)
    codec = None
    restored = manifest_updated = 0
    try:
        conn.execute("BEGIN")
        if row is None:
            conn.execute("INSERT INTO ContentTypes (value, compression) VALUES (?, ?)", (value, target))
            print(f"Added content type {value} ({target}).")
        else:
            type_id, current = row
            codec = BrotliCodec(load_dictionary(conn))
            rows = conn.execute("SELECT path, content, languageID, templateId FROM Content "
                                "WHERE contentTypeID = ? ORDER BY path", (type_id,)).fetchall()
            lengths = {path: len(blob) for path, blob, _language, _template in rows}
            for path, blob, language_id, template_id in rows:
                if is_continuation_path(lengths, path):
                    continue  # written again as part of its base row
                stored = reassemble(conn, path, blob)
                raw = codec.decompress(stored, current)
                new_stored = codec.compress(raw, target)
                write_media_row(conn, path, len(blob), new_stored, language_id, type_id, template_id, warn)
                # Read back through the same chunking the server uses, so a
                # write that would not reassemble fails here, not on a device.
                first = conn.execute("SELECT content FROM Content WHERE path = ?", (path,)).fetchone()[0]
                if codec.decompress(reassemble(conn, path, first), target) != raw:
                    raise RuntimeError(f"{path} did not read back as the same content after re-storing it")
                restored += 1
                entry = manifest.get(path) if manifest is not None else None
                if entry and entry[0] == digest(stored) and entry[1] == len(stored):
                    manifest[path] = (digest(new_stored), len(new_stored), entry[2])
                    manifest_updated += 1
            conn.execute("UPDATE ContentTypes SET compression = ? WHERE id = ?", (target, type_id))
            print(f"Re-stored {restored} {value} row(s) from {current} to {target}.")

        if cfg["dry_run"]:
            conn.rollback()
        else:
            if manifest is not None and manifest_updated:
                temporary = manifest_path.with_name(manifest_path.name + ".tmp")
                try:
                    write_manifest(temporary, manifest)
                    temporary.replace(manifest_path)
                except OSError as exc:
                    temporary.unlink(missing_ok=True)
                    print(f"error: could not update the manifest: {exc}. Rolling back - the rows it "
                          "describes must not change without it.", file=sys.stderr)
                    conn.rollback()
                    return 1
                print(f"Updated {manifest_updated} entry(ies) in {manifest_path}.")
            conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        if codec is not None:
            codec.close()
        conn.close()

    if not cfg["dry_run"] and restored:
        print("Vacuuming database...")
        vac = sqlite3.connect(db_path)
        try:
            vac.execute("VACUUM")
        finally:
            vac.close()
    return 0


def main() -> None:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("db_path", type=Path, help="SQLite database to change in place, e.g. documentation.db")
    p.add_argument("content_type", help="The ContentTypes.value to change, e.g. image/webp")
    p.add_argument("compression", help=f"How to store it: {' or '.join(COMPRESSIONS)}")
    p.add_argument("--create", action="store_true", help="Add the content type if the database has none")
    p.add_argument("--manifest", type=Path, default=None, metavar="PATH",
                   help="An optimize_db_media.py manifest to keep in step with the re-stored rows")
    p.add_argument("--dry-run", action="store_true", help="Do all the work, then roll back without writing")
    args = p.parse_args()
    sys.exit(run({"db_path": args.db_path, "content_type": args.content_type, "compression": args.compression,
                  "create": args.create, "manifest": args.manifest, "dry_run": args.dry_run}))


if __name__ == "__main__":
    main()
