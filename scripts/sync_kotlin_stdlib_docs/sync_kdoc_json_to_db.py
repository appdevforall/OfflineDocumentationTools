#!/usr/bin/env python3
"""
Overwrites the kotlin-stdlib / kotlin-reflect / kotlin-test Content rows in
documentation.db with fresh output from the KDoc-to-JSON Dokka plugin.

For every existing Content row whose path starts with "k/kotlin-stdlib",
"k/kotlin-reflect", or "k/kotlin-test":
  - Compute the corresponding file in the plugin output tree: strip the "k/"
    prefix, and if the path ends in ".html", swap that for ".json" (paths with
    no extension, e.g. ".../package-list", are looked up unchanged).
  - If that file exists, re-compress it (matching the row's existing
    ContentTypes.compression) and overwrite the row's `content` blob only --
    `path`, `languageID`, `contentTypeID`, and `templateId` are left untouched.
  - If it doesn't exist, delete the row.

Any TooltipButtons row whose `uri` (ignoring a trailing "#fragment") matches one
of the deleted Content paths is now a dead link. Its entire parent Tooltips
record -- along with all of that tooltip's other TooltipButtons rows, dead or
not -- is deleted too, since TooltipButtons has no ON DELETE CASCADE and a
dangling tooltipId would otherwise be left behind.

A timestamped backup of the database is made before anything is modified.
"""
import argparse
import os
import shutil
import sqlite3
import sys
from datetime import datetime, timezone

import brotli

PREFIXES = ["k/kotlin-stdlib", "k/kotlin-reflect", "k/kotlin-test"]


def backup_database(db_path):
    timestamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    backup_path = f"{db_path}.bak.{timestamp}"
    shutil.copy2(db_path, backup_path)
    return backup_path


def relative_target_path(content_path):
    """'k/kotlin-stdlib/kotlin.text/index.html' -> 'kotlin-stdlib/kotlin.text/index.json'
    'k/kotlin-stdlib/package-list' -> 'kotlin-stdlib/package-list' (no extension to swap)"""
    without_prefix = content_path[len("k/"):]
    if without_prefix.endswith(".html"):
        return without_prefix[: -len(".html")] + ".json"
    return without_prefix


def compress_for(compression, raw_bytes, path):
    if compression == "brotli":
        return brotli.compress(raw_bytes)
    if compression == "none":
        return raw_bytes
    raise ValueError(f"Unknown compression '{compression}' needed for {path}")


