#!/usr/bin/env python3
# /// script
# requires-python = ">=3.9"
# dependencies = [
#     "brotli",
#     "Pillow",
#     "pytest",
# ]
# ///
"""
test_trim_kotlin_json.py

Covers trim_kotlin_json.py: that each of the five changes takes exactly what it
is meant to from small Dokka-shaped pages and keeps what layout.pebble reads,
that a row is rewritten in the form it was stored in and compressed with the
database's own dictionary, that the template is found by name, that rows it
does not change keep their stored bytes, that a run is all or nothing, and that
each guard stops what it guards against.

Stored rows are compressed at a quality the tool never uses (5), so a row it
re-compressed cannot pass for one it left alone. The database tests use a tiny
template and point TEMPLATE_CODE_SHA256 at it for the length of the run.

Run it either way - as a plain script:

    uv run scripts/test_trim_kotlin_json.py

or under pytest:

    uv run --with brotli --with Pillow --with pytest pytest scripts/test_trim_kotlin_json.py

The database tests need the `brotli` CLI on PATH, as the tool itself does.
"""
from __future__ import annotations

import contextlib
import copy
import io
import json
import re
import shutil
import sqlite3
import subprocess
import sys
import tempfile
import traceback
from pathlib import Path

import brotli

sys.path.insert(0, str(Path(__file__).resolve().parent))

import trim_kotlin_json as tool  # noqa: E402

CHUNK_SIZE = 1024 * 1024
ALL = list(tool.CHANGES)

TINY_TEMPLATE = (b"<title>{{ name }}</title>\n{%- for m in functions %}\n<a href=\"{{ m.url }}\">{{ m.name }}</a>\n"
                 b"{%- endfor %}\n")

INT = {"kind": "GenericTypeConstructor", "dri": "kotlin/Int///PointingToDeclaration/", "url": "../-int/index.html"}
T_PARAM = {"kind": "TypeParameter", "dri": "kotlin//plus/#T/PointingToDeclaration/", "name": "T",
           "url": "plus.html"}
SINCE = {"dri": "kotlin/SinceKotlin///PointingToDeclaration/", "url": "../-since-kotlin/index.html",
         "params": {"version": "\"1.0\""}}

# A function page: everything a signature can carry, plus what Dokka adds that the template never reads.
FUNCTION_PAGE = {
    "name": "plus",
    "kind": "function",
    "dri": "kotlin/Int/plus/#kotlin.Int/PointingToDeclaration/",
    "url": "plus.html",
    "sourceSets": [":kotlin-stdlib/common", ":kotlin-stdlib/jvm"],
    "breadcrumbs": [{"name": "kotlin-stdlib", "url": "../../index.html"}, {"name": "kotlin", "url": "../index.html"},
                    {"name": "plus", "url": "plus.html"}],
    "documentation": {
        ":kotlin-stdlib/common": [{"type": "Description", "text": "<p>Adds.</p><p>Overflows.</p>"},
                                  {"type": "CustomTagWrapper", "name": "Since Kotlin", "text": "<p>1.0</p>"},
                                  {"type": "Sample", "name": "samples.Ints.plus"}],
        ":kotlin-stdlib/jvm": [{"type": "Description", "text": "<p>Adds, on the JVM.</p>"}]},
    "visibility": {"common": "public", "jvm": "public"},
    "modifier": {":kotlin-stdlib/common": "open", ":kotlin-stdlib/jvm": "final"},
    "extras": {"annotations": {":kotlin-stdlib/common": [SINCE], ":kotlin-stdlib/jvm": [SINCE, SINCE]},
               "additionalModifiers": {":kotlin-stdlib/common": ["operator"], ":kotlin-stdlib/jvm": ["inline"]},
               "isException": False},
    "generics": [{"name": "T", "dri": "kotlin//plus/#T/PointingToDeclaration/", "url": "plus.html",
                  "sourceSets": [":kotlin-stdlib/common"], "bounds": [INT],
                  "variantTypeParameter": {"kind": "Invariance"}}],
    "receiver": {"dri": "kotlin//plus/#kotlin.Int/PointingToDeclaration/", "url": "plus.html",
                 "sourceSets": [":kotlin-stdlib/common"], "type": INT},
    "parameters": [
        {"name": "other", "dri": "kotlin//plus/#kotlin.Int/PointingToCallableParameters(0)/", "url": "plus.html",
         "sourceSets": [":kotlin-stdlib/common"],
         "documentation": {":kotlin-stdlib/common": [{"type": "Description", "text": "<p>The other.</p>"}]},
         "type": {"kind": "Nullable", "inner": T_PARAM},
         "extras": {"defaultValues": {":kotlin-stdlib/common": "0", ":kotlin-stdlib/jvm": "1"},
                    "additionalModifiers": {":kotlin-stdlib/common": ["vararg"]}}},
        {"name": "block", "dri": "kotlin//plus/#kotlin.Int/PointingToCallableParameters(1)/", "url": "plus.html",
         "sourceSets": [":kotlin-stdlib/common"],
         "type": {"kind": "FunctionalTypeConstructor", "dri": "kotlin/Function1///PointingToDeclaration/",
                  "url": "../-function1/index.html", "isSuspendable": True, "isExtensionFunction": False,
                  "projections": [{"kind": "Covariance", "inner": INT}, {"kind": "Star"}]},
         "extras": {"additionalModifiers": {":kotlin-stdlib/common": ["crossinline"]}}}],
    "type": {"kind": "TypeAliased", "typeAlias": {"kind": "GenericTypeConstructor", "dri": "kotlin/Alias///",
                                                  "url": "alias.html", "presentableName": "result"},
             "inner": INT},
    "isConstructor": False,
    "isExpectActual": True,
    "expectPresentInSet": ":kotlin-stdlib/common",
    "getter": {"name": "get", "dri": "kotlin/get///", "documentation": {":kotlin-stdlib/common": []}},
}

