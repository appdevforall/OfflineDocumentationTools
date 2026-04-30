#!/usr/bin/env python3
"""Show SQLite database summary: tables and row counts in a Flet window."""

import argparse
import sqlite3
from pathlib import Path

import flet as ft


def get_summary(db_path: Path) -> list[tuple[str, int]]:
    """Return list of (table_name, row_count) for all user tables."""
    result: list[tuple[str, int]] = []
    with sqlite3.connect(db_path) as conn:
        cur = conn.execute(
            "SELECT name FROM sqlite_master WHERE type = 'table' AND name NOT LIKE 'sqlite_%' ORDER BY name"
        )
        for (name,) in cur.fetchall():
            quoted = '"' + name.replace('"', '""') + '"'
            cur = conn.execute(f"SELECT COUNT(*) FROM {quoted}")
            result.append((name, cur.fetchone()[0]))
    return result


def main(page: ft.Page, summary: list[tuple[str, int]], error: str | None) -> None:
    page.title = "DB Summary"
    if error:
        page.add(ft.Text(f"Error: {error}", color=ft.Colors.RED, selectable=True))
        return
    for table_name, count in summary:
        page.add(ft.Text(f"{table_name}: {count}", selectable=True))


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Show SQLite table summary in a window.")
    parser.add_argument("database", type=Path, help="Path to SQLite database file")
    args = parser.parse_args()

    error: str | None = None
    summary: list[tuple[str, int]] = []
    if not args.database.exists():
        error = f"File not found: {args.database}"
    else:
        try:
            summary = get_summary(args.database)
        except sqlite3.Error as e:
            error = str(e)

    def make_main(page: ft.Page) -> None:
        main(page, summary, error)

    ft.app(target=make_main)
