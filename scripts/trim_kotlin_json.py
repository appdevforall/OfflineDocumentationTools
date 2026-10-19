#!/usr/bin/env python3
"""
Trims the JSON of the Kotlin API pages to what their template shows.

The k/ Content rows rendered with layout.pebble hold Dokka's JSON for each
page, and the template reads well under half of it. Five changes, each its own
function:

- T01 drop_unread_keys: keeps only the keys layout.pebble reads where they
  occur (a page's own documentation tags are kept whole), and removes an
  extras object left empty.
- T02 keep_first_source_set: keeps only the first entry of each per-source-set
  map the template reads only the first entry of: documentation,
  extras.annotations, extras.additionalModifiers, a parameter's
  extras.defaultValues, underlyingType and modifier. This drops what the other
  platforms say, which no page shows today.
- T03 cut_member_briefs: cuts each member's Description, which a member table
  shows as its summary, after its first </p>. This changes pages: member tables
  show only the first paragraph.
- T04 keep_member_descriptions: in member lists keeps only Description tags,
  the only tags the template shows there, and drops a member's documentation
  left without one.
- T05 drop_unprinted: drops a modifier whose first value is final or public,
  which the template prints nothing for, and the last breadcrumb's url, which
  it never links.

With T01, T02, T04 and T05 every page renders byte-identical; with T03 only
member summaries change (checked on all 3,237 rows of the 2026-10-18 database
with Pebble 4.1.1 and Gson 2.12.1 set up as the app sets them up).

The template is found by name. The changes are only right for the template
they were checked against, so the step refuses to run when layout.pebble's
Pebble code differs from that (TEMPLATE_CODE_SHA256; whitespace outside
Pebble tags and {# #} comments do not count). If any layout.pebble row cannot
be read, is not a JSON object, or is not stored as compact JSON (the form
json.dumps with separators=(",", ":") and ensure_ascii=False writes, which
every row had), the step names those rows and writes nothing. So exit 1
always means the database was left as it was, and 0 that the run finished.
The k/*package-list rows of ADFA-6630 are such rows while they still point
at layout.pebble: move them off it first.

Measured on the 2026-10-18 database (the ADFA-6627 state, 110,895,104
bytes): the 3,237 rows go from 35,468,934 decoded bytes to 13,118,583 without
T03 and 12,138,123 with it. Bytes saved against that database packed the same
way (rebuild_database.py --pack Content,Tooltips --page-size 4096; .br made
with brotli -q 11 -w 24):

                  rows stored      file       .br
    T01 T02 T04 T05   637,435   643,072   779,293
    all five          732,279   737,280   867,001

Re-minting the dictionary afterwards made the file larger, by 536,576 and
335,872 bytes, so this step needs no re-mint. Row order alone moves the .br:
packing the same database a second time changed it by 173,277 bytes.

Pipeline position: after every step that writes the Kotlin API rows
(sync_kdoc_json_to_db.py), and before rebuild_database.py.

Usage:
    ./trim_kotlin_json.py documentation.db [--changes T01,T02,T03,T04,T05] [--dry-run] [--workers N] [--verbose]
"""
from __future__ import annotations

import argparse
import hashlib
import json
import re
import sqlite3
import sys
from collections import defaultdict
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from optimize_db_media import BrotliCodec, backup_database, human, is_continuation_path, load_dictionary, reassemble
from update_media_references import replace_row

TEMPLATE_NAME = "layout.pebble"

# template_code_fingerprint() of the layout.pebble these changes were checked against.
TEMPLATE_CODE_SHA256 = "6aaa5b48a8c7453f47c22ba648ab318b98e829d2a6b069e3e08d7f8068fcc757"

MEMBER_LISTS = ("modules", "packages", "classlikes", "typeAliases", "constructors", "properties", "functions",
                "entries")
