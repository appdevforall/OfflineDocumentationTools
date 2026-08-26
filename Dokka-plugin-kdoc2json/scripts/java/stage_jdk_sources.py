#!/usr/bin/env python3
"""Stages the JDK's *documented* sources for a Javadoc-mode Dokka run.

`src.zip` ships every source file in the JDK, including internal packages that the official API
docs deliberately leave out. javadoc's rule is exact and easy to reproduce: a package appears in
`api/` if and only if its module `exports` it *unqualified* (an `exports ... to ...` directive is
a targeted export and is not documented). Verified against the JDK 17 docs in
SourceDocs/JavaDocs: for java.base, the 53 unqualified exports are precisely the 53 documented
packages, with nothing on either side left over.

So this script copies, per module, only the source files of unqualified-exported packages, plus
that module's `module-info.java` (which the plugin reads back for the module page's requires /
uses / provides / exports sections). The result is a tree of one directory per module, each a
valid package root:

    <staging>/java.base/module-info.java
    <staging>/java.base/java/lang/Object.java
    <staging>/java.sql/java/sql/Connection.java

Everything left behind still resolves at analysis time from the JDK on the compile classpath, so
dropping it costs nothing but analysis time.
"""

from __future__ import annotations

import argparse
import re
import shutil
import sys
import zipfile
from pathlib import Path

# `exports <pkg>;` with no `to` clause. The `to` form is a qualified export -- visible only to the
# named modules, and never part of the published API docs.
UNQUALIFIED_EXPORT = re.compile(r"^\s*exports\s+([\w.]+)\s*;", re.MULTILINE)

# Dokka's {@inheritDoc} resolver recurses without bound on parts of the JDK
# (InheritDocTagResolver.resolveThrowsTag -> PsiElementToHtmlConverter.toInheritDocHtml) and brings
# the whole run down with a StackOverflowError -- reproducibly, on java.io and java.util among
# others. Rewriting the tag to an inert text marker before Dokka parses it avoids that; the plugin
# then resolves the marker itself, walking the same supertype chain javadoc walks. Nothing is lost:
# 3,214 occurrences across the JDK are still resolved, just by us instead of by Dokka.
INHERIT_DOC = re.compile(r"\{@inheritDoc\}")
INHERIT_DOC_MARKER = "ADFAINHERITDOC"  # must match JavadocMapper.INHERIT_DOC_MARKER

BLOCK_COMMENT = re.compile(r"/\*.*?\*/", re.DOTALL)
LINE_COMMENT = re.compile(r"//[^\n]*")
MODULE_DECL = re.compile(r"\bmodule\s+([\w.]+)\s*\{", re.MULTILINE)

# Modules the JDK's own docs build leaves out (make/Docs.gmk's MODULES_FILTER). They ship in
# src.zip but never appear in api/, so staging them would produce module pages the official docs
# don't have. Verified against the JDK 17 docs: excluding exactly these makes the staged module
# set identical to the documented one.
EXCLUDED_MODULE_PREFIXES = ("jdk.internal.",)
EXCLUDED_MODULES = frozenset({"jdk.unsupported", "jdk.unsupported.desktop", "jdk.random"})


def is_excluded(name: str) -> bool:
    return name in EXCLUDED_MODULES or name.startswith(EXCLUDED_MODULE_PREFIXES)


def strip_comments(source: str) -> str:
    """Removes comments so a commented-out directive is never mistaken for a live one."""
    return LINE_COMMENT.sub("", BLOCK_COMMENT.sub("", source))


def parse_module_info(path: Path) -> tuple[str, list[str]]:
    """Returns (module name, unqualified exported packages) for one module-info.java."""
    body = strip_comments(path.read_text(encoding="utf-8", errors="replace"))
    match = MODULE_DECL.search(body)
    name = match.group(1) if match else path.parent.name
    return name, sorted(set(UNQUALIFIED_EXPORT.findall(body)))


def extract_sources(src_zip: Path, destination: Path) -> None:
    if destination.exists() and any(destination.iterdir()):
        print(f"    reusing already-extracted sources at {destination}")
        return
    destination.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(src_zip) as archive:
        archive.extractall(destination)


