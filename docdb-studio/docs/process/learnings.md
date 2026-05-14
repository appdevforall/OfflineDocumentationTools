# Learnings

Recurring gotchas, surprises, and patterns that future work in this repo should benefit from.

## Flet 0.80 API

- `FilePicker` is a `Service`, not a `Control`. It lives at `flet.controls.services.file_picker`. Construct inside an async event handler — the base class auto-registers via `context.page._services` and the runtime sweeps unreferenced services after each event. Do **not** append to `page.overlay`; that throws "Unknown control: FilePicker".
- All `FilePicker` methods are async: `await picker.save_file(...)`, `await picker.pick_files(...)`, `await picker.get_directory_path(...)`.
- `pyproject.toml` floors (`flet >= 0.27`) say very little about the installed version. The two have diverged a lot — Flet went from sync Controls to async Services. Check `uv run python -c "import flet; print(flet.__version__)"` before planning Flet changes.

## tkinter + Flet

- Don't mix `tkinter.filedialog` with a Flet app. Two failure modes:
  1. **Deadlock**: calling `root.update()` on a withdrawn topmost Tk root while Flet's event loop is running can hang the whole process — picker never opens, Ctrl-C can't kill the app.
  2. **Z-order on every desktop OS**: an unparented Tk root has no parent relationship to the Flutter main window. Native dialogs (NSOpenPanel on macOS, IFileDialog on Windows) then have no z-order hint and frequently render *behind* the Flet window. The bug is cross-platform, not macOS-specific.

## Verification habits

- `pytest` covers DB/import logic in this repo, but not UI. After a UI-touching change, exercise the flow in `uv run python docdb_studio.py --user test` — or explicitly say "I cannot UI-test, please verify."
