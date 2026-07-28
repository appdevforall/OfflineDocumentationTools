#!/usr/bin/env python3
"""Checks for TEST_PLAN.md §6 (JsonRenderer traversal & index generation).

Run via tests/test_renderer.sh. Takes two directories as args: the rendered
JSON output dir, and the stock-Dokka-HTML baseline's top-level output dir
(examples/html-baseline/build/dokka/html). The latter nests package/class
pages under a "html-baseline/" (module name) subfolder but keeps the root
module's own index.html one level up, alongside navigation.html/styles/etc,
so both need scanning from this same parent directory.

Not covered here: "Multimodule index.json" (context.configuration.modules
non-empty) -- needs a real multi-module Dokka build, which this single-module
fixture can't provide. Deferred to TEST_PLAN.md §8's kotlin-stdlib stress test.
"""
import os
import sys

sys.path.insert(0, os.path.dirname(__file__))
from checklib import Checker, load


def collect_pages(root, ext, strip_prefix=None):
    pages = set()
    for dirpath, _, files in os.walk(root):
        for f in files:
            if f.endswith(ext):
                rel = os.path.relpath(os.path.join(dirpath, f), root)
                if strip_prefix and rel.startswith(strip_prefix + os.sep):
                    rel = rel[len(strip_prefix) + 1 :]
                pages.add(rel[: -len(ext)])
    return pages


def main():
    output_dir = sys.argv[1]
    html_baseline_dir = sys.argv[2]
    c = Checker()

    # --- package-list ---
    with open(os.path.join(output_dir, "package-list"), "r", encoding="utf-8") as f:
        lines = [line.rstrip("\n") for line in f]
    c.check(
        lines[:2] == ["$dokka.format:json-v1$", "$dokka.linkExtension:json$"],
        "package-list has the expected header lines",
        f"got {lines[:2]}",
    )
    packages = lines[2:]
    c.check(
        {"com.example.testlib", "com.example.utils"} <= set(packages),
        "package-list lists the real packages",
        f"got {packages}",
    )
    c.check(packages == sorted(packages), "package-list packages are sorted", f"got {packages}")

    # --- all-types.json ---
    all_types = load(os.path.join(output_dir, "all-types.json"))
    types_by_name = {t["name"]: t for t in all_types["types"]}
    expected_kinds = {
        "BoundedContainer": "class",
        "Provider": "interface",
        "Level3": "enum",
        "Level1": "object",
        "Meta": "annotation",
        "DataMap": "typeAlias",
    }
    for name, kind in expected_kinds.items():
        present = c.check(name in types_by_name, f"all-types.json includes {name}")
        if present:
            c.check(
                types_by_name[name]["kind"] == kind,
                f"{name}'s kind in all-types.json is '{kind}'",
                f"got {types_by_name[name]['kind']}",
            )
    names = [t["name"] for t in all_types["types"]]
    c.check(names == sorted(names), "all-types.json entries are sorted by name")

    # --- Breadcrumbs at the root and at max depth ---
    root_module = load(os.path.join(output_dir, "index.json"))
    c.check(
        len(root_module.get("breadcrumbs", [])) <= 1,
        "root module page has an empty or single-entry breadcrumb list",
        f"got {root_module.get('breadcrumbs')}",
    )

    level3 = load(os.path.join(output_dir, "com.example.testlib", "-level1", "-level2", "-level3", "index.json"))
    breadcrumb_names = [b["name"] for b in level3.get("breadcrumbs", [])]
    c.check(
        breadcrumb_names == ["testlib", "com.example.testlib", "Level1", "Level2", "Level3"],
        "max-depth page (Level1.Level2.Level3) has a 5-entry breadcrumb list in root-to-leaf order",
        f"got {breadcrumb_names}",
    )

    # --- File path parity with Dokka's own HTML layout ---
    json_pages = collect_pages(output_dir, ".json")
    html_pages = collect_pages(html_baseline_dir, ".html", strip_prefix="html-baseline")
    # Known, expected non-page artifacts unique to each renderer: "all-types" is
    # our own aggregate index (not a Documentable page), "navigation" is the
    # default HTML renderer's sidebar fragment, and everything else at the
    # baseline's top level (styles/, scripts/, images/, ui-kit/) is static
    # site-template assets our JSON renderer never writes at all.
    static_asset_prefixes = ("styles/", "scripts/", "images/", "ui-kit/")
    json_comparable = json_pages - {"all-types"}
    html_comparable = {
        p for p in html_pages if p != "navigation" and not p.startswith(static_asset_prefixes)
    }
    only_json = sorted(json_comparable - html_comparable)
    only_html = sorted(html_comparable - json_comparable)
    c.check(
        not only_json and not only_html,
        "every JSON page path (extension aside) matches a page Dokka's default HTML renderer produces",
        f"only in JSON: {only_json[:10]}, only in HTML: {only_html[:10]}",
    )

    return c.summarize(os.path.basename(__file__))


if __name__ == "__main__":
    sys.exit(main())