def copy_source(source: Path, target: Path, rewrite_inherit_doc: bool) -> None:
    """Copies one .java file, optionally neutralising {@inheritDoc} on the way through."""
    if not rewrite_inherit_doc:
        shutil.copy2(source, target)
        return
    text = source.read_text(encoding="utf-8", errors="replace")
    rewritten = INHERIT_DOC.sub(INHERIT_DOC_MARKER, text)
    if rewritten == text:
        shutil.copy2(source, target)
    else:
        target.write_text(rewritten, encoding="utf-8")


def stage(
    extracted: Path,
    staging: Path,
    only: set[str] | None,
    excluded: set[str],
    rewrite_inherit_doc: bool = True,
) -> tuple[int, int, int]:
    if staging.exists():
        shutil.rmtree(staging)
    staging.mkdir(parents=True)

    modules = staged_packages = staged_files = 0
    skipped: list[str] = []

    for module_info in sorted(extracted.glob("*/module-info.java")):
        module_dir = module_info.parent
        name, exports = parse_module_info(module_info)
        if only and name not in only:
            continue
        if not only and (is_excluded(name) or name in excluded):
            skipped.append(name)
            continue

        target_module = staging / name
        target_module.mkdir(parents=True, exist_ok=True)
        shutil.copy2(module_info, target_module / "module-info.java")
        modules += 1

        for package in exports:
            source_package = module_dir / Path(*package.split("."))
            if not source_package.is_dir():
                # A package can be exported by one module but live in another's directory in
                # src.zip (or not ship sources at all); skip rather than fail the whole run.
                print(f"    warning: {name} exports {package}, but no sources found", file=sys.stderr)
                continue
            target_package = target_module / Path(*package.split("."))
            target_package.mkdir(parents=True, exist_ok=True)
            # Non-recursive on purpose: a Java package is exactly one directory, and a
            # subdirectory is a *different* package that must be exported in its own right.
            files = [f for f in source_package.iterdir() if f.suffix == ".java" and f.is_file()]
            for java_file in files:
                copy_source(java_file, target_package / java_file.name, rewrite_inherit_doc)
            staged_packages += 1
            staged_files += len(files)

    if skipped:
        print(f"    skipped {len(skipped)} undocumented module(s): {', '.join(sorted(skipped))}")
    return modules, staged_packages, staged_files


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("src_zip", type=Path, help="path to the JDK's lib/src.zip")
    parser.add_argument("staging", type=Path, help="directory to write the staged source tree to")
    parser.add_argument("--extract-to", type=Path, default=None,
                        help="where to unpack src.zip (default: <staging>/../src-extracted)")
    parser.add_argument("--modules", default=None,
                        help="comma-separated module names to stage; overrides the exclusion "
                             "list, so naming an excluded module stages it (default: all "
                             "documented modules)")
    parser.add_argument("--keep-inherit-doc", action="store_true",
                        help="leave {@inheritDoc} tags as they are. Dokka's own resolver crashes "
                             "with a StackOverflowError on much of the JDK, so this is expected to "
                             "fail; it exists to re-check whether a newer Dokka has fixed the bug")
    parser.add_argument("--exclude-modules", default="",
                        help="extra comma-separated module names to leave out, on top of the "
                             "ones the JDK's own docs build filters")
    args = parser.parse_args()

    if not args.src_zip.is_file():
        print(f"Error: {args.src_zip} not found", file=sys.stderr)
        return 1

    extracted = args.extract_to or args.staging.parent / "src-extracted"
    only = {m.strip() for m in args.modules.split(",")} if args.modules else None
    excluded = {m.strip() for m in args.exclude_modules.split(",") if m.strip()}

    print(f"==> Extracting {args.src_zip}")
    extract_sources(args.src_zip, extracted)

    print(f"==> Staging exported packages into {args.staging}")
    modules, packages, files = stage(
        extracted, args.staging, only, excluded, rewrite_inherit_doc=not args.keep_inherit_doc
    )

    if modules == 0:
        print("Error: no modules staged -- is this a modular JDK's src.zip?", file=sys.stderr)
        return 1

    print(f"    {modules} modules, {packages} exported packages, {files} source files")
    if not args.keep_inherit_doc:
        print("    {@inheritDoc} rewritten to an inert marker; the plugin resolves it (see the "
              "comment on INHERIT_DOC above)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
