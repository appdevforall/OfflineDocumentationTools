"""Regression test for find_missing_assets.py's exit-code behavior (PR #24
review). find_missing_assets.py imports md_to_json.py, which doesn't exist
on this branch yet (it lands with ADFA-5039) - so this runs the script as a
subprocess against a minimal stand-in md_to_json module on PYTHONPATH
instead of importing the real one.
"""
import os
import subprocess
import sys
from pathlib import Path

STUB_MD_TO_JSON = '''
class Converter:
    def __init__(self, md, variables, topic_index=None, image_index=None):
        self.warnings = []

    def convert_file(self, path, page_id, source_rel):
        if path.read_text(encoding="utf-8").strip() == "FAIL":
            raise ValueError(f"stub failure for {path}")
        return {"id": page_id, "sourceFile": source_rel, "blocks": []}


def build_topic_index(topics_dir):
    return {}


def build_image_index(images_dir):
    return {}, []


def load_variables(docs_root):
    return {}


def make_markdown_it():
    return None
'''


def _write_stub_md_to_json(tmp_path):
    stub_dir = tmp_path / "stub"
    stub_dir.mkdir()
    (stub_dir / "md_to_json.py").write_text(STUB_MD_TO_JSON, encoding="utf-8")
    return stub_dir


def _write_minimal_docs_root(tmp_path, *, with_failure=False):
    docs_root = tmp_path / "docs"
    (docs_root / "topics").mkdir(parents=True)
    (docs_root / "topics" / "good.md").write_text("# Good\n\nHello.\n", encoding="utf-8")
    if with_failure:
        (docs_root / "topics" / "bad.md").write_text("FAIL", encoding="utf-8")
    return docs_root


def _run(stub_dir, *args):
    script = Path(__file__).resolve().parent.parent / "find_missing_assets.py"
    env = dict(os.environ)
    existing = env.get("PYTHONPATH")
    env["PYTHONPATH"] = f"{stub_dir}{os.pathsep}{existing}" if existing else str(stub_dir)
    return subprocess.run([sys.executable, str(script), *map(str, args)], capture_output=True, text=True, env=env)


def test_exits_zero_and_reports_zero_failures_when_nothing_fails(tmp_path):
    stub_dir = _write_stub_md_to_json(tmp_path)
    docs_root = _write_minimal_docs_root(tmp_path)
    report = tmp_path / "report.md"
    result = _run(stub_dir, docs_root, report)
    assert result.returncode == 0
    assert "0 file(s) failed to scan" in report.read_text(encoding="utf-8")


def test_exits_nonzero_when_a_file_fails_to_scan(tmp_path):
    """A per-file scan failure used to be printed to stderr and otherwise
    ignored - the report still claimed a clean summary and the process
    still exited 0, so a totally broken corpus was indistinguishable from a
    clean one (this is the pre-flight gate run before populate_db.py)."""
    stub_dir = _write_stub_md_to_json(tmp_path)
    docs_root = _write_minimal_docs_root(tmp_path, with_failure=True)
    report = tmp_path / "report.md"
    result = _run(stub_dir, docs_root, report)
    assert result.returncode == 1
    text = report.read_text(encoding="utf-8")
    assert "1 file(s) failed to scan" in text
    assert "incomplete" in text.lower()


def test_allow_failures_exits_zero_despite_failure(tmp_path):
    stub_dir = _write_stub_md_to_json(tmp_path)
    docs_root = _write_minimal_docs_root(tmp_path, with_failure=True)
    report = tmp_path / "report.md"
    result = _run(stub_dir, docs_root, report, "--allow-failures")
    assert result.returncode == 0