# Page kinds whose page shows a signature, so it reads the page's own signature fields.
SIGNATURE_PAGE_KINDS = {"function", "property", "typeAlias", "class", "interface", "object", "enum"}
PAGE_KEYS = frozenset(("name", "kind", "sourceSets", "breadcrumbs", "documentation", "types", *MEMBER_LISTS))
SIGNATURE_KEYS = frozenset(("kind", "name", "extras", "modifier", "generics", "receiver", "parameters", "type",
                            "underlyingType"))
MEMBER_KEYS = SIGNATURE_KEYS | {"url", "sourceSets", "documentation"}

# What renderType reads of a type, by kind; any other kind is read as a named type (NAMED_TYPE_KEYS).
TYPE_KEYS = {
    "Star": ("kind",),
    "JavaObject": ("kind",),
    "Void": ("kind",),
    "Dynamic": ("kind",),
    "Covariance": ("kind", "inner"),
    "Contravariance": ("kind", "inner"),
    "Invariance": ("kind", "inner"),
    "Nullable": ("kind", "inner"),
    "DefinitelyNonNullable": ("kind", "inner"),
    "TypeAliased": ("kind", "typeAlias"),
    "FunctionalTypeConstructor": ("kind", "isSuspendable", "isExtensionFunction", "projections"),
}
# dri is read only when the type has no name.
NAMED_TYPE_KEYS = ("kind", "name", "presentableName", "url", "projections")

# The modifiers renderTableSignature prints nothing for.
UNPRINTED_MODIFIERS = ("final", "public")

# A Pebble tag, its quoted strings included, so "}}" inside one does not end it.
_PEBBLE_TAG = re.compile(
    r"\{\{(?:\"(?:\\.|[^\"\\])*\"|'(?:\\.|[^'\\])*'|[^\"'}]|\}(?!\}))*\}\}"
    r"|\{%(?:\"(?:\\.|[^\"\\])*\"|'(?:\\.|[^'\\])*'|[^\"'%]|%(?!\}))*%\}"
    r"|\{#.*?#\}",
    re.S,
)


class Unexpected(Exception):
    """A row this step does not know how to change safely."""


def template_code_fingerprint(template: str) -> str:
    """sha256 of a template's Pebble tags in order, each with its whitespace runs made one space. Comments and
    everything outside the tags are left out, so minifying the markup does not change it."""
    tags = [" ".join(m.group(0).split()) for m in _PEBBLE_TAG.finditer(template) if not m.group(0).startswith("{#")]
    return hashlib.sha256("\n".join(tags).encode("utf-8")).hexdigest()


def serialize(page) -> bytes:
    """JSON in the form every row is stored in."""
    return json.dumps(page, separators=(",", ":"), ensure_ascii=False).encode("utf-8")


def _keep(obj: dict, keys) -> dict:
    """A copy of obj with only the given keys, in obj's order."""
    return {key: value for key, value in obj.items() if key in keys}


def _with(obj: dict, key: str, value) -> dict:
    """obj with obj[key] set to value: a copy if that changes anything, otherwise obj itself."""
    if key in obj and obj[key] is value:
        return obj
    return {**obj, key: value}


def _each_member(page: dict, change) -> dict:
    """The page with change applied to every member of its member lists, copying only what changes."""
    for name in MEMBER_LISTS:
        members = page.get(name)
        if isinstance(members, list):
            changed = [change(m) if isinstance(m, dict) else m for m in members]
            if any(new is not old for new, old in zip(changed, members)):
                page = {**page, name: changed}
    return page


def _each_doc_list(member: dict, change) -> dict:
    """The member with change applied to the tag list of every entry of its documentation."""
    doc = member.get("documentation")
    if not isinstance(doc, dict):
        return member
    changed = {key: change(tags) if isinstance(tags, list) else tags for key, tags in doc.items()}
    if all(changed[key] is doc[key] for key in doc):
        return member
    return _with(member, "documentation", changed)