FUNCTION_PAGE_READ = {
    "name": "plus",
    "kind": "function",
    "sourceSets": [":kotlin-stdlib/common", ":kotlin-stdlib/jvm"],
    "breadcrumbs": FUNCTION_PAGE["breadcrumbs"],
    "documentation": FUNCTION_PAGE["documentation"],
    "modifier": FUNCTION_PAGE["modifier"],
    "extras": {"annotations": {":kotlin-stdlib/common": [{"dri": SINCE["dri"], "url": SINCE["url"]}],
                               ":kotlin-stdlib/jvm": [{"dri": SINCE["dri"], "url": SINCE["url"]}] * 2},
               "additionalModifiers": FUNCTION_PAGE["extras"]["additionalModifiers"]},
    "generics": [{"name": "T"}],
    "receiver": {"type": {"kind": "GenericTypeConstructor", "url": "../-int/index.html",
                          "dri": "kotlin/Int///PointingToDeclaration/"}},
    "parameters": [
        {"name": "other", "type": {"kind": "Nullable", "inner": {"kind": "TypeParameter", "name": "T",
                                                                 "url": "plus.html"}},
         "extras": {"defaultValues": {":kotlin-stdlib/common": "0", ":kotlin-stdlib/jvm": "1"}}},
        {"name": "block", "type": {"kind": "FunctionalTypeConstructor", "isSuspendable": True,
                                   "isExtensionFunction": False,
                                   "projections": [{"kind": "Covariance", "inner": INT}, {"kind": "Star"}]}}],
    "type": {"kind": "TypeAliased", "typeAlias": {"kind": "GenericTypeConstructor", "dri": "kotlin/Alias///",
                                                  "url": "alias.html", "presentableName": "result"}},
}


def _member(name, **fields):
    member = {"name": name, "kind": "function", "dri": f"kotlin/Short/{name}///", "url": f"{name}.html",
              "sourceSets": [":kotlin-stdlib/common"], "visibility": {"common": "public"}, "isConstructor": False}
    member.update(fields)
    return member


# A class page: what member lists carry.
CLASS_PAGE = {
    "name": "Short",
    "kind": "class",
    "dri": "kotlin/Short///PointingToDeclaration/",
    "url": "index.html",
    "sourceSets": [":kotlin-stdlib/common"],
    "breadcrumbs": [{"name": "kotlin", "url": "../index.html"}, {"name": "Short", "url": "index.html"}],
    "documentation": {":kotlin-stdlib/common": [{"type": "Description", "text": "<p>A short.</p><p>16 bits.</p>"}],
                      ":kotlin-stdlib/jvm": [{"type": "Description", "text": "<p>A JVM short.</p>"}]},
    "modifier": {":kotlin-stdlib/common": "final"},
    "supertypes": {"common": [{"kind": "GenericTypeConstructor", "typeConstructor": INT}]},
    "functions": [
        _member("plus",
                documentation={":kotlin-stdlib/common": [
                    {"type": "Description", "text": "<p>Adds.</p>\n<p>Overflows.</p>"},
                    {"type": "Param", "name": "other", "text": "<p>The other.</p>"},
                    {"type": "Sample", "name": "samples.plus"}],
                    ":kotlin-stdlib/jvm": [{"type": "Description", "text": "<p>JVM adds.</p>"}]},
                modifier={":kotlin-stdlib/common": "open", ":kotlin-stdlib/jvm": "final"},
                extras={"annotations": {":kotlin-stdlib/common": [SINCE]}, "isException": True,
                        "isObviousMember": False},
                parameters=[{"name": "other", "dri": "d", "type": INT, "sourceSets": []}],
                type=INT),
        _member("toString",
                documentation={":kotlin-stdlib/common": [{"type": "Sample", "name": "samples.toString"}]},
                modifier={":kotlin-stdlib/common": "public"},
                extras={"isException": False},
                receiver={"dri": "no type here"},
                type={"kind": "TypeParameter", "dri": "kotlin//toString/#R/PointingToDeclaration/", "name": "",
                      "url": "r.html"}),
    ],
    "properties": [
        _member("MAX", kind="property", getter={"name": "get-MAX"},
                generics=[{"dri": "a generic with no name"}],
                type=INT, modifier={":kotlin-stdlib/common": "final", ":kotlin-stdlib/jvm": "open"}),
    ],
}


def _compress(raw: bytes, dictionary: bytes = None) -> bytes:
    """Stored bytes for a brotli row, at quality 5: the tool re-compresses at the default (11), so a row it touched
    comes out different from one it did not."""
    if dictionary is None:
        return brotli.compress(raw, quality=5)
    with tempfile.TemporaryDirectory() as directory:
        dict_path = Path(directory) / "dictionary.bin"
        dict_path.write_bytes(dictionary)
        return subprocess.run(["brotli", "-D", str(dict_path), "-q", "5", "-c"], input=raw,
                              stdout=subprocess.PIPE, check=True).stdout


