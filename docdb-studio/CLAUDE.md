# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Commands

Dependencies are managed with `uv`. Always use `uv run` rather than activating a venv.

- Run the main browser app: `uv run python docdb_studio.py [database] --user <name>`
  - Database defaults to `documentation.db` in the cwd. `--user` is required (used to stamp the `LastChange` table on edits).
- Run the table-summary window: `uv run python db-summary.py <database>`
- Run all tests: `uv run pytest`
- Run a single test: `uv run pytest tests/test_uri_validity.py::test_uris_exist_in_content_strips_query_before_lookup`

## Architecture

### Big picture

This is a Flet (Flutter-based) GUI tool over a SQLite database (`documentation.db`, ~380 MB) that holds an IDE's tooltip / help-content data. There are two apps:

- `docdb_studio.py` — full CRUD over the `Tooltips` / `TooltipCategories` / `TooltipButtons` tables, with paginated browse, search, view, edit, single-insert, and CSV bulk-import flows. ~1600 lines, single file by design.
- `db-summary.py` — read-only window listing every user table and its row count.

### Schema is locked

Per `AGENTS.md`: **never change the schema.** The full schema is in `SCHEMA.md`. Key relationships the code depends on:

- `Tooltips.categoryId` → `TooltipCategories.id`
- `TooltipButtons.tooltipId` → `Tooltips.id`, `TooltipButtons.buttonNumberId` → `TooltipButtonNumbers.id` (button-number IDs are manually assigned to control display order)
- Tooltip uniqueness is `(categoryId, tag)`
- Button URIs are validated against `Content.path` after stripping `?query` and `#fragment` (see `_uri_path_for_content_lookup` / `uris_exist_in_content`)
- Every mutation should call `update_last_change(db, documentation_set, user)` so the `LastChange` table reflects who edited what and when. The `--user` CLI arg feeds this.

### Inside `docdb_studio.py`

The file is organized as a flat layer of DB helpers (top half, ~lines 1–490) followed by one large `main(page, ...)` function (~lines 490–1601) that owns all UI state via nested closures. There is no MVC split — the pattern is "one Flet page, multiple `show_*_page()` closures that clear `page.controls` and rebuild the view." When adding a new screen, follow that pattern and route back through an existing `show_browse_page` so the user lands back on the paginated list.

Display uses `flet_datatable2.DataTable2` for the main table; standard `ft.DataRow` cells inside it.

### Tests

Tests live in `tests/` and run against tempfile SQLite DBs. The script is importable as `import docdb_studio` thanks to `pythonpath = ["."]` under `[tool.pytest.ini_options]` in `pyproject.toml`.

## Conventions (from AGENTS.md)

- Python 3.10+, PEP 8, `snake_case` for funcs/vars, `PascalCase` for Flet component classes.
- Type hints everywhere (`ft.Page`, `sqlite3.Cursor`, etc.).
- All SQL must use parameterized queries — never f-string user input into SQL. The one exception in the codebase is `db-summary.py`'s `COUNT(*)` query, which quotes a table name retrieved from `sqlite_master`.
- Wrap each DB operation in `with sqlite3.connect(...) as conn:` and commit explicitly.
- Surface errors to the user via `ft.SnackBar` rather than letting exceptions propagate to the UI.

## Verifying UI changes

`pytest` passes do **not** prove a UI change works — the test suite does not exercise Flet. After any change touching `main()` / `show_*_page()` / dialogs / pickers, either:
- launch `uv run python docdb_studio.py --user test` and exercise the affected flow yourself, or
- explicitly tell the user "I cannot UI-test this from here, please verify before I declare done."

Never claim a UI fix is done on the strength of pytest alone.

## Library notes

- `pyproject.toml` pins `flet >= 0.27` but the actually installed version is **0.80**, which has API breaks. Before writing Flet code, run `uv run python -c "import flet; print(flet.__version__)"`.
- In Flet 0.80, `FilePicker` is a **Service** (not a Control) under `flet.controls.services.file_picker`. Construct it inside an event handler and call `await picker.save_file(...)` / `pick_files(...)` / `get_directory_path(...)` — it auto-registers via the Flet context. Do **not** add it to `page.overlay` (throws "Unknown control: FilePicker").
- Do **not** use `tkinter.filedialog` inside a Flet app. Tkinter's Tk mainloop and Flet's event loop can deadlock the process (picker never opens, Ctrl-C cannot kill the app). Also, an unparented Tk window has no z-order relationship to the Flutter main window on macOS *or* Windows, causing dialogs to render behind the app.
