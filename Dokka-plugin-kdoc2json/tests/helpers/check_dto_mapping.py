#!/usr/bin/env python3
"""Checks for TEST_PLAN.md §2 (DTO mapping correctness, ModelMapper.mapToDto).

Run via tests/test_dto_mapping.sh, which regenerates examples/example-data-processor
first. Takes the rendered JSON output directory as argv[1].
"""
import os
import sys

sys.path.insert(0, os.path.dirname(__file__))
from checklib import Checker, load


def path(output_dir, *parts):
    return os.path.join(output_dir, *parts)


def main():
    output_dir = sys.argv[1]
    c = Checker()

    # --- Module -> Package -> Class -> Function nesting ---
    module = load(path(output_dir, "index.json"))
    c.check(module.get("kind") == "module", "root index.json is a ModuleDto")
    package_names = {p.get("name") for p in module.get("packages", [])}
    c.check(
        {"com.example.testlib", "com.example.utils"} <= package_names,
        "root ModuleDto.packages lists both expected packages",
        f"got {package_names}",
    )

    testlib_pkg = load(path(output_dir, "com.example.testlib", "index.json"))
    c.check(testlib_pkg.get("kind") == "package", "package index.json is a PackageDto")
    function_names = {f["name"] for f in testlib_pkg.get("functions", [])}
    c.check("safeDivide" in function_names, "package lists its top-level functions", f"got {function_names}")
    typealias_names = {t["name"] for t in testlib_pkg.get("typeAliases", [])}
    c.check(typealias_names == {"DataMap"}, "package lists its typeAliases", f"got {typealias_names}")
    classlike_names = {cl["name"] for cl in testlib_pkg.get("classlikes", [])}
    c.check(
        {"Level1", "BoundedContainer", "Shape", "Counter", "Provider", "Meta"} <= classlike_names,
        "package lists its classlikes",
        f"got {classlike_names}",
    )

    # --- Class vs. Interface vs. Enum vs. Object vs. Annotation ---
    data_processor = load(path(output_dir, "com.example.utils", "-data-processor", "index.json"))
    c.check(data_processor.get("kind") == "class", "DataProcessor kind == class")
    c.check("entries" not in data_processor, "ClassDto has no entries key")

    provider = load(path(output_dir, "com.example.testlib", "-provider", "index.json"))
    c.check(provider.get("kind") == "interface", "Provider kind == interface")
    c.check("entries" not in provider, "InterfaceDto has no entries key")

    level1 = load(path(output_dir, "com.example.testlib", "-level1", "index.json"))
    c.check(level1.get("kind") == "object", "Level1 kind == object")
    c.check("entries" not in level1, "ObjectDto has no entries key")

    meta = load(path(output_dir, "com.example.testlib", "-meta", "index.json"))
    c.check(meta.get("kind") == "annotation", "Meta kind == annotation")
    c.check("entries" not in meta, "AnnotationDto has no entries key")

    level3 = load(path(output_dir, "com.example.testlib", "-level1", "-level2", "-level3", "index.json"))
    c.check(level3.get("kind") == "enum", "Level3 kind == enum")
    c.check(len(level3.get("entries", [])) > 0, "EnumDto.entries is non-empty")

    # --- EnumEntryDto ---
    entry_names = {e["name"] for e in level3.get("entries", [])}
    c.check(entry_names == {"ACTIVE", "INACTIVE"}, "Level3 has both enum entries", f"got {entry_names}")
    for entry in level3.get("entries", []):
        docs = entry.get("documentation", {}).get(":/main", [])
        has_text = any(tag.get("text") for tag in docs)
        c.check(has_text, f"enum entry {entry['name']} carries its own KDoc text")

    # --- Constructor mapping ---
    ctor = data_processor["constructors"][0]
    c.check(ctor["isConstructor"] is True, "constructor function has isConstructor == true")
    regular_fn = next(f for f in data_processor["functions"] if f["name"] == "processRecord")
    c.check(regular_fn["isConstructor"] is False, "regular function has isConstructor == false")

    # --- Property with getter/setter ---
    counter = load(path(output_dir, "com.example.testlib", "-counter", "index.json"))
    count_prop = next(p for p in counter["properties"] if p["name"] == "count")
    c.check(count_prop.get("getter") is not None, "var property (Counter.count) has a getter")
    c.check(count_prop.get("setter") is not None, "var property (Counter.count) has a setter")

    bounded = load(path(output_dir, "com.example.testlib", "-bounded-container", "index.json"))
    value_prop = next(p for p in bounded["properties"] if p["name"] == "value")
    c.check(value_prop.get("getter") is not None, "val property (BoundedContainer.value) has a getter")
    c.check(value_prop.get("setter") is None, "val property (BoundedContainer.value) has no setter")

    # --- TypeAliasDto ---
    data_map = load(path(output_dir, "com.example.testlib", "-data-map", "index.json"))
    c.check(data_map.get("kind") == "typeAlias", "DataMap kind == typeAlias")
    underlying = data_map["underlyingType"]["main"]
    c.check(
        underlying.get("kind") == "GenericTypeConstructor" and "Map" in underlying.get("dri", ""),
        "DataMap.underlyingType resolves to a GenericTypeConstructorDto for Map<String, Any>, not a raw string",
        f"got {underlying}",
    )

    # --- Nested classlikes: breadcrumbs in root-to-leaf order ---
    breadcrumb_names = [b["name"] for b in level3.get("breadcrumbs", [])]
    c.check(
        breadcrumb_names == ["testlib", "com.example.testlib", "Level1", "Level2", "Level3"],
        "Level1/Level2/Level3 breadcrumbs are in root-to-leaf order",
        f"got {breadcrumb_names}",
    )
    c.check(level3["breadcrumbs"][-1]["url"] == "index.json", "leaf breadcrumb url points at the page itself")

    # --- Shallow vs. deep recursion ---
    level1_shallow = next(cl for cl in testlib_pkg["classlikes"] if cl["name"] == "Level1")
    c.check(
        not level1_shallow.get("classlikes") and not level1_shallow.get("functions"),
        "Level1 listed shallowly inside the package (no nested members)",
        f"got classlikes={level1_shallow.get('classlikes')} functions={level1_shallow.get('functions')}",
    )
    c.check(
        any(cl["name"] == "Level2" for cl in level1.get("classlikes", [])),
        "Level1's own index.json has Level2 fully populated (deep)",
    )

    return c.summarize(os.path.basename(__file__))


if __name__ == "__main__":
    sys.exit(main())