def drop_unread_keys(page: dict) -> dict:
    """T01: the page with only the keys layout.pebble reads where they occur, and no extras object left empty."""
    signature = page.get("kind") in SIGNATURE_PAGE_KINDS
    out = _keep(page, (PAGE_KEYS | SIGNATURE_KEYS) if signature else PAGE_KEYS)
    if isinstance(out.get("breadcrumbs"), list):
        out["breadcrumbs"] = [_keep(c, ("name", "url")) if isinstance(c, dict) else c for c in out["breadcrumbs"]]
    if isinstance(out.get("types"), list):
        out["types"] = [_keep(t, ("kind", "url", "name", "sourceSets")) if isinstance(t, dict) else t
                        for t in out["types"]]
    for name in MEMBER_LISTS:
        if isinstance(out.get(name), list):
            out[name] = [_read_of_member(m) if isinstance(m, dict) else m for m in out[name]]
    if signature:
        _read_of_signature(out, member=False)
    return out


def _read_of_member(member: dict) -> dict:
    out = _keep(member, MEMBER_KEYS)
    doc = out.get("documentation")
    if isinstance(doc, dict):
        # renderMembers reads every tag's type, and the text of a Description only.
        out["documentation"] = {
            key: [_keep(t, ("type", "text") if t.get("type") == "Description" else ("type",))
                  if isinstance(t, dict) else t for t in tags] if isinstance(tags, list) else tags
            for key, tags in doc.items()}
    _read_of_signature(out, member=True)
    return out


def _read_of_signature(out: dict, member: bool) -> None:
    """Narrows, in out (already a copy), the fields renderTableSignature reads to what it reads of them."""
    extras = out.get("extras")
    if isinstance(extras, dict):
        # renderMembers reads isObviousMember; the page-level signature does not.
        extras = _keep(extras, ("annotations", "additionalModifiers", "isObviousMember") if member
                       else ("annotations", "additionalModifiers"))
        annotations = extras.get("annotations")
        if isinstance(annotations, dict):
            extras["annotations"] = {
                key: [_keep(a, ("dri", "url")) if isinstance(a, dict) else a for a in value]
                if isinstance(value, list) else value for key, value in annotations.items()}
        # A missing extras and an empty one render alike in every test the template makes of it.
        if extras:
            out["extras"] = extras
        else:
            del out["extras"]
    if isinstance(out.get("generics"), list):
        out["generics"] = [_keep(g, ("name",)) if isinstance(g, dict) else g for g in out["generics"]]
    receiver = out.get("receiver")
    if isinstance(receiver, dict):
        # Kept even if left empty: a receiver that is there prints its "." whatever it holds.
        receiver = _keep(receiver, ("type",))
        if "type" in receiver:
            receiver["type"] = _read_of_type(receiver["type"])
        out["receiver"] = receiver
    if isinstance(out.get("parameters"), list):
        out["parameters"] = [_read_of_parameter(p) if isinstance(p, dict) else p for p in out["parameters"]]
    if "type" in out:
        out["type"] = _read_of_type(out["type"])
    if isinstance(out.get("underlyingType"), dict):
        out["underlyingType"] = {key: _read_of_type(t) for key, t in out["underlyingType"].items()}


def _read_of_parameter(parameter: dict) -> dict:
    out = _keep(parameter, ("name", "type", "extras"))
    if "type" in out:
        out["type"] = _read_of_type(out["type"])
    extras = out.get("extras")
    if isinstance(extras, dict):
        extras = _keep(extras, ("defaultValues",))
        if extras:
            out["extras"] = extras
        else:
            del out["extras"]
    return out


