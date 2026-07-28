#!/usr/bin/env python3
"""Checks for TEST_PLAN.md §3 (Type/Bound/Projection mapping).

Run via tests/test_type_mapping.sh, which regenerates examples/example-data-processor
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
    testlib = path(output_dir, "com.example.testlib")

    # --- Nullable wrapping ---
    format_or_default = load(os.path.join(testlib, "format-or-default.json"))
    param_type = format_or_default["parameters"][0]["type"]
    c.check(param_type.get("kind") == "Nullable", "String? maps to NullableDto, not a flag on the inner bound")
    inner = param_type.get("inner", {})
    c.check(
        inner.get("kind") == "GenericTypeConstructor" and inner.get("dri", "").startswith("kotlin/String"),
        "NullableDto.inner is the underlying String type",
        f"got {inner}",
    )

    # --- Generic type with projections ---
    copy_items = load(os.path.join(testlib, "copy-items.json"))
    source_type = copy_items["parameters"][0]["type"]
    c.check(
        source_type.get("kind") == "GenericTypeConstructor" and source_type.get("dri", "").startswith("kotlin.collections/List"),
        "List<out Number> maps to a GenericTypeConstructorDto for List",
    )
    c.check(len(source_type.get("projections", [])) == 1, "List<out Number> has exactly one projection")

    # --- Variance ---
    c.check(source_type["projections"][0]["kind"] == "Covariance", "'out Number' parameter maps to CovarianceDto")
    dest_type = copy_items["parameters"][1]["type"]
    c.check(dest_type["projections"][0]["kind"] == "Contravariance", "'in Number' parameter maps to ContravarianceDto")

    bounded = load(os.path.join(testlib, "-bounded-container", "index.json"))
    type_param = bounded["generics"][0]
    c.check(
        type_param["variantTypeParameter"]["kind"] == "Invariance",
        "an unannotated type parameter (T) maps to InvarianceDto by default",
    )

    # --- Functional types ---
    apply_callbacks = load(os.path.join(testlib, "apply-callbacks.json"))
    params_by_name = {p["name"]: p["type"] for p in apply_callbacks["parameters"]}

    plain = params_by_name["plain"]
    c.check(plain["kind"] == "FunctionalTypeConstructor", "plain lambda maps to FunctionalTypeConstructorDto")
    c.check(plain["isExtensionFunction"] is False and plain["isSuspendable"] is False, "plain lambda: not extension, not suspend")

    extension = params_by_name["extension"]
    c.check(extension["isExtensionFunction"] is True and extension["isSuspendable"] is False, "extension lambda: isExtensionFunction true")

    suspending = params_by_name["suspending"]
    c.check(suspending["isExtensionFunction"] is False and suspending["isSuspendable"] is True, "suspend lambda: isSuspendable true")

    # --- Java interop ---
    current_java_date = load(os.path.join(testlib, "current-java-date.json"))
    java_type = current_java_date["type"]
    c.check(
        java_type.get("kind") in ("GenericTypeConstructor", "JavaObject", "PrimitiveJavaType"),
        "java.util.Date maps to one of the Java-interop BoundDto kinds",
        f"got kind={java_type.get('kind')}",
    )
    c.check(java_type.get("url"), "java.util.Date's type has a resolvable url (external doc link configured)")
    c.check("java.util/Date" in java_type.get("dri", ""), "java.util.Date's dri points at the real JDK class")

    # --- Type parameter bounds ---
    bound = type_param["bounds"][0]
    c.check(
        bound.get("kind") == "GenericTypeConstructor" and bound.get("dri", "").startswith("kotlin/Comparable"),
        "T : Comparable<T> emits the bound's own DRI, not just the parameter name",
        f"got {bound}",
    )
    c.check(bound.get("url"), "the bound (Comparable) has a resolvable url")

    return c.summarize(os.path.basename(__file__))


if __name__ == "__main__":
    sys.exit(main())
