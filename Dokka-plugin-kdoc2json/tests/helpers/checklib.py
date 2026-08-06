"""Shared assertion plumbing for the tests/helpers/check_*.py scripts.

These scripts perform the structural JSON assertions for TEST_PLAN.md's §2-§4
(DTO/type/doc-tag mapping) -- checks that need real JSON parsing rather than
grep, unlike the config-option tests in tests/test_*.sh. Each check_*.py is
invoked by its matching tests/test_*.sh wrapper (which runs run_dokka first),
takes the rendered output directory as argv[1], and exits 0/1 like a normal
test binary.
"""
import json


class Checker:
    def __init__(self):
        self.passed = 0
        self.failed = 0

    def check(self, condition, desc, detail=""):
        if condition:
            print(f"  PASS: {desc}")
            self.passed += 1
        else:
            suffix = f" ({detail})" if detail else ""
            print(f"  FAIL: {desc}{suffix}")
            self.failed += 1
        return condition

    def summarize(self, name):
        print()
        print(f"{name}: {self.passed} passed, {self.failed} failed")
        return 0 if self.failed == 0 else 1


def load(path):
    with open(path, "r", encoding="utf-8") as f:
        return json.load(f)