def _decompress(blob: bytes, dictionary: bytes = None) -> bytes:
    """Decoded independently of the tool's own codec."""
    if dictionary is None:
        return brotli.decompress(blob)
    with tempfile.TemporaryDirectory() as directory:
        dict_path = Path(directory) / "dictionary.bin"
        dict_path.write_bytes(dictionary)
        return subprocess.run(["brotli", "-D", str(dict_path), "-d", "-c"], input=blob,
                              stdout=subprocess.PIPE, check=True).stdout


def _dump(page) -> bytes:
    return json.dumps(page, separators=(",", ":"), ensure_ascii=False).encode("utf-8")


TYPES = {"text/html": (12, "brotli"), "text/plain": (5, "none")}


def _database(tmp_path: Path, rows: dict, dictionary: bytes = None, templates: dict = None) -> Path:
    """A miniature documentation database. `rows` maps path -> (content type, decoded bytes [, templateId [, stored
    as they are]]); `templates` maps id -> (name, body), and defaults to layout.pebble at id 7. Brotli rows are
    compressed against `dictionary` when one is given."""
    db_path = tmp_path / "documentation.db"
    conn = sqlite3.connect(db_path)
    conn.executescript(
        "CREATE TABLE ContentTypes (id INTEGER PRIMARY KEY, value TEXT NOT NULL UNIQUE, "
        "compression TEXT NOT NULL);"
        "CREATE TABLE Content (id INTEGER PRIMARY KEY AUTOINCREMENT, path TEXT NOT NULL, "
        "languageID INTEGER NOT NULL, content BLOB NOT NULL, contentTypeID INTEGER NOT NULL, "
        "templateId INTEGER NOT NULL DEFAULT 0, UNIQUE(path));"
        "CREATE TABLE Templates (id INTEGER PRIMARY KEY AUTOINCREMENT, name TEXT NOT NULL, "
        "content BLOB NOT NULL, UNIQUE(name));"
    )
    conn.executemany("INSERT INTO ContentTypes (id, value, compression) VALUES (?, ?, ?)",
                     [(type_id, value, compression) for value, (type_id, compression) in TYPES.items()])
    if dictionary is not None:
        conn.execute("CREATE TABLE CompressionDictionary (id INTEGER PRIMARY KEY, data BLOB NOT NULL)")
        conn.execute("INSERT INTO CompressionDictionary (id, data) VALUES (1, ?)", (dictionary,))
    for path, (content_type, raw, *extra) in rows.items():
        template_id, as_is = (list(extra) + [7, False][len(extra):])[:2]
        type_id, compression = TYPES[content_type]
        stored = raw if as_is or compression == "none" else _compress(raw, dictionary)
        conn.execute("INSERT INTO Content (path, languageID, content, contentTypeID, templateId) "
                     "VALUES (?, 1, ?, ?, ?)", (path, stored, type_id, template_id))
    if templates is None:
        templates = {7: (tool.TEMPLATE_NAME, TINY_TEMPLATE)}
    for template_id, (name, body) in templates.items():
        conn.execute("INSERT INTO Templates (id, name, content) VALUES (?, ?, ?)", (template_id, name, body))
    conn.commit()
    conn.close()
    return db_path


def _stored(db_path: Path) -> dict:
    """{path: (languageID, content, contentTypeID, templateId)} for every row."""
    conn = sqlite3.connect(db_path)
    try:
        return {row[0]: row[1:] for row in conn.execute(
            "SELECT path, languageID, content, contentTypeID, templateId FROM Content")}
    finally:
        conn.close()


def _run(db_path: Path, template: bytes = TINY_TEMPLATE, **overrides) -> tuple:
    """(exit code, stdout, stderr) of one run of the tool, with TEMPLATE_CODE_SHA256 pointing at `template`."""
    cfg = {"db_path": db_path, "changes": ALL, "dry_run": False, "workers": 2, "verbose": False}
    cfg.update(overrides)
    out, err = io.StringIO(), io.StringIO()
    real = tool.TEMPLATE_CODE_SHA256
    tool.TEMPLATE_CODE_SHA256 = tool.template_code_fingerprint(template.decode("utf-8"))
    try:
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            code = tool.run(cfg)
    finally:
        tool.TEMPLATE_CODE_SHA256 = real
    return code, out.getvalue(), err.getvalue()


def _skip(reason: str) -> None:
    try:
        import pytest
    except ImportError:
        raise _Skipped(reason) from None
    pytest.skip(reason)


class _Skipped(Exception):
    pass


def _needs(tool_name: str) -> None:
    if shutil.which(tool_name) is None:
        _skip(f"{tool_name} is not on PATH")


def _all(page: dict, changes=ALL) -> dict:
    for name in changes:
        page = tool.CHANGES[name][0](page)
    return page


def test_t01_keeps_only_what_a_signature_page_reads():
    assert tool.drop_unread_keys(FUNCTION_PAGE) == FUNCTION_PAGE_READ


