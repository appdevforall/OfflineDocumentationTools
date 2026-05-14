# Retrospectives

## 2026-05-14 — Fix file picker rendering behind Flet window (Add → Import content folder)

### Time Breakdown
| Started | Phase | 👤 Hands-On | 🤖 Agent | Problems |
|---------|-------|-------------|----------|----------|
| 14:10 PT | Plan + option 0 (Tk helper with `update()`) | ██ 4m | █████ 13m | ⚠ Tk+Flet deadlock; picker never opened, Ctrl-C jammed Flet |
| 14:30 PT | Option 1 (minimal `-topmost`) | █ 1m | █ 1m | ⚠ still appeared behind |
| 14:32 PT | Option 2 v1 (FilePicker in `page.overlay`) | █ 2m | ██ 4m | ⚠ "Unknown control: FilePicker" |
| 14:37 PT | Option 2 v2 (FilePicker as Service) | █ 3m | ███ 7m | ✅ works |
| 14:44 PT | Windows/Linux Q&A + commit/push | █████ 12m | ██ 4m | minor bash heredoc quote issue |

### Metrics
| Metric | Duration |
|--------|----------|
| Total wall-clock | 42 min |
| Hands-on (adjusted) | 22 min (52%) |
| Agent time | ~29 min |
| Estimated cost | $32.83 |
| Retro analysis time | ~10 min |

### Key Observations
- Three of four implementation attempts failed because the original plan was built against `pyproject.toml`'s `flet >= 0.27` floor without checking the installed version (0.80). Flet 0.80 changed `FilePicker` from a sync Control to an async Service. Checking installed version up-front would have skipped options 0, 1, and 2-v1.
- Option 0 shipped after pytest passed without me running the app. The `update()` call deadlocked the Flet runtime. The cost of "the picker never opened AND Ctrl-C was stuck" landed on the user.
- The "Unknown control: FilePicker" error in option 2-v1 was avoidable — the module path `flet.controls.services.file_picker` was visible in the first `help()` output I ran.
- I mis-framed the bug as macOS-specific; the user corrected me — the original report came from a Windows user. The root cause is platform-agnostic.
- Good thing: every failure had a clean rollback, so each next attempt started from a known state.

### Feedback
**What worked:** Having Option 2 (`ft.FilePicker` migration) staged as a fallback in the original plan paid off — when option 1's minimal `-topmost` tweak didn't work, we had an alternative ready to go without re-planning.

**What didn't:** Multiple "claimed done" moments turned out to be broken. User feedback: "Next time do more testing and confirmation before claiming success."

### Actions Taken
| Issue | Action Type | Change |
|-------|-------------|--------|
| Declared UI fix done based on pytest only | CLAUDE.md | Added "Verifying UI changes" section — either smoke-test or explicitly say "I cannot UI-test" |
| Wrong Flet API assumed from pyproject floor | CLAUDE.md | Added "Library notes" section flagging Flet 0.80 installed, `FilePicker` as Service, and tkinter+Flet deadlock |
| General behavior change | Memory (feedback) | `feedback_ui_verification.md` — verify UI work before declaring done |
| Project-specific facts | Memory (project) | `project_docdb_flet_version.md` — docdb runs on Flet 0.80; file pickers migrated to async `ft.FilePicker` |
| Recurring gotchas | Doc | Created `docs/process/learnings.md` with Flet 0.80 API, tkinter+Flet, verification habits |