def _read_of_type(node):
    """A type as renderType reads it."""
    if not isinstance(node, dict):
        return node
    keys = TYPE_KEYS.get(node.get("kind"))
    if keys is None:
        keys = NAMED_TYPE_KEYS if node.get("name") not in (None, "") else NAMED_TYPE_KEYS + ("dri",)
    out = _keep(node, keys)
    for key in ("inner", "typeAlias"):
        if key in out:
            out[key] = _read_of_type(out[key])
    if isinstance(out.get("projections"), list):
        out["projections"] = [_read_of_type(p) for p in out["projections"]]
    return out


def keep_first_source_set(page: dict) -> dict:
    """T02: the page with only the first entry of each per-source-set map the template reads only the first
    entry of."""
    page = _signature_first_entries(_first_entry(page, "documentation"))
    return _each_member(page, lambda m: _signature_first_entries(_first_entry(m, "documentation")))


def _first_entry(obj: dict, key: str) -> dict:
    value = obj.get(key)
    if isinstance(value, dict) and len(value) > 1:
        first = next(iter(value))
        return {**obj, key: {first: value[first]}}
    return obj


def _signature_first_entries(obj: dict) -> dict:
    obj = _first_entry(_first_entry(obj, "modifier"), "underlyingType")
    extras = obj.get("extras")
    if isinstance(extras, dict):
        obj = _with(obj, "extras", _first_entry(_first_entry(extras, "annotations"), "additionalModifiers"))
    parameters = obj.get("parameters")
    if isinstance(parameters, list):
        changed = [_with(p, "extras", _first_entry(p["extras"], "defaultValues"))
                   if isinstance(p, dict) and isinstance(p.get("extras"), dict) else p for p in parameters]
        if any(new is not old for new, old in zip(changed, parameters)):
            obj = {**obj, "parameters": changed}
    return obj


def cut_member_briefs(page: dict) -> dict:
    """T03: the page with each member's Description cut after its first </p>."""
    def cut(tags: list) -> list:
        changed = [_cut_after_first_paragraph(t) for t in tags]
        return tags if all(new is old for new, old in zip(changed, tags)) else changed
    return _each_member(page, lambda m: _each_doc_list(m, cut))


def _cut_after_first_paragraph(tag):
    if not isinstance(tag, dict) or tag.get("type") != "Description" or not isinstance(tag.get("text"), str):
        return tag
    text = tag["text"]
    end = text.find("</p>")
    if end < 0 or end + len("</p>") == len(text):
        return tag
    return {**tag, "text": text[:end + len("</p>")]}


def keep_member_descriptions(page: dict) -> dict:
    """T04: the page with only Description tags in member lists, and no member documentation left without one."""
    def descriptions(tags: list) -> list:
        kept = [t for t in tags if not isinstance(t, dict) or t.get("type") == "Description"]
        return tags if len(kept) == len(tags) else kept

    def member(m: dict) -> dict:
        m = _each_doc_list(m, descriptions)
        doc = m.get("documentation")
        if isinstance(doc, dict) and doc and all(isinstance(tags, list) and not tags for tags in doc.values()):
            m = {key: value for key, value in m.items() if key != "documentation"}
        return m
    return _each_member(page, member)


def drop_unprinted(page: dict) -> dict:
    """T05: the page without the final or public modifiers the template prints nothing for, and without the last
    breadcrumb's url, which it never links."""
    page = _each_member(_drop_unprinted_modifier(page), _drop_unprinted_modifier)
    crumbs = page.get("breadcrumbs")
    if isinstance(crumbs, list) and crumbs and isinstance(crumbs[-1], dict) and "url" in crumbs[-1]:
        last = {key: value for key, value in crumbs[-1].items() if key != "url"}
        page = {**page, "breadcrumbs": crumbs[:-1] + [last]}
    return page


def _drop_unprinted_modifier(obj: dict) -> dict:
    modifier = obj.get("modifier")
    # The template prints only the first entry's value, and nothing for these.
    if isinstance(modifier, dict) and modifier and next(iter(modifier.values())) in UNPRINTED_MODIFIERS:
        return {key: value for key, value in obj.items() if key != "modifier"}
    return obj