def test_t01_keeps_only_what_member_lists_read():
    page = tool.drop_unread_keys(CLASS_PAGE)
    assert list(page) == ["name", "kind", "sourceSets", "breadcrumbs", "documentation", "modifier", "functions",
                          "properties"]
    plus, to_string = page["functions"]
    assert plus == {
        "name": "plus", "kind": "function", "url": "plus.html", "sourceSets": [":kotlin-stdlib/common"],
        "documentation": {":kotlin-stdlib/common": [
            {"type": "Description", "text": "<p>Adds.</p>\n<p>Overflows.</p>"}, {"type": "Param"},
            {"type": "Sample"}], ":kotlin-stdlib/jvm": [{"type": "Description", "text": "<p>JVM adds.</p>"}]},
        "modifier": {":kotlin-stdlib/common": "open", ":kotlin-stdlib/jvm": "final"},
        "extras": {"annotations": {":kotlin-stdlib/common": [{"dri": SINCE["dri"], "url": SINCE["url"]}]},
                   "isObviousMember": False},
        "parameters": [{"name": "other", "type": INT}], "type": INT}
    # An extras left empty goes; a receiver left empty stays, since one that is there prints its ".".
    assert "extras" not in to_string and to_string["receiver"] == {}
    # A type with no name keeps the dri its name is read from.
    assert to_string["type"] == {"kind": "TypeParameter", "dri": "kotlin//toString/#R/PointingToDeclaration/",
                                 "name": "", "url": "r.html"}
    # A generic with no name stays, as an empty object, so the separators between generics do not move.
    assert page["properties"][0]["generics"] == [{}]


def test_t01_drops_the_signature_of_a_page_that_shows_none():
    for kind in ("package", "annotation", "enumEntry", "module", None):
        page = dict(FUNCTION_PAGE, kind=kind)
        if kind is None:
            del page["kind"]
        read = tool.drop_unread_keys(page)
        assert not set(read) & {"modifier", "extras", "generics", "receiver", "parameters", "type"}, kind


def test_t01_reads_each_type_kind_as_render_type_does():
    for kind in ("Star", "JavaObject", "Void", "Dynamic"):
        assert tool._read_of_type({"kind": kind, "dri": "x", "name": "n", "inner": INT}) == {"kind": kind}
    for kind in ("Covariance", "Contravariance", "Invariance", "Nullable", "DefinitelyNonNullable"):
        assert tool._read_of_type({"kind": kind, "dri": "x", "inner": T_PARAM}) == \
            {"kind": kind, "inner": {"kind": "TypeParameter", "name": "T", "url": "plus.html"}}
    assert tool._read_of_type({"kind": "GenericTypeConstructor", "name": None, "dri": "d", "url": "u",
                               "projections": [T_PARAM]}) == \
        {"kind": "GenericTypeConstructor", "name": None, "dri": "d", "url": "u",
         "projections": [{"kind": "TypeParameter", "name": "T", "url": "plus.html"}]}
    assert tool._read_of_type({"dri": "no kind"}) == {"dri": "no kind"}
    assert tool._read_of_type(None) is None and tool._read_of_type("Int") == "Int"


def test_t02_keeps_the_first_entry_of_each_map_read_only_first():
    page = tool.keep_first_source_set(FUNCTION_PAGE)
    common = ":kotlin-stdlib/common"
    assert list(page["documentation"]) == [common]
    assert page["modifier"] == {common: "open"}
    assert list(page["extras"]["annotations"]) == [common] and list(page["extras"]["additionalModifiers"]) == [common]
    assert page["parameters"][0]["extras"]["defaultValues"] == {common: "0"}
    # Maps the template reads whole, or not at all, are T01's to drop, not T02's.
    assert page["visibility"] == FUNCTION_PAGE["visibility"]
    member = tool.keep_first_source_set(CLASS_PAGE)["functions"][0]
    assert list(member["documentation"]) == [common] and member["modifier"] == {common: "open"}
    alias = tool.keep_first_source_set({"typeAliases": [{"underlyingType": {"a": INT, "b": T_PARAM}}]})
    assert alias["typeAliases"][0]["underlyingType"] == {"a": INT}


def test_t03_cuts_member_briefs_after_their_first_paragraph():
    page = tool.cut_member_briefs(CLASS_PAGE)
    plus = page["functions"][0]["documentation"]
    assert plus[":kotlin-stdlib/common"][0] == {"type": "Description", "text": "<p>Adds.</p>"}
    assert plus[":kotlin-stdlib/common"][1:] == CLASS_PAGE["functions"][0]["documentation"][":kotlin-stdlib/common"][1:]
    assert plus[":kotlin-stdlib/jvm"] == [{"type": "Description", "text": "<p>JVM adds.</p>"}]
    # The page's own documentation is not a brief.
    assert page["documentation"] == CLASS_PAGE["documentation"]
    assert tool._cut_after_first_paragraph({"type": "Description", "text": "no paragraph"})["text"] == "no paragraph"


def test_t04_keeps_only_description_tags_in_member_lists():
    page = tool.keep_member_descriptions(CLASS_PAGE)
    plus, to_string = page["functions"]
    assert plus["documentation"] == {
        ":kotlin-stdlib/common": [{"type": "Description", "text": "<p>Adds.</p>\n<p>Overflows.</p>"}],
        ":kotlin-stdlib/jvm": [{"type": "Description", "text": "<p>JVM adds.</p>"}]}
    assert "documentation" not in to_string
    assert page["documentation"] == CLASS_PAGE["documentation"]
    # Documentation goes only when no entry has a Description left.
    mixed = {"functions": [{"name": "f", "documentation": {"a": [{"type": "Sample"}],
                                                           "b": [{"type": "Description", "text": "<p>B.</p>"}]}}]}
    assert tool.keep_member_descriptions(mixed)["functions"][0]["documentation"] == \
        {"a": [], "b": [{"type": "Description", "text": "<p>B.</p>"}]}


