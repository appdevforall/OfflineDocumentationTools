#!/usr/bin/env python3
"""Binary-search bisection to find the minimal set of .java files in a java.base package
that trigger Dokka's StackOverflowError (github.com/Kotlin/dokka/issues/2171 - an
unresolvable {@inheritDoc}/related tag during Java analysis).

IMPORTANT: uses TRUE inclusion via a staging directory of symlinks passed as the sole
sourceRoots entry (-PabsSourceRoot). An earlier version of this script used the
`suppressedFiles` Dokka option to exclude candidates instead, which turned out to be a
dead end: suppressedFiles only filters final output pages - the analysis phase where the
crash happens still processes every file under sourceRoots regardless of it. Confirmed by
direct experiment: suppressing 268/269 files still crashed identically, but truly
restricting sourceRoots to that same 1 file (via a symlink staging dir) built successfully.

Usage: python3 bisect_inheritdoc.py <package, e.g. java/lang> [java/io ...]
"""
import glob
import os
import shutil
import subprocess
import sys
import tempfile

PROJECT_DIR = os.path.dirname(os.path.abspath(__file__))
JDK_BASE_CLASSES = os.path.abspath(
    os.path.join(PROJECT_DIR, "..", "jdk17", "src", "java.base", "share", "classes")
)
GRADLEW = os.path.join(PROJECT_DIR, "gradlew")


def find_jdk17_home():
    """The Kotlin Gradle plugin kdoc-to-json depends on doesn't support this machine's
    default JDK as a compile target if it's newer than 17, so JAVA_HOME must be pinned to
    an actual JDK 17 install. Set JDK17_HOME to override; otherwise this tries macOS's
    `java_home` locator (install one with e.g. `brew install openjdk@17` if it fails)."""
    override = os.environ.get("JDK17_HOME")
    if override:
        return override
    try:
        result = subprocess.run(
            ["/usr/libexec/java_home", "-v", "17"],
            capture_output=True,
            text=True,
            check=True,
        )
        return result.stdout.strip()
    except Exception as e:
        raise RuntimeError(
            "Could not locate a JDK 17 installation via /usr/libexec/java_home. Install "
            "one (e.g. `brew install openjdk@17`) or set the JDK17_HOME environment "
            "variable to its home directory."
        ) from e


ENV = dict(os.environ)
ENV["JAVA_HOME"] = find_jdk17_home()
ENV["JAVA_TOOL_OPTIONS"] = "-Xss256m"

test_count = 0

# Files already confirmed (via prior bisection runs on java/lang, java/io, java/util in
# isolation) to trigger github.com/Kotlin/dokka/issues/2171 - excluded up front so a
# broader run (e.g. the whole "java" top-level dir, all 9 subpackages together) looks for
# NEW combos instead of rediscovering these.
KNOWN_BAD = {
    os.path.join(JDK_BASE_CLASSES, rel)
    for rel in [
        "java/lang/reflect/Constructor.java",
        "java/lang/reflect/Executable.java",
        "java/lang/reflect/AccessibleObject.java",
        "java/lang/reflect/Field.java",
        "java/io/BufferedReader.java",
        "java/io/LineNumberReader.java",
        "java/util/AbstractList.java",
        "java/util/AbstractSequentialList.java",
        "java/util/NavigableMap.java",
        "java/util/TreeMap.java",
        "java/util/concurrent/ConcurrentNavigableMap.java",
        "java/util/concurrent/ConcurrentSkipListMap.java",
        "java/util/concurrent/ScheduledThreadPoolExecutor.java",
        "java/util/concurrent/ThreadPoolExecutor.java",
    ]
}


def all_java_files(pkg):
    files = sorted(
        glob.glob(os.path.join(JDK_BASE_CLASSES, pkg, "**", "*.java"), recursive=True)
    )
    return [f for f in files if f not in KNOWN_BAD]


def make_staging_dir(included):
    staging = tempfile.mkdtemp(prefix="dokka-stage-", dir="/tmp")
    for f in included:
        rel = os.path.relpath(f, JDK_BASE_CLASSES)
        dest = os.path.join(staging, rel)
        os.makedirs(os.path.dirname(dest), exist_ok=True)
        os.symlink(f, dest)
    return staging