def cleanup_orphaned_tooltips(cur, deleted_paths, dry_run, prune_tooltips=False):
    """Drops every TooltipButton whose uri points at a now-deleted Content path.

    A tooltip itself is removed only when *every* one of its buttons was orphaned, and even then
    only when `prune_tooltips` says so. It used to be removed whenever *any* button was, which
    took the hand-authored `summary`/`detail` and every still-valid sibling button with it - and
    that text is not in the plugin output, so nothing could regenerate it. A stdlib symbol renamed
    upstream was enough to trigger it: its Content row goes, one button is orphaned, the whole
    tooltip goes. The destructive path is opt-in because a docs rebuild should not quietly delete
    hand-written content as a side effect. The buttons are not opt-in: a button pointing at a row
    this run deleted is a dead link and nothing else, so it goes either way.

    Returns (tooltips_removed, buttons_removed).
    """
    if not deleted_paths:
        return 0, 0

    deleted_path_set = set(deleted_paths)

    where_clause = " OR ".join(["uri = ? OR uri LIKE ?"] * len(PREFIXES))
    params = []
    for prefix in PREFIXES:
        params.extend([prefix, prefix + "/%"])

    candidate_buttons = cur.execute(
        f"SELECT tooltipId, uri FROM TooltipButtons WHERE {where_clause}", params
    ).fetchall()

    orphaned = [(tooltip_id, uri) for tooltip_id, uri in candidate_buttons
                if uri.split("#", 1)[0] in deleted_path_set]
    if not orphaned:
        return 0, 0

    # How many buttons each affected tooltip has in total, so a tooltip that still has a working
    # button can be told from one that has none left.
    affected_ids = sorted({tooltip_id for tooltip_id, _uri in orphaned})
    placeholders = ",".join("?" * len(affected_ids))
    total_buttons = dict(cur.execute(
        f"SELECT tooltipId, count(*) FROM TooltipButtons WHERE tooltipId IN ({placeholders}) "
        f"GROUP BY tooltipId", affected_ids
    ).fetchall())
    orphaned_by_tooltip = {}
    for tooltip_id, uri in orphaned:
        orphaned_by_tooltip.setdefault(tooltip_id, []).append(uri)

    fully_orphaned = [t for t in affected_ids
                      if len(orphaned_by_tooltip[t]) >= total_buttons.get(t, 0)]

    # Every orphaned button goes, whichever kind of tooltip it hangs off. It points at a Content
    # row this same run deleted, so leaving it behind only puts a dead link in the IDE's tooltip
    # dialog - there is nothing to preserve. What `prune_tooltips` gates is the *tooltip*, whose
    # summary/detail is hand-authored and absent from the plugin output. Every deletion is named:
    # this is the one place in the pipeline that removes content no other step can put back.
    buttons_removed = 0
    for tooltip_id in affected_ids:
        for uri in orphaned_by_tooltip[tooltip_id]:
            print(f"  [DELETE BUTTON] tooltip={tooltip_id} uri={uri}")
            if not dry_run:
                cur.execute("DELETE FROM TooltipButtons WHERE tooltipId = ? AND uri = ?",
                            (tooltip_id, uri))
            buttons_removed += 1

    if not fully_orphaned:
        return 0, buttons_removed

    if not prune_tooltips:
        print(f"  [KEEP] {len(fully_orphaned)} tooltip(s) have no buttons left, their last one(s) "
              f"having just been dropped; the hand-authored summary/detail is kept, pass "
              f"--prune-tooltips to delete the tooltips too: "
              + ", ".join(f"id={t}" for t in fully_orphaned))
        return 0, buttons_removed

    placeholders = ",".join("?" * len(fully_orphaned))
    for tooltip_id in fully_orphaned:
        print(f"  [DELETE TOOLTIP] id={tooltip_id} "
              f"(all {total_buttons.get(tooltip_id, 0)} button(s) orphaned)")
    if not dry_run:
        # The loop above already dropped these tooltips' buttons one by one; this sweeps up any
        # row it could not address by exact uri (a NULL uri counted in total_buttons, say).
        cur.execute(f"DELETE FROM TooltipButtons WHERE tooltipId IN ({placeholders})", fully_orphaned)
        cur.execute(f"DELETE FROM Tooltips WHERE id IN ({placeholders})", fully_orphaned)

    return len(fully_orphaned), buttons_removed


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument(
        "plugin_output_root",
        help="Root dir directly containing kotlin-stdlib/, kotlin-reflect/, kotlin-test/ "
             "(e.g. .../all-libs from a KDoc-to-JSON run)",
    )
    parser.add_argument("--db", default="documentation.db", help="Path to documentation.db (default: documentation.db in the current directory)")
    parser.add_argument("--dry-run", action="store_true", help="Report what would happen without modifying anything")
    parser.add_argument(
        "--prune-tooltips", action="store_true",
        help="Also delete Tooltips whose buttons have all become orphaned. Off by default: the "
             "summary/detail text is hand-authored and is not in the plugin output, so a docs "
             "rebuild cannot put it back. Orphaned buttons are always dropped either way.")
    args = parser.parse_args()

    if not os.path.isdir(args.plugin_output_root):
        print(f"Error: '{args.plugin_output_root}' is not a directory.", file=sys.stderr)
        sys.exit(2)
    if not os.path.isfile(args.db):
        print(f"Error: database '{args.db}' not found.", file=sys.stderr)
        sys.exit(2)

    if args.dry_run:
        print("Dry run: no backup will be made and no changes will be written.")
    else:
        backup_path = backup_database(args.db)
        print(f"Backed up database to: {backup_path}")

    conn = sqlite3.connect(args.db)
    cur = conn.cursor()

    compression_by_type = dict(cur.execute("SELECT id, compression FROM ContentTypes"))

    where_clause = " OR ".join(["path = ? OR path LIKE ?"] * len(PREFIXES))
    params = []
    for prefix in PREFIXES:
        params.extend([prefix, prefix + "/%"])

    rows = cur.execute(
        f"SELECT id, path, contentTypeID FROM Content WHERE {where_clause}", params
    ).fetchall()

    print(f"Found {len(rows)} existing Content record(s) under {PREFIXES}.")

    updated = 0
    deleted = 0
    deleted_paths = []
    unknown_types = set()

    try:
        conn.execute("BEGIN")
        for content_id, path, content_type_id in rows:
            rel_target = relative_target_path(path)
            source_file = os.path.join(args.plugin_output_root, rel_target)

            if os.path.isfile(source_file):
                with open(source_file, "rb") as f:
                    raw_bytes = f.read()

                compression = compression_by_type.get(content_type_id)
                if compression is None:
                    unknown_types.add(content_type_id)
                    compression = "none"

                new_blob = compress_for(compression, raw_bytes, path)

                if args.dry_run:
                    print(f"  [UPDATE] {path}  <-  {rel_target}")
                else:
                    cur.execute("UPDATE Content SET content = ? WHERE id = ?", (new_blob, content_id))
                updated += 1
            else:
                if args.dry_run:
                    print(f"  [DELETE] {path}  (no matching {rel_target})")
                else:
                    cur.execute("DELETE FROM Content WHERE id = ?", (content_id,))
                deleted += 1
                deleted_paths.append(path)

        tooltips_removed, buttons_removed = cleanup_orphaned_tooltips(
            cur, deleted_paths, args.dry_run, args.prune_tooltips)

        if unknown_types:
            print(
                f"WARNING: contentTypeID(s) {sorted(unknown_types)} not found in ContentTypes; "
                "treated as uncompressed.",
                file=sys.stderr,
            )

        if args.dry_run:
            conn.rollback()
            print(
                f"\nDry run complete: would update {updated}, delete {deleted} Content record(s); "
                f"would delete {tooltips_removed} Tooltips record(s) ({buttons_removed} TooltipButtons "
                "row(s)). No changes made."
            )
        else:
            conn.commit()
            print(
                f"\nDone: updated {updated}, deleted {deleted} Content record(s); "
                f"deleted {tooltips_removed} Tooltips record(s) ({buttons_removed} TooltipButtons row(s))."
            )
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


if __name__ == "__main__":
    main()