def test_t05_drops_unprinted_modifiers_and_the_last_crumb_url():
    page = tool.drop_unprinted(CLASS_PAGE)
    assert "modifier" not in page
    assert page["breadcrumbs"] == [{"name": "kotlin", "url": "../index.html"}, {"name": "Short"}]
    plus, to_string = page["functions"]
    # Only the first entry's value is printed, so a later final does not count.
    assert plus["modifier"] == CLASS_PAGE["functions"][0]["modifier"]
    assert "modifier" not in to_string and "modifier" not in page["properties"][0]
    assert tool.drop_unprinted(FUNCTION_PAGE)["modifier"] == FUNCTION_PAGE["modifier"]


# What layout.pebble shows, written out here rather than taken from the tool, so a test fails when the tool's tables
# stop matching the template: the page kinds it renders a signature for (its "if kind == ..." before the signature),
# and the member lists it renders with renderMembers.
SIGNED_PAGE_KINDS = ("function", "property", "typeAlias", "class", "interface", "object", "enum")
SHOWN_MEMBER_LISTS = ("modules", "packages", "classlikes", "typeAliases", "constructors", "properties", "functions",
                      "entries")
# CLASS_PAGE's "plus" member after all five changes.
PLUS_TRIMMED = {
    "name": "plus", "kind": "function", "url": "plus.html", "sourceSets": [":kotlin-stdlib/common"],
    "documentation": {":kotlin-stdlib/common": [{"type": "Description", "text": "<p>Adds.</p>"}]},
    "modifier": {":kotlin-stdlib/common": "open"},
    "extras": {"annotations": {":kotlin-stdlib/common": [{"dri": SINCE["dri"], "url": SINCE["url"]}]},
               "isObviousMember": False},
    "parameters": [{"name": "other", "type": INT}], "type": INT}


def test_every_member_list_the_template_shows_is_kept_and_trimmed():
    member = CLASS_PAGE["functions"][0]
    for name in SHOWN_MEMBER_LISTS:
        page = _all({"name": "p", "kind": "package", "dri": "p///", name: [copy.deepcopy(member)]})
        assert list(page) == ["name", "kind", name], name
        assert page[name] == [PLUS_TRIMMED], name


def test_t01_keeps_the_signature_of_every_page_kind_the_template_signs():
    for kind in SIGNED_PAGE_KINDS:
        assert tool.drop_unread_keys(dict(FUNCTION_PAGE, kind=kind)) == dict(FUNCTION_PAGE_READ, kind=kind), kind


def test_t01_keeps_what_the_types_index_reads():
    # The "Index of All Types" table reads each entry's kind, url, name and sourceSets.
    page = {"name": "All Types", "kind": "allTypes", "dri": "x///", "types": [
        {"kind": "class", "url": "a.html", "name": "A", "sourceSets": [":kotlin-stdlib/common"], "dri": "a///",
         "documentation": {":kotlin-stdlib/common": []}},
        {"kind": "typeAlias", "name": "B", "sourceSets": [":kotlin-stdlib/jvm"], "visibility": {}}]}
    assert tool.drop_unread_keys(page) == {"name": "All Types", "kind": "allTypes", "types": [
        {"kind": "class", "url": "a.html", "name": "A", "sourceSets": [":kotlin-stdlib/common"]},
        {"kind": "typeAlias", "name": "B", "sourceSets": [":kotlin-stdlib/jvm"]}]}


def test_t05_keeps_every_modifier_the_template_prints():
    # The template prints the first modifier's value unless it is final or public, an empty one included.
    for value in ("open", "abstract", "sealed", ""):
        page = {"name": "f", "kind": "function", "modifier": {":kotlin-stdlib/common": value},
                "functions": [{"name": "g", "modifier": {":kotlin-stdlib/common": value}}]}
        out = tool.drop_unprinted(page)
        assert out["modifier"] == {":kotlin-stdlib/common": value}, value
        assert out["functions"][0]["modifier"] == {":kotlin-stdlib/common": value}, value


def test_no_change_alters_the_page_it_is_given():
    for page in (FUNCTION_PAGE, CLASS_PAGE):
        before = copy.deepcopy(page)
        for name in ALL:
            tool.CHANGES[name][0](page)
        assert page == before


def test_a_page_with_nothing_to_change_comes_back_as_it_was():
    page = {"name": "x", "kind": "package", "sourceSets": [], "functions": [{"name": "f", "url": "f.html"}]}
    for name in ALL:
        assert tool.CHANGES[name][0](page) == page, name


def test_changing_twice_changes_nothing_more():
    for page in (FUNCTION_PAGE, CLASS_PAGE):
        once = _all(page)
        assert _all(once) == once
        for name in ALL:
            assert tool.CHANGES[name][0](once) == once, name


def test_odd_shapes_are_left_as_they_are():
    page = {"name": "x", "kind": "function", "documentation": ["not", "a", "map"], "modifier": "open",
            "extras": ["odd"], "parameters": "none", "receiver": None, "generics": None,
            "functions": ["not a member", {"name": "f", "documentation": {"a": "not a list"}}],
            "breadcrumbs": "home"}
    assert _all(page) == page


