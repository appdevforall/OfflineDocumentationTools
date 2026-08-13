"""Regression test for find_missing_assets.py's exit-code behavior (PR #24
review). Runs the script as a subprocess against the real md_to_json.py
(merged from ADFA-5039); a non-UTF-8 .md file gives convert_file a genuine
reason to raise, matching the pattern md_to_json.py's own test suite uses
for its equivalent main()-exit-code tests.
"""
import subprocess
import sys
from pathlib import Path

import find_missing_assets as fma


def _write_minimal_docs_root(tmp_path, *, with_failure=False):
    docs_root = tmp_path / "docs"
    (docs_root / "topics").mkdir(parents=True)
    (docs_root / "topics" / "good.md").write_text("# Good\n\nHello.\n", encoding="utf-8")
    if with_failure:
        # Not valid UTF-8 - convert_file's read_text(encoding="utf-8") raises.
        (docs_root / "topics" / "bad.md").write_bytes(b"\xff\xfe not utf-8")
    return docs_root


def _run(*args):
    script = Path(__file__).resolve().parent.parent / "find_missing_assets.py"
    return subprocess.run([sys.executable, str(script), *map(str, args)], capture_output=True, text=True)


def test_exits_zero_and_reports_zero_failures_when_nothing_fails(tmp_path):
    docs_root = _write_minimal_docs_root(tmp_path)
    report = tmp_path / "report.md"
    result = _run(docs_root, report)
    assert result.returncode == 0
    assert "0 file(s) failed to scan" in report.read_text(encoding="utf-8")


def test_exits_nonzero_when_a_file_fails_to_scan(tmp_path):
    """A per-file scan failure used to be printed to stderr and otherwise
    ignored - the report still claimed a clean summary and the process
    still exited 0, so a totally broken corpus was indistinguishable from a
    clean one (this is the pre-flight gate run before populate_db.py). The
    bad file is scanned by two independent passes (the main conversion loop
    and find_include_warnings' own <include> scan), so it counts twice."""
    docs_root = _write_minimal_docs_root(tmp_path, with_failure=True)
    report = tmp_path / "report.md"
    result = _run(docs_root, report)
    assert result.returncode == 1
    text = report.read_text(encoding="utf-8")
    assert "2 file(s) failed to scan" in text
    assert "incomplete" in text.lower()


def test_allow_failures_exits_zero_despite_failure(tmp_path):
    docs_root = _write_minimal_docs_root(tmp_path, with_failure=True)
    report = tmp_path / "report.md"
    result = _run(docs_root, report, "--allow-failures")
    assert result.returncode == 0


def test_find_include_warnings_reports_failure_instead_of_raising(tmp_path):
    """find_include_warnings had its own unguarded read_text(encoding="utf-8")
    outside the main loop's try/except - a non-UTF-8 file crashed the whole
    process with an uncaught traceback, bypassing --allow-failures entirely
    rather than being counted as a scan failure like every other file-read
    in this script."""
    topics_dir = tmp_path / "topics"
    topics_dir.mkdir()
    (topics_dir / "good.md").write_text("no includes here\n", encoding="utf-8")
    (topics_dir / "bad.md").write_bytes(b"\xff\xfe not utf-8")

    warnings, failed = fma.find_include_warnings(topics_dir)
    assert warnings == []
    assert failed == 1