CHANGES = {
    "T01": (drop_unread_keys, "keys layout.pebble never reads"),
    "T02": (keep_first_source_set, "source-set entries after the first"),
    "T03": (cut_member_briefs, "member summaries after their first paragraph (changes what pages show)"),
    "T04": (keep_member_descriptions, "member tags other than Description"),
    "T05": (drop_unprinted, "final/public modifiers and the last breadcrumb's url"),
}


def trim(raw: bytes, changes) -> tuple:
    """(the row's new bytes, [(change, changed, decoded bytes saved)] in the order applied). Raises Unexpected
    for a row this step should leave alone."""
    try:
        page = json.loads(raw.decode("utf-8"))
    except ValueError as exc:  # UnicodeDecodeError and JSONDecodeError are both ValueErrors
        raise Unexpected(f"is not JSON: {exc}") from None
    if not isinstance(page, dict):
        raise Unexpected("is JSON but not an object, which the template cannot render")
    try:
        compact = serialize(page)
    except UnicodeEncodeError:
        compact = None
    if compact != raw:
        raise Unexpected("is not stored as compact JSON, so writing it back would change more than this step means to")
    before, steps = raw, []
    for name in changes:
        page = CHANGES[name][0](page)
        after = serialize(page)
        steps.append((name, after != before, len(before) - len(after)))
        before = after
    return before, steps