def test_a_row_is_written_back_in_the_form_it_was_stored_in():
    page = {"name": "caf\u00e9 \"x\"", "kind": "package", "visibility": {}, "sourceSets": ["a/b"]}
    raw = _dump(page)
    new, steps = tool.trim(raw, ALL)
    assert new == b'{"name":"caf\xc3\xa9 \\"x\\"","kind":"package","sourceSets":["a/b"]}'
    assert steps[0] == ("T01", True, len(raw) - len(new)) and all(not changed for _n, changed, _s in steps[1:])


def test_the_template_fingerprint_ignores_markup_whitespace_and_comments():
    fingerprint = tool.template_code_fingerprint
    assert fingerprint("<p>\n  {{ a }}\n</p>{# note #}") == fingerprint("<p>{{  a\n}}</p>")
    assert fingerprint("{{ a }}") != fingerprint("{{ b }}")
    assert fingerprint("{% if a %}{% endif %}") != fingerprint("{%- if a %}{% endif %}")
    # A string inside a tag is code: the template choosing a different tag type is a change.
    assert fingerprint('{% if t.type == "Description" %}') != fingerprint('{% if t.type == "Param" %}')


def test_rows_are_trimmed_and_compressed_with_the_database_dictionary(tmp_path: Path):
    _needs("brotli")
    dictionary = _dump(CLASS_PAGE) * 2 + _dump(FUNCTION_PAGE)
    db = _database(tmp_path, {
        "k/kotlin/-short/index.html": ("text/html", _dump(CLASS_PAGE)),
        "k/kotlin/plus.html": ("text/html", _dump(FUNCTION_PAGE)),
        "p/topic.html": ("text/html", _dump(FUNCTION_PAGE), 1),
        "k/notes.txt": ("text/plain", b'{"name": "plain"}', 0),
    }, dictionary, templates={1: ("page.peb", b"{{ name }}"), 7: (tool.TEMPLATE_NAME, TINY_TEMPLATE)})
    before = _stored(db)
    code, out, err = _run(db)
    after = _stored(db)

    assert code == 0, err
    assert _decompress(after["k/kotlin/-short/index.html"][1], dictionary) == _dump(_all(CLASS_PAGE))
    assert _decompress(after["k/kotlin/plus.html"][1], dictionary) == _dump(_all(FUNCTION_PAGE))
    for path in ("p/topic.html", "k/notes.txt"):
        assert after[path] == before[path], path
    for path in after:
        assert after[path][0] == before[path][0] and after[path][2:] == before[path][2:], path
    assert "changed 2 row(s)" in out and "T03 cut_member_briefs: 1 row(s)" in out


def test_the_template_is_found_by_name_not_by_id(tmp_path: Path):
    _needs("brotli")
    db = _database(tmp_path, {"k/a.html": ("text/html", _dump(FUNCTION_PAGE), 1),
                              "k/b.html": ("text/html", _dump(FUNCTION_PAGE), 9)},
                   templates={1: ("page.peb", TINY_TEMPLATE), 9: (tool.TEMPLATE_NAME, TINY_TEMPLATE)})
    before = _stored(db)
    code, out, err = _run(db)
    after = _stored(db)
    assert code == 0, err
    assert after["k/a.html"] == before["k/a.html"]
    assert _decompress(after["k/b.html"][1]) == _dump(_all(FUNCTION_PAGE))


def test_only_the_named_changes_are_made(tmp_path: Path):
    _needs("brotli")
    db = _database(tmp_path, {"k/a.html": ("text/html", _dump(CLASS_PAGE))})
    code, out, err = _run(db, changes=["T01", "T02", "T04", "T05"])
    assert code == 0, err
    trimmed = json.loads(_decompress(_stored(db)["k/a.html"][1]))
    assert trimmed == _all(CLASS_PAGE, ["T01", "T02", "T04", "T05"])
    assert trimmed["functions"][0]["documentation"][":kotlin-stdlib/common"][0]["text"] == \
        "<p>Adds.</p>\n<p>Overflows.</p>"
    assert "T03" not in out


def test_changes_option_takes_a_list_in_any_order():
    assert tool.changes_arg("t05, T01") == ["T01", "T05"]
    for bad in ("T06", "", "T01,,X"):
        try:
            tool.changes_arg(bad)
        except Exception as exc:  # noqa: BLE001
            assert "expected a comma-separated list" in str(exc)
        else:
            raise AssertionError(f"{bad!r} was accepted")


def test_a_missing_database_is_an_error(tmp_path: Path):
    db = tmp_path / "missing.db"
    code, _out, err = _run(db)
    assert code == 1 and "does not exist" in err and not db.exists()


def test_without_the_template_nothing_changes(tmp_path: Path):
    _needs("brotli")
    db = _database(tmp_path, {"k/a.html": ("text/html", _dump(FUNCTION_PAGE), 1)},
                   templates={1: ("page.peb", TINY_TEMPLATE)})
    before = _stored(db)
    code, _out, err = _run(db)
    assert code == 1 and "no template is named layout.pebble" in err
    assert _stored(db) == before