def test_fails(included):
    """True if this exact set of files, as the ONLY sourceRoots content, reproduces the
    StackOverflowError. False if it builds successfully. Raises on anything else."""
    global test_count
    test_count += 1
    staging = make_staging_dir(included)
    try:
        proc = subprocess.run(
            [GRADLEW, "dokkaGenerateHtml", f"-PabsSourceRoot={staging}"],
            cwd=PROJECT_DIR,
            env=ENV,
            capture_output=True,
            text=True,
            timeout=300,
        )
    finally:
        shutil.rmtree(staging, ignore_errors=True)

    out = proc.stdout + proc.stderr
    names = ", ".join(os.path.basename(f) for f in included) if len(included) <= 8 else ""
    print(f"  [test {test_count}] {len(included)} files {names} -> ", end="", flush=True)
    if "BUILD SUCCESSFUL" in out:
        print("PASS")
        return False
    if "StackOverflowError" in out:
        print("FAIL (StackOverflowError)")
        return True
    print("UNEXPECTED RESULT")
    print(out[-4000:])
    raise RuntimeError(f"Unexpected gradle result for {len(included)} files")


def shrink_with_anchor(pool, anchor):
    """pool + anchor is known to fail. Binary-search pool (keeping the full anchor fixed
    and present in every test) down to a minimal subset that, combined with anchor, still
    fails. Scales as O(log n) instead of the combinatorial blowup of trying every subset."""
    if len(pool) <= 1:
        return pool
    mid = len(pool) // 2
    left, right = pool[:mid], pool[mid:]
    if test_fails(left + anchor):
        return shrink_with_anchor(left, anchor)
    if test_fails(right + anchor):
        return shrink_with_anchor(right, anchor)
    return pool  # both halves need each other too - can't shrink further this way


def find_minimal_combo(left, right):
    """left+right is confirmed to fail, but neither alone does - the crash needs files
    from both sides. Alternately shrink each side while anchoring the other, converging
    on a small (not always provably minimal, but tight) cross-cutting combo."""
    print(f"  -- {len(left) + len(right)} files fail together but neither half alone does; "
          f"anchored-shrinking to find the cross-cutting combo")
    min_left = shrink_with_anchor(left, right)
    min_right = shrink_with_anchor(right, min_left)
    # One more pass: min_right may be small enough now to shrink min_left further.
    min_left = shrink_with_anchor(min_left, min_right)
    combo = min_left + min_right
    assert test_fails(combo), "combo lost the failure during minimization - bug in shrink logic"
    return combo


def find_bad(candidates):
    if not candidates:
        return []
    if not test_fails(candidates):
        return []
    if len(candidates) == 1:
        print(f"  >>> BAD FILE: {candidates[0]}")
        return candidates
    mid = len(candidates) // 2
    left, right = candidates[:mid], candidates[mid:]
    bad_left = find_bad(left)
    bad_right = find_bad(right)
    if bad_left or bad_right:
        return bad_left + bad_right
    combo = find_minimal_combo(left, right)
    print(f"  >>> MINIMAL FAILING COMBO ({len(combo)} files): {[os.path.basename(f) for f in combo]}")
    # BUG FIX: finding one combo does NOT prove the rest of this branch is clean - a
    # second, independent problem could be hiding in the same left+right split. Recurse
    # into what's left after removing the combo to be sure. (This was missing in the
    # first few runs, which is why re-testing "java" with all found files excluded still
    # crashed - at least one more combo was hiding, unverified, in an already-resolved
    # branch.)
    combo_set = set(combo)
    remaining = [f for f in candidates if f not in combo_set]
    more_bad = find_bad(remaining)
    return combo + more_bad


def main():
    packages = sys.argv[1:]
    if not packages:
        print(__doc__)
        sys.exit(1)

    results = {}
    for pkg in packages:
        print(f"=== Bisecting {pkg} ===", flush=True)
        files = all_java_files(pkg)
        print(f"  {len(files)} total .java files under {pkg}")
        bad = find_bad(files)
        results[pkg] = bad
        print(f"  {pkg}: found {len(bad)} minimal-failing file(s) after {test_count} tests total so far")

    print("\n=== SUMMARY ===")
    for pkg, bad in results.items():
        print(f"{pkg}: {len(bad)} file(s) in minimal failing set")
        for f in bad:
            print(f"  - {f}")


if __name__ == "__main__":
    main()