def run(cfg: dict) -> int:
    db_path = cfg["db_path"]
    if not db_path.is_file():
        print(f"error: {db_path} does not exist", file=sys.stderr)
        return 1

    conn = sqlite3.connect(db_path)
    codec = None
    changes, skipped = [], {}
    per_change = defaultdict(lambda: [0, 0])
    try:
        template = conn.execute("SELECT id, content FROM Templates WHERE name = ?", (TEMPLATE_NAME,)).fetchone()
        if template is None:
            print(f"error: no template is named {TEMPLATE_NAME}; nothing was changed", file=sys.stderr)
            return 1
        template_id, text = template
        found = template_code_fingerprint(text.decode("utf-8", errors="replace") if isinstance(text, bytes) else text)
        if found != TEMPLATE_CODE_SHA256:
            print(f"error: {TEMPLATE_NAME}'s Pebble code is not the code these changes were checked against "
                  f"(fingerprint {found}, expected {TEMPLATE_CODE_SHA256}). Check what the template reads now, "
                  "bring this step and TEMPLATE_CODE_SHA256 up to date, and run it again; nothing was changed",
                  file=sys.stderr)
            return 1

        codec = BrotliCodec(load_dictionary(conn))
        rows = conn.execute(
            "SELECT c.path, c.content, c.languageID, c.contentTypeID, c.templateId, ct.compression "
            "FROM Content c JOIN ContentTypes ct ON c.contentTypeID = ct.id WHERE c.templateId = ? ORDER BY c.path",
            (template_id,)).fetchall()
        lengths = {row[0]: len(row[1]) for row in rows}
        bases = [row for row in rows if not is_continuation_path(lengths, row[0])]
        print(f"Trimming {human(len(bases))} {TEMPLATE_NAME} row(s) with {', '.join(cfg['changes'])}...")
        work = [(row[0], reassemble(conn, row[0], row[1]), len(row[1]), *row[2:]) for row in bases]

        def one(item):
            path, full, base_len, language_id, content_type_id, row_template_id, compression = item
            try:
                raw = codec.decompress(full, compression)
            except Exception as exc:  # noqa: BLE001 - one unreadable row is not the run
                return path, None, None, f"could not be read: {exc}"
            try:
                new, steps = trim(raw, cfg["changes"])
            except Unexpected as exc:
                return path, None, None, str(exc)
            if new == raw:
                return path, None, steps, None
            stored = codec.compress(new, compression)
            return path, (base_len, stored, language_id, content_type_id, row_template_id, len(raw) - len(new),
                          len(full) - len(stored)), steps, None

        with ThreadPoolExecutor(max_workers=cfg["workers"]) as pool:
            results = list(pool.map(one, work))
        for path, change, steps, problem in results:
            if problem is not None:
                skipped[path] = problem
                continue
            for name, changed, saved in steps:
                per_change[name][0] += changed
                per_change[name][1] += saved
            if change is not None:
                changes.append((path, *change))

        for name in cfg["changes"]:
            count, saved = per_change[name]
            print(f"  {name} {CHANGES[name][0].__name__}: {human(count)} row(s), {human(saved)} decoded byte(s) of "
                  f"{CHANGES[name][1]}")
        # All or nothing, so exit 1 always means the database was not touched and a rerun starts from the same
        # state: trimming the other rows would leave a half-trimmed database that still exits 1.
        if skipped:
            print(f"error: {len(skipped)} {TEMPLATE_NAME} row(s) cannot be trimmed safely, so nothing was written:",
                  file=sys.stderr)
            for path, problem in sorted(skipped.items()):
                print(f"  {path}: {problem}", file=sys.stderr)
            return 1

        if cfg["dry_run"]:
            print("Dry run: nothing written.")
        elif changes:
            print(f"Backing up {db_path} ...")
            print(f"Backup written to {backup_database(db_path)}")
            conn.execute("BEGIN")
            try:
                for path, base_len, stored, lang, ctid, tid, decoded_saved, stored_saved in changes:
                    replace_row(conn, path, base_len, stored, lang, ctid, tid, lambda m: print(m, file=sys.stderr))
                    if cfg["verbose"]:
                        print(f"  [TRIM] {path}: {human(decoded_saved)} decoded byte(s), {human(stored_saved)} "
                              "stored saved")
                conn.commit()
            except Exception:
                conn.rollback()
                raise
    finally:
        if codec is not None:
            codec.close()
        conn.close()

    if not cfg["dry_run"] and changes:
        print("Vacuuming database...")
        vac = sqlite3.connect(db_path)
        try:
            vac.execute("VACUUM")
        finally:
            vac.close()

    verb = "would change" if cfg["dry_run"] else "changed"
    print(f"\nDone. {verb} {human(len(changes))} row(s), saving {human(sum(c[6] for c in changes))} decoded and "
          f"{human(sum(c[7] for c in changes))} stored byte(s) against the current dictionary.")
    if changes and not cfg["dry_run"]:
        print("Next: pack the database if the build packs it (rebuild_database.py --pack, from PR #38). Do not "
              "re-mint the dictionary after this step; measured, a re-mint made the file larger.")
    return 0


def changes_arg(value: str) -> list:
    names = [name.strip().upper() for name in value.split(",") if name.strip()]
    unknown = [name for name in names if name not in CHANGES]
    if unknown or not names:
        raise argparse.ArgumentTypeError(f"expected a comma-separated list of {', '.join(CHANGES)}; got {value!r}")
    return [name for name in CHANGES if name in names]


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("db_path", type=Path, help="SQLite database to edit in place, e.g. documentation.db")
    p.add_argument("--changes", type=changes_arg, default=list(CHANGES), metavar="T01,T02,...",
                   help="The changes to make, applied in numeric order (default: all five)")
    p.add_argument("--dry-run", action="store_true",
                   help="Do all the work and report what would change, without writing or backing up")
    p.add_argument("--workers", type=int, default=8,
                   help="Threads used to decompress, trim and recompress rows (default: 8)")
    p.add_argument("--verbose", action="store_true", help="Log every row changed")
    return p


def main() -> None:
    args = build_parser().parse_args()
    sys.exit(run({"db_path": args.db_path, "changes": args.changes, "dry_run": args.dry_run,
                  "workers": args.workers, "verbose": args.verbose}))


if __name__ == "__main__":
    main()