def test_a_changed_template_stops_the_step(tmp_path: Path):
    _needs("brotli")
    changed = TINY_TEMPLATE + b"{{ dri }}"
    db = _database(tmp_path, {"k/a.html": ("text/html", _dump(FUNCTION_PAGE))},
                   templates={7: (tool.TEMPLATE_NAME, changed)})
    before = _stored(db)
    code, _out, err = _run(db)
    assert code == 1 and "is not the code these changes were checked against" in err
    assert _stored(db) == before
    # The same template with its markup reflowed is not a change.
    reflowed = TINY_TEMPLATE.replace(b"\n", b"\n   ") + b"{# a note #}"
    conn = sqlite3.connect(db)
    conn.execute("UPDATE Templates SET content = ?", (reflowed,))
    conn.commit()
    conn.close()
    assert _run(db)[0] == 0


def test_a_database_declaring_version_3_is_refused(tmp_path: Path):
    # From 3.0.0 migrate_tooltip_schema.py stores every template compressed, so the step has to run before it. The
    # template here is plain, so only the version can stop the run.
    _needs("brotli")
    db = _database(tmp_path, {"k/a.html": ("text/html", _dump(FUNCTION_PAGE))})
    conn = sqlite3.connect(db)
    conn.execute("CREATE TABLE DocumentationDatabaseVersion (major INT, minor INT, patch INT, who TEXT, "
                 "comment TEXT, changeTime TIMESTAMP)")
    conn.executemany("INSERT INTO DocumentationDatabaseVersion VALUES (?, 0, 0, 'test', '', ?)",
                     [(2, "2026-08-20 22:52:54"), (3, "2026-10-19 03:15:03")])
    conn.commit()
    conn.close()
    before = _stored(db)
    code, _out, err = _run(db)
    assert code == 1 and "Run it before migrate_tooltip_schema.py" in err
    assert _stored(db) == before
    # The newest row decides: with 3.0.0 gone, the 2.0.0 database is trimmed.
    conn = sqlite3.connect(db)
    conn.execute("DELETE FROM DocumentationDatabaseVersion WHERE major = 3")
    conn.commit()
    conn.close()
    assert _run(db)[0] == 0


def test_an_unreadable_row_stops_the_run_before_anything_is_written(tmp_path: Path):
    _needs("brotli")
    db = _database(tmp_path, {"k/a.html": ("text/html", _dump(FUNCTION_PAGE)),
                              "k/broken.html": ("text/html", b"not brotli at all", 7, True)})
    before = _stored(db)
    code, _out, err = _run(db)
    assert code == 1 and "nothing was written" in err and "k/broken.html: could not be read" in err
    # The good row is not trimmed either, and no backup is made: exit 1 means the database was not touched.
    assert _stored(db) == before
    assert [p.name for p in tmp_path.iterdir()] == ["documentation.db"]


def test_a_row_that_is_not_a_json_object_stops_the_run_before_anything_is_written(tmp_path: Path):
    _needs("brotli")
    # The package-list row is ADFA-6630's case: plain text left on layout.pebble.
    db = _database(tmp_path, {"k/a.html": ("text/html", _dump(FUNCTION_PAGE)),
                              "k/list.html": ("text/html", b'[{"name":"x","dri":"y"}]'),
                              "k/package-list": ("text/html", b"$dokka.format:html-v1\nkotlin\n")})
    before = _stored(db)
    code, _out, err = _run(db)
    assert code == 1 and "nothing was written" in err
    assert "k/list.html: is JSON but not an object" in err and "k/package-list: is not JSON" in err
    assert _stored(db) == before
    # A rerun refuses the same way and still writes nothing.
    assert _run(db)[0] == 1 and _stored(db) == before
    assert [p.name for p in tmp_path.iterdir()] == ["documentation.db"]


def test_a_refused_dry_run_exits_1_too(tmp_path: Path):
    _needs("brotli")
    db = _database(tmp_path, {"k/a.html": ("text/html", _dump(FUNCTION_PAGE)),
                              "k/package-list": ("text/html", b"kotlin\n")})
    before = _stored(db)
    code, _out, err = _run(db, dry_run=True)
    assert code == 1 and "k/package-list: is not JSON" in err
    assert _stored(db) == before


def test_a_row_not_stored_as_compact_json_is_left_as_it_is(tmp_path: Path):
    _needs("brotli")
    loose = json.dumps(FUNCTION_PAGE, ensure_ascii=False).encode("utf-8")
    escaped = json.dumps(FUNCTION_PAGE | {"name": "caf\u00e9"}, separators=(",", ":")).encode("utf-8")
    # A lone surrogate reads as JSON but cannot be written back as UTF-8.
    surrogate = b'{"name":"\\ud800","kind":"package"}'
    db = _database(tmp_path, {"k/loose.html": ("text/html", loose), "k/escaped.html": ("text/html", escaped),
                              "k/surrogate.html": ("text/html", surrogate)})
    before = _stored(db)
    code, _out, err = _run(db)
    assert code == 1 and err.count("is not stored as compact JSON") == 3
    assert _stored(db) == before


def test_rows_the_step_does_not_change_keep_their_stored_bytes(tmp_path: Path):
    _needs("brotli")
    db = _database(tmp_path, {"k/done.html": ("text/html", _dump(_all(CLASS_PAGE)))})
    before = _stored(db)
    code, out, _err = _run(db)
    assert code == 0 and "changed 0 row(s)" in out
    assert _stored(db) == before


