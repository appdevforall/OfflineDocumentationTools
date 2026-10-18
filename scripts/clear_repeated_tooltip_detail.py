#!/usr/bin/env python3
"""
clear_repeated_tooltip_detail.py

Empties Tooltips.detail where it is the same text as Tooltips.summary.

In the 2026-10-18 database 1,605 tooltips (1,592 Kotlin, 13 Java) repeat their
summary as their detail, 151,274 bytes in all; 25,285 others already have an
empty detail.

What the user sees differently (ToolTipManager.kt, the only reader of detail):
- The first popup shows the summary, as before.
- "See More" shows when the detail is not blank or the tooltip has buttons.
  For a tooltip with buttons it still shows, and the second popup now lists
  just the buttons' links instead of the summary again, a blank line, then the
  links. All 1,605 tooltips in that database have buttons.
- A tooltip with no buttons loses "See More", whose popup held only the
  summary again (and the sponsor button every second popup shows). This step
  counts such tooltips and says so.
- The info dialog and the feedback e-mail show an empty Detail.

The comparison is exact (Python string equality), whatever collation the
schema gives the columns. Tooltips are not Brotli-compressed, so the shared
dictionary does not need re-minting.

Measured on the 2026-10-18 database after ADFA-6627, packed with
rebuild_database.py --pack Content,Tooltips --page-size 4096: the file is
151,552 bytes smaller; its brotli -q 11 -w 24 is 10,049 bytes smaller, which is
within the 0.2 MB that repacking alone moves it.

Runs in one transaction (rolled back on any error), backs the database up
first, and VACUUMs afterwards. --dry-run reports what would change without
writing. A second run finds nothing to do and exits 0.

Pipeline position: after every step that writes Tooltips, before
rebuild_database.py.

    uv run scripts/clear_repeated_tooltip_detail.py documentation.db [--dry-run] [--verbose]
"""
from __future__ import annotations

import argparse
import sqlite3
import sys
from collections import Counter
from pathlib import Path

from optimize_db_media import backup_database, human


def run(cfg: dict) -> int:
    db_path = cfg["db_path"]
    if not db_path.is_file():
        print(f"error: {db_path} does not exist", file=sys.stderr)
        return 1

    conn = sqlite3.connect(db_path)
    try:
        columns = {row[1] for row in conn.execute("PRAGMA table_info(Tooltips)")}
        if not {"id", "categoryId", "tag", "summary", "detail"} <= columns:
            print("error: this database has no Tooltips table with id, categoryId, tag, summary and detail",
                  file=sys.stderr)
            return 1
        rows = conn.execute(
            "SELECT T.id, T.tag, T.summary, T.detail, COALESCE(TC.category, '?'), "
            "EXISTS (SELECT 1 FROM TooltipButtons B WHERE B.tooltipId = T.id) "
            "FROM Tooltips T LEFT JOIN TooltipCategories TC ON TC.id = T.categoryId ORDER BY T.id").fetchall()
        repeated = [row for row in rows if isinstance(row[3], str) and row[3] != "" and row[3] == row[2]]
        saved = sum(len(row[3].encode("utf-8")) for row in repeated)
        by_category = Counter(row[4] for row in repeated)
        no_buttons = [row for row in repeated if not row[5]]

        if not repeated:
            print(f"Nothing to do: none of {len(rows)} tooltip(s) repeats its summary as its detail.")
            return 0
        print(f"{len(repeated)} of {len(rows)} tooltip(s) repeat their summary as their detail "
              f"({', '.join(f'{category} {count}' for category, count in sorted(by_category.items()))}); "
              f"emptying it saves {human(saved)} byte(s).")
        if no_buttons:
            print(f"  {len(no_buttons)} of them have no buttons, so their tooltip loses its See More link, "
                  "which led only to the same text again.")

        if cfg["dry_run"]:
            print("Dry run: nothing written.")
            return 0
        print(f"Backing up {db_path} ...")
        print(f"Backup written to {backup_database(db_path)}")
        conn.execute("BEGIN")
        try:
            for tooltip_id, tag, _summary, _detail, category, _buttons in repeated:
                conn.execute("UPDATE Tooltips SET detail = '' WHERE id = ?", (tooltip_id,))
                if cfg["verbose"]:
                    print(f"  [CLEAR] {category} {tag}")
            conn.commit()
        except Exception:
            conn.rollback()
            raise
    finally:
        conn.close()

    print("Vacuuming database...")
    vac = sqlite3.connect(db_path)
    try:
        vac.execute("VACUUM")
    finally:
        vac.close()
    print(f"\nDone. emptied the detail of {len(repeated)} tooltip(s), saving {human(saved)} byte(s).")
    return 0


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("db_path", type=Path, help="SQLite database to edit in place, e.g. documentation.db")
    p.add_argument("--dry-run", action="store_true",
                   help="Report what would change, without writing or backing up")
    p.add_argument("--verbose", action="store_true", help="Log every tooltip changed")
    return p


def main() -> None:
    args = build_parser().parse_args()
    sys.exit(run({"db_path": args.db_path, "dry_run": args.dry_run, "verbose": args.verbose}))


if __name__ == "__main__":
    main()