def test_a_second_run_changes_nothing(tmp_path: Path):
    _needs("brotli")
    db = _database(tmp_path, {"k/a.html": ("text/html", _dump(CLASS_PAGE)),
                              "k/b.html": ("text/html", _dump(FUNCTION_PAGE))})
    assert _run(db)[0] == 0
    once = _stored(db)
    code, out, _err = _run(db)
    assert code == 0 and "changed 0 row(s)" in out
    assert _stored(db) == once


def test_dry_run_writes_nothing(tmp_path: Path):
    _needs("brotli")
    db = _database(tmp_path, {"k/a.html": ("text/html", _dump(FUNCTION_PAGE))})
    before = _stored(db)
    code, out, _err = _run(db, dry_run=True)
    assert code == 0 and "would change 1 row(s)" in out
    assert _stored(db) == before
    assert len(list(tmp_path.iterdir())) == 1


def test_the_run_gives_the_space_it_frees_back(tmp_path: Path):
    _needs("brotli")
    import random
    rng = random.Random(11)
    # Random text compresses poorly, so dropping it (an unread key) frees many pages.
    noise = "".join(rng.choice("abcdefghijklmnopqrstuvwxyz0123456789") for _ in range(400_000))
    db = _database(tmp_path, {"k/a.html": ("text/html", _dump(dict(FUNCTION_PAGE, dri=noise)))})
    size = db.stat().st_size
    code, _out, err = _run(db)
    conn = sqlite3.connect(db)
    free = conn.execute("PRAGMA freelist_count").fetchone()[0]
    conn.close()
    assert code == 0, err
    assert free == 0 and db.stat().st_size < size // 4


def test_the_backup_holds_the_database_as_it_was(tmp_path: Path):
    _needs("brotli")
    db = _database(tmp_path, {"k/a.html": ("text/html", _dump(FUNCTION_PAGE))})
    before = _stored(db)
    code, out, _err = _run(db)
    backup = Path(re.search(r"Backup written to (.+)", out).group(1).strip())
    assert code == 0 and backup.is_file()
    assert _stored(backup) == before


def test_an_error_while_writing_rolls_everything_back(tmp_path: Path):
    _needs("brotli")
    db = _database(tmp_path, {"k/a.html": ("text/html", _dump(FUNCTION_PAGE)),
                              "k/b.html": ("text/html", _dump(CLASS_PAGE))})
    before = _stored(db)
    real = tool.replace_row
    calls = []

    def failing(*args, **kwargs):
        calls.append(args[1])
        if len(calls) == 2:
            raise RuntimeError("disk full")
        return real(*args, **kwargs)

    tool.replace_row = failing
    try:
        try:
            _run(db)
        except RuntimeError:
            pass
        else:
            raise AssertionError("the failure did not propagate")
    finally:
        tool.replace_row = real
    assert len(calls) == 2 and _stored(db) == before


def test_a_chunked_row_is_read_and_written_whole(tmp_path: Path):
    _needs("brotli")
    # Random-looking text compresses poorly, so the stored row spans chunks.
    import random
    rng = random.Random(7)
    members = [{"name": "".join(rng.choice("abcdefghijklmnopqrstuvwxyz") for _ in range(12)),
                "url": "".join(rng.choice("abcdefghijklmnopqrstuvwxyz") for _ in range(12)) + ".html",
                "dri": "".join(rng.choice("abcdefghijklmnopqrstuvwxyz") for _ in range(40))}
               for _ in range(40_000)]
    page = {"name": "big", "kind": "package", "functions": members}
    stored = brotli.compress(_dump(page), quality=5)
    assert len(stored) > CHUNK_SIZE
    db = _database(tmp_path, {"k/big.html": ("text/html", stored[:CHUNK_SIZE], 7, True),
                              "k/big.html-1": ("text/html", stored[CHUNK_SIZE:], 7, True)})
    code, _out, err = _run(db)
    rows = _stored(db)
    assert code == 0, err
    joined = b"".join(rows[p][1] for p in sorted(rows, key=lambda p: (len(p), p)))
    assert _decompress(joined) == _dump(_all(page))


def _main() -> int:
    """Runs the tests without pytest installed as a runner, so this file works the same way the tools beside it do
    (`uv run scripts/test_trim_kotlin_json.py`)."""
    failures = []
    for name, function in sorted(globals().items()):
        if not name.startswith("test_") or not callable(function):
            continue
        with tempfile.TemporaryDirectory() as directory:
            needs_tmp = "tmp_path" in function.__code__.co_varnames[:function.__code__.co_argcount]
            try:
                function(Path(directory)) if needs_tmp else function()
            except (KeyboardInterrupt, SystemExit):
                raise
            except BaseException as exc:  # noqa: BLE001 - a failure is the result here
                if type(exc).__name__ in ("Skipped", "_Skipped"):
                    print(f"skip {name}: {exc}")
                    continue
                frame = traceback.extract_tb(exc.__traceback__)[-1]
                failures.append(f"{name} (line {frame.lineno}: {frame.line}): "
                                f"{type(exc).__name__}: {exc}")
                print(f"FAIL {name}", file=sys.stderr)
            else:
                print(f"ok   {name}")
    print()
    print(f"{len(failures)} failure(s)" if failures else "all tests passed")
    for failure in failures:
        print(f"  {failure}", file=sys.stderr)
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(_main())
