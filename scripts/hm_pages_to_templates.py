#!/usr/bin/env python3
# /// script
# requires-python = ">=3.9"
# dependencies = [
#     "brotli",
#     "Pillow",
# ]
# ///
"""
hm_pages_to_templates.py

Stores each Help+Manual topic page under i/ as JSON plus one shared Pebble
template, instead of as a finished HTML page (ADFA-6618).

Every topic's page, i/X.html, carries the same ~43 KB skin shell around a few
per-topic parts. This step derives that shell from the pages themselves - no
hand-written copy of the skin, so a skin change is picked up on the next run -
stores it once in Templates as a Pebble template, and turns each page row into
the topic's parts as JSON with templateId pointing at the template. The app
already renders such rows (DocumentationContentSource: a Content row with a
non-zero templateId holds JSON, which is rendered through that template before
it is served), so this needs no app change.

How the template is derived:

  1. A topic is a text/html page under --prefix whose JavaScript copy,
     <prefix>jstopics/X.js, exists. That file's hmLoadTopic({...}) fields
     (hmTitle, hmBody, hmPrevLink, ...) are the export's own list of what is
     per-topic. Pages without one (hmftsearch.html, the _hm*.htm templates,
     ...) are not topics and are left alone.
  2. Each page's body is located by its jstopics hmBody, with Help+Manual's
     "\\n\\r" line breaks written as the page's "\\r\\n", and stood in for by a
     marker, so the rest of the page is the skin plus a few small parts.
  3. The pages are aligned line by line. Lines every page shares, in the same
     order, are the template's static text. Between them are the holes.
  4. Each hole is explained by the topic's fields: its text is rewritten as a
     "shape" - static text with {{ field }} placeholders - and a shape is
     accepted for a page only when rendering it with that page's values gives
     the page's text back exactly. When pages need different shapes (a
     disabled "Previous" link on the first topic, say), the template chooses
     between them with {% if field is empty %}.
  5. Where one shape fits every page but a page shows a different value in
     it - a meta description written differently from its jstopics value, a
     caption that is not the topic's title - that value is read out of the
     page's text. It becomes the page's value of the field when no other hole
     reads the field; otherwise it goes in a "part_" key, which the template
     uses where a page has one. A hole none of this explains keeps each
     page's own text in a "part_" key, so a page never depends on a guess.

Each page's parts are stored in its JSON exactly as they appear in that page -
HTML-escaped where the page has them escaped (Help+Manual writes "&quot;" and
"&nbsp;" into meta descriptions) - and inserted with | raw, so nothing is
re-escaped on the way out. The JSON also holds the jstopics fields the page
itself does not show (hmHeader, hmTitlePath), and, under "js_" keys, a
field's jstopics value wherever it differs from the page's, so a later step
can build i/jstopics/X.js from the same row (ADFA-6618 phase 2).

Before anything is written, every converted page is rendered with the real
Pebble engine (scripts/hm_render_check/Render.java, built as the app builds
its engine) and compared with the stored page byte for byte. A page that does
not render back exactly stays as HTML, is named, and is counted - as is any
topic that cannot be converted for another reason. Rendering needs `java`
(17 or later) and the Pebble 4.1.1, unbescape, slf4j-api and Gson jars; they
are looked for in the Gradle and Maven caches, or given with --classpath.

Running it again changes nothing: pages already stored as JSON are rendered
back to HTML with the stored template, so a second run derives the same
template from the same pages, and a topic whose jstopics row is gone takes
its jstopics fields from its JSON. A page the newly derived template cannot
reproduce is written back as HTML rather than left pointing at a template
that no longer fits it.

Phase 2, with --drop-jstopics: the jstopics rows go too. The same run
derives a second template, helpmanual-jstopic.peb, from the jstopics files'
shared frame (hmLoadTopic({...}) with each field's value JavaScript-escaped
from the page's JSON), and stores it in Templates beside the page template,
as plain UTF-8 like the page template. Every topic's stored jstopics file is
rebuilt from its page's JSON with the real engine first, and its row (chunks
included) is dropped only when the rebuilt file has the same field values
after JavaScript unescaping and the same text around them. A topic that does
not match keeps its row, is named, and makes the run exit 1. On later runs,
every topic whose row is gone is built again with the stored template and
checked against the fields its JSON holds; one that differs is named and
fails the run too.

CodeOnTheGo's side of ADFA-6618 builds i/jstopics/X.js on request by
rendering helpmanual-jstopic.peb with i/X.html's JSON, so a database written
this way needs an app that does: with an older app, in-page navigation,
printing and topic popups break. Without --drop-jstopics the jstopics rows
are left alone, the run still reports how many it would rebuild, and the
database works with the app as it is.

It is an option rather than a separate step because both templates come from
one derivation: the "js_" keys the jstopics template reads are written into
the page JSON by the same run, and the check before dropping a row has to use
exactly the JSON being stored. --jstopic-template FILE also writes the
derived jstopics template to FILE.

Runs in one transaction (rolled back on any error), backs the database up
first, and VACUUMs afterwards - all three only when there is something to
change. --dry-run does all the work, the render check included, and reports
what would change without writing.

    uv run scripts/hm_pages_to_templates.py documentation.db --dry-run
    uv run scripts/hm_pages_to_templates.py documentation.db
    uv run scripts/hm_pages_to_templates.py documentation.db --drop-jstopics

Where it goes in the pipeline: LAST among the steps that read or edit i/
pages - update_media_references.py, and ADFA-6589's remove_skin_font_comment.py,
remove_remote_loads.py and check_remote_loads.py. They work on HTML; after
this step the topics' rows hold JSON and the skin lives in Templates.

Recovering: the timestamped backup taken before the first write is the whole
undo.
"""
from __future__ import annotations

import argparse
import difflib
import json
import os
import re
import sqlite3
import statistics
import subprocess
import sys
import tempfile
import threading
from collections import Counter
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from optimize_db_media import (
    BrotliCodec, backup_database, delete_media_row, human, is_continuation_path, load_dictionary, reassemble,
)
from update_media_references import replace_row

PAGE_TEMPLATE = "helpmanual-topic.peb"
JSTOPIC_TEMPLATE = "helpmanual-jstopic.peb"
CONTENT_TYPE = "text/html"

BODY_FIELD = "hmBody"
FILE_KEY = "file"
PART_PREFIX = "part_"
JS_PREFIX = "js_"

# A page must share this much of its lines with the other topics' pages to be
# aligned with them; one that does not has a different shell and stays HTML.
MIN_SHARED_LINES = 0.9

# A hole whose pages need more distinct shapes than this is not explained by
# fields; each page keeps its own text instead.
MAX_SHAPES = 40

# Field values shorter than this are not looked for inside holes: a one- or
# two-character value matches too much by accident. A page whose short value
# does appear is still explained by a shape found on the other pages.
MIN_SUBSTITUTED = 3

# Stands in for the page body while pages are aligned. NUL never occurs in a
# page that is converted (checked), so it cannot collide with page text.
BODY_MARKER = "\x00body\x00"

RENDER_JAVA = Path(__file__).resolve().parent / "hm_render_check" / "Render.java"

# Pebble's newline trimming: the line break right after any tag is dropped.
# Same alternatives, same order, as io.pebbletemplates.pebble.lexer.Syntax.
_TRIMMED_NEWLINE = re.compile("\r\n|\n\r|\r|\n|\u0085|\u2028|\u2029")
_LINE = re.compile(r"[^\r\n]*(?:\r\n|\r|\n)|[^\r\n]+")
_JS_FIELD = re.compile(r'([A-Za-z_$][\w$]*)\s*:\s*"((?:[^"\\]|\\.)*)"', re.S)
_JS_FIELD_SLOT = re.compile(r'([A-Za-z_$][\w$]*)\s*:\s*""')
_JS_STRING = re.compile(r'"((?:[^"\\]|\\.)*)"', re.S)
_JS_ESCAPE = re.compile(r"\\(u[0-9a-fA-F]{4}|x[0-9a-fA-F]{2}|\r\n|.)", re.S)
_JS_SIMPLE = {"n": "\n", "t": "\t", "r": "\r", "b": "\b", "f": "\f", "v": "\v", "0": "\0"}
_IDENTIFIER = re.compile(r"[A-Za-z][A-Za-z0-9_]*")


class BodyForm:
    """How a jstopics hmBody relates to the body in the page."""

    def __init__(self, name: str, to_page, to_jstopic, pebble_filter: str):
        self.name = name
        self.to_page = to_page
        self.to_jstopic = to_jstopic
        self.pebble_filter = pebble_filter


# Help+Manual writes a body's line breaks as "\n\r" in jstopics and "\r\n" in
# the page. The Pebble filter carries literal CR and LF characters: Pebble's
# string literals have no backslash escapes.
BODY_FORMS = (
    BodyForm("lfcr", lambda v: v.replace("\n\r", "\r\n"), lambda v: v.replace("\r\n", "\n\r"),
             ' | replace({"\r\n": "\n\r"})'),
    BodyForm("same", lambda v: v, lambda v: v, ""),
)


def js_unescape(text: str) -> str:
    """The value of a JavaScript string literal's contents."""
    def one(match):
        esc = match.group(1)
        if esc[0] in "ux" and len(esc) > 1:
            return chr(int(esc[1:], 16))
        if esc in ("\r\n", "\n", "\r", "\u2028", "\u2029"):
            return ""  # a line continuation
        return _JS_SIMPLE.get(esc, esc)
    # "\uD83D\uDE00" is one character in JavaScript; join such pairs here too.
    return _JS_ESCAPE.sub(one, text).encode("utf-16", "surrogatepass").decode("utf-16", "surrogatepass")


def parse_jstopic(text: str):
    """(fields, frame) for a jstopics file: its field values, JavaScript-
    unescaped and in file order, and the file with every string literal
    emptied, which is the same for every topic of one export. None when the
    file holds no hmBody."""
    fields = {}
    for name, value in _JS_FIELD.findall(text):
        fields.setdefault(name, js_unescape(value))
    if BODY_FIELD not in fields:
        return None
    return fields, _JS_STRING.sub('""', text)


def hm_html(value: str) -> str:
    """A value as Help+Manual writes it into page markup."""
    return (value.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
            .replace('"', "&quot;").replace("\u00a0", "&nbsp;"))


def pebble_empty(value) -> bool:
    """Pebble's `is empty` test on a string: Java's trim() leaves nothing."""
    return value is None or all(ch <= " " for ch in value)


def split_lines(text: str) -> list:
    """Lines with their endings, split on CR, LF and CRLF only."""
    return _LINE.findall(text)


class Topic:
    def __init__(self, name: str, path: str, page: str, fields: dict, frame: str):
        self.name = name          # "debug-top"
        self.path = path          # "i/debug-top.html"
        self.page = page          # the page as text
        self.fields = fields      # jstopics fields, unescaped
        self.frame = frame        # the jstopics file with its strings emptied

    @property
    def file(self) -> str:
        return self.path.rsplit("/", 1)[-1]


# --- template text -----------------------------------------------------------

def text_seg(text: str) -> tuple:
    return ("text", text)


def tag_seg(tag: str) -> tuple:
    return ("tag", tag)


def var_tag(name: str) -> str:
    return "{{ " + name + " | raw }}"


def emit(segments: list) -> str:
    """Template source for a list of ("text", s) / ("tag", s) segments.

    Static text is made safe for Pebble's lexer: a "{" that would open a
    delimiter ("{{", "{%", "{#") is written as {{ "{" }}, and a line break right
    after a tag is doubled, since Pebble trims one there."""
    escaped = []
    for index, (kind, value) in enumerate(segments):
        if kind == "tag":
            escaped.append((kind, value))
            continue
        following = next((v for _k, v in segments[index + 1:] if v), "")
        start = 0
        for pos, ch in enumerate(value):
            if ch != "{":
                continue
            nxt = value[pos + 1] if pos + 1 < len(value) else following[:1]
            if nxt in ("{", "%", "#"):
                escaped.append(("text", value[start:pos]))
                escaped.append(("tag", '{{ "{" }}'))
                start = pos + 1
        escaped.append(("text", value[start:]))
    out = []
    after_tag = False
    for kind, value in escaped:
        if kind == "tag":
            out.append(value)
            after_tag = True
            continue
        if not value:
            continue
        if after_tag:
            trimmed = _TRIMMED_NEWLINE.match(value)
            if trimmed:
                value = trimmed.group(0) + value
        out.append(value)
        after_tag = False
    return "".join(out)


# --- derivation --------------------------------------------------------------

# A shape is a tuple of tokens: ("t", text) is static text, ("v", key) a
# page's value for key, and ("o", key, part) the value of part where a page
# has one and of key everywhere else.

class Hole:
    """A place where the pages differ, and how the template fills it."""

    def __init__(self, values: dict, context: str):
        self.values = values      # {topic name: its text here, body marked}
        self.context = context    # static text before it, for naming
        self.kind = None          # "shape", "choice" or "part"
        self.branches = []        # [(predicate or None, shape)]
        self.part = None          # key holding pages' own text, for "part"
        self.part_pages = set()

    def keys(self) -> set:
        """Every key the template reads to fill this hole."""
        out = {p[0] for p, _s in self.branches if p}
        for _p, shape in self.branches:
            for token in shape:
                if token[0] != "t":
                    out.update(token[1:])
        if self.part:
            out.add(self.part)
        return out


class Derivation:
    def __init__(self):
        self.template = None
        self.jstopic_template = None
        self.contexts = {}        # {topic name: JSON context}
        self.unfit = {}           # {topic name: reason}
        self.holes = []
        self.fields = []
        self.base = None
        self.body_form = {}


def _shape_of(value: str, lookup: dict, pattern) -> tuple:
    """`value` as a shape: text with each field value found in it replaced by
    a placeholder for that field."""
    if pattern is None:
        return (("t", value),) if value else ()
    tokens, pos = [], 0
    for match in pattern.finditer(value):
        if match.start() > pos:
            tokens.append(("t", value[pos:match.start()]))
        tokens.append(("v", lookup[match.group(0)]))
        pos = match.end()
    if pos < len(value):
        tokens.append(("t", value[pos:]))
    return tuple(tokens)


def _token_value(token: tuple, values: dict) -> str:
    if token[0] == "t":
        return token[1]
    if token[0] == "o" and values.get(token[2]) is not None:
        return values[token[2]]
    return values.get(token[1], "")


def _render_shape(shape: tuple, values: dict) -> str:
    return "".join(_token_value(token, values) for token in shape)


def _shape_segments(shape: tuple) -> list:
    out = []
    for token in shape:
        if token[0] == "t":
            out.append(text_seg(token[1]))
        elif token[0] == "v":
            out.append(tag_seg(var_tag(token[1])))
        else:
            out.append(tag_seg("{{ (" + token[2] + " is null ? " + token[1] + " : " + token[2] + ") | raw }}"))
    return out


def _shape_regex(shape: tuple):
    """A regular expression matching what `shape` renders to with any values,
    capturing each key's value; None for a shape with no keys."""
    parts, groups, seen = [], {}, {}
    for token in shape:
        if token[0] == "t":
            parts.append(re.escape(token[1]))
        elif token[1] in seen:
            parts.append(f"(?P={seen[token[1]]})")
        else:
            group = f"g{len(groups)}"
            seen[token[1]] = group
            groups[group] = token[1]
            parts.append(f"(?P<{group}>.*?)")
    return (re.compile("".join(parts), re.S), groups) if groups else (None, {})


def _predicate_tag(predicate: tuple, first: bool) -> str:
    name, negate = predicate
    return "{% " + ("if " if first else "elseif ") + name + (" is not empty" if negate else " is empty") + " %}"


def _part_name(context: str, taken: set) -> str:
    words = _IDENTIFIER.findall(context[-200:])
    stem = PART_PREFIX + (words[-1] if words else "text")
    name, n = stem, 1
    while name in taken:
        n += 1
        name = f"{stem}{n}"
    taken.add(name)
    return name


def _explain(hole: Hole, variables: dict, patterns: dict, names: list) -> None:
    """Decides how the template fills `hole`; see the module docstring."""
    pages = list(hole.values)
    shapes = {n: _shape_of(hole.values[n], *patterns[n]) for n in pages}
    counts = Counter(shapes[n] for n in pages)
    explained = {}

    def explains(shape):
        if shape not in explained:
            explained[shape] = {n for n in pages if _render_shape(shape, variables[n]) == hole.values[n]}
        return explained[shape]

    chosen = []
    if len(counts) <= MAX_SHAPES:
        remaining = set(pages)
        while remaining:
            candidates = {shapes[n] for n in remaining}
            best = max(candidates, key=lambda s: (len(explains(s) & remaining), counts[s], repr(s)))
            chosen.append(best)
            remaining -= explains(best)
    hole.kind, hole.branches, hole.part_pages = None, [], set()
    if len(chosen) == 1:
        hole.kind = "shape"
        hole.branches = [(None, chosen[0])]
        return
    if chosen:
        main = chosen[0]
        uncovered = set(pages) - explains(main)
        branches = []
        for shape in chosen[1:]:
            must = uncovered & explains(shape)
            if not must:
                continue
            found = None
            for name in names:
                for negate in (False, True):
                    selected = {n for n in pages if pebble_empty(variables[n].get(name)) != negate}
                    if must <= selected <= explains(shape):
                        found = (name, negate)
                        break
                if found:
                    break
            if found is None:
                branches = None
                break
            branches.append((found, shape))
            uncovered -= {n for n in pages if pebble_empty(variables[n].get(found[0])) != found[1]}
        if branches is not None and not uncovered:
            hole.kind = "choice"
            hole.branches = branches + [(None, main)]
            return
    # Nothing chooses between the shapes: the pages the main shape does not
    # explain keep their own text, unless _refine finds their values in it.
    hole.kind = "part"
    if chosen:
        hole.branches = [(None, chosen[0])]
        hole.part_pages = set(pages) - explains(chosen[0])
    else:
        hole.part_pages = set(pages)


def _refine(holes: list, variables: dict, taken: set) -> None:
    """For a hole whose main shape fails on some pages, reads those pages'
    values out of their own text with the shape's pattern: the meta
    description written differently from its jstopics value, say, or a page
    whose caption is not its title.

    A key no other hole reads takes the page's value as it is - it is that
    page's value of the key, in the form the page shows it. A key other holes
    read keeps its value, and this hole reads the page's text from a "part_"
    key on the pages that need one."""
    readers = Counter()
    for hole in holes:
        readers.update(hole.keys())
    for hole in holes:
        if hole.kind != "part" or not hole.branches:
            continue
        main = hole.branches[0][1]
        pattern, groups = _shape_regex(main)
        if pattern is None:
            continue
        found = {}
        for n in hole.part_pages:
            match = pattern.fullmatch(hole.values[n])
            if match is None:
                break
            found[n] = {groups[g]: match.group(g) for g in groups}
        else:
            shape, pending = list(main), {}
            for key in groups.values():
                differs = {n for n in found if found[n][key] != variables[n].get(key)}
                if not differs:
                    continue
                if readers[key] == 1 and key != BODY_FIELD:
                    for n in differs:
                        pending.setdefault(n, {})[key] = found[n][key]
                    continue
                at = next(i for i, token in enumerate(shape) if token[0] != "t" and token[1] == key)
                before = "".join(token[1] for token in shape[:at] if token[0] == "t")
                part = _part_name(hole.context + before, taken)
                shape = [("o", key, part) if token[0] == "v" and token[1] == key else token for token in shape]
                for n in differs:
                    pending.setdefault(n, {})[part] = found[n][key]
            trial = {n: dict(variables[n], **pending.get(n, {})) for n in hole.values}
            if all(_render_shape(tuple(shape), trial[n]) == hole.values[n] for n in hole.values):
                for n, values in pending.items():
                    variables[n].update(values)
                hole.kind, hole.branches, hole.part_pages = "shape", [(None, tuple(shape))], set()


def derive(topics: list) -> Derivation:
    """Derives the page template, each topic's JSON and the jstopics template
    from `topics` (sorted by path, so the result is deterministic)."""
    result = Derivation()
    unfit = result.unfit

    keysets = Counter(tuple(t.fields) for t in topics)
    if not keysets:
        return result
    fields = list(keysets.most_common(1)[0][0])
    result.fields = fields
    seeded, body_text = {}, {}
    for t in topics:
        if tuple(t.fields) != tuple(fields):
            unfit[t.name] = (f"its jstopics file has fields {', '.join(t.fields)}; "
                             f"the other topics have {', '.join(fields)}")
            continue
        if "\x00" in t.page:
            unfit[t.name] = "the page contains a NUL character"
            continue
        body = t.fields[BODY_FIELD]
        for form in BODY_FORMS:
            in_page = form.to_page(body)
            if in_page and t.page.count(in_page) == 1:
                result.body_form[t.name] = form
                body_text[t.name] = in_page
                seeded[t.name] = t.page.replace(in_page, BODY_MARKER)
                break
        else:
            unfit[t.name] = "its jstopics hmBody does not occur exactly once in the page"
    by_name = {t.name: t for t in topics}

    lines = {n: split_lines(s) for n, s in seeded.items()}
    if len(lines) < 2:
        for n in lines:
            unfit[n] = "fewer than two topics to derive a shared template from"
        return result
    sizes = Counter(len(v) for v in lines.values())
    mode = max(sizes, key=lambda size: (sizes[size], -size))
    base_name = next(n for n in lines if len(lines[n]) == mode)
    base = lines[base_name]
    result.base = base_name
    maps = {}
    for n, own in lines.items():
        matcher = difflib.SequenceMatcher(None, base, own, autojunk=False)
        mapping = {}
        for a, b, size in matcher.get_matching_blocks():
            for k in range(size):
                mapping[a + k] = b + k
        shared = len(mapping) / max(len(base), len(own))
        if shared < MIN_SHARED_LINES:
            unfit[n] = f"shares only {shared:.0%} of its lines with the other topics' pages"
            continue
        maps[n] = mapping
    names = sorted(maps)
    if len(names) < 2:
        for n in names:
            unfit[n] = "fewer than two topics to derive a shared template from"
        return result
    anchors = [i for i in range(len(base)) if all(i in maps[n] for n in names)]

    # Static text and holes, in page order.
    pieces = []
    bounds = [-1] + anchors + [len(base)]
    context = ""
    for k in range(len(bounds) - 1):
        a, b = bounds[k], bounds[k + 1]
        if k > 0:
            pieces.append(base[a])
            context = (context + base[a])[-400:]
        values = {}
        for n in names:
            start = maps[n][a] + 1 if a >= 0 else 0
            end = maps[n][b] if b < len(base) else len(lines[n])
            values[n] = "".join(lines[n][start:end])
        if len(set(values.values())) == 1:
            pieces.append(values[names[0]])
            context = (context + values[names[0]])[-400:]
            continue
        split = {n: split_lines(v) for n, v in values.items()}
        counts = {len(v) for v in split.values()}
        subs = ([{n: split[n][j] for n in names} for j in range(len(split[names[0]]))]
                if len(counts) == 1 and counts.pop() > 1 else [values])
        for sub in subs:
            if len(set(sub.values())) == 1:
                pieces.append(sub[names[0]])
                context = (context + sub[names[0]])[-400:]
            else:
                pieces.append(Hole(sub, context))
    holes = [p for p in pieces if isinstance(p, Hole)]
    result.holes = holes

    # Each page's values, in the form its page shows them.
    hole_text = {n: "".join(h.values[n] for h in holes) for n in names}
    variables = {}
    for n in names:
        t = by_name[n]
        own = {FILE_KEY: t.file, BODY_FIELD: BODY_MARKER}
        for f in fields:
            if f == BODY_FIELD:
                continue
            value = t.fields[f]
            escaped = hm_html(value)
            own[f] = escaped if escaped != value and escaped in hole_text[n] else value
        variables[n] = own
    patterns = {}
    for n in names:
        lookup = {}
        for key, value in variables[n].items():
            if key == BODY_FIELD or (len(value) >= MIN_SUBSTITUTED and not pebble_empty(value)):
                lookup.setdefault(value, key)
        ordered = sorted(lookup, key=lambda v: (-len(v), v))
        patterns[n] = (lookup, re.compile("|".join(map(re.escape, ordered))) if ordered else None)
    predicate_names = [FILE_KEY] + [f for f in fields if f != BODY_FIELD]
    for hole in holes:
        _explain(hole, variables, patterns, predicate_names)
    taken = set(fields) | {FILE_KEY}
    _refine(holes, variables, taken)
    for hole in holes:
        if hole.kind == "part":
            hole.part = _part_name(hole.context, taken)

    segments = [tag_seg("{# Help+Manual topic page, derived from the export by OfflineDocumentationTools "
                        "scripts/hm_pages_to_templates.py. Generated: rerun the step, do not edit. #}")]
    used = set()
    for piece in pieces:
        if not isinstance(piece, Hole):
            for i, part in enumerate(piece.split(BODY_MARKER)):
                if i:
                    segments.append(tag_seg(var_tag(BODY_FIELD)))
                    used.add(BODY_FIELD)
                segments.append(text_seg(part))
            continue
        hole = piece
        used.update(hole.keys())
        if hole.kind == "part":
            if hole.branches:
                segments.append(tag_seg("{% if " + hole.part + " is null %}"))
                segments.extend(_shape_segments(hole.branches[0][1]))
                segments.append(tag_seg("{% else %}"))
                segments.append(tag_seg(var_tag(hole.part)))
                segments.append(tag_seg("{% endif %}"))
            else:
                segments.append(tag_seg(var_tag(hole.part)))
        elif hole.kind == "choice":
            for index, (predicate, shape) in enumerate(hole.branches):
                if predicate is None:
                    segments.append(tag_seg("{% else %}"))
                else:
                    segments.append(tag_seg(_predicate_tag(predicate, index == 0)))
                segments.extend(_shape_segments(shape))
            segments.append(tag_seg("{% endif %}"))
        else:
            segments.extend(_shape_segments(hole.branches[0][1]))
    result.template = emit(segments)

    # The JSON: every jstopics field (phase 2 builds jstopics/X.js from it) in
    # the form the page shows it, the file name if the template uses it, and
    # the "part_" keys a page needs.
    for n in names:
        values = dict(variables[n], **{BODY_FIELD: body_text[n]})
        context = {}
        if FILE_KEY in used:
            context[FILE_KEY] = values[FILE_KEY]
        for f in fields:
            context[f] = values[f]
        for key in sorted(k for k in values if k.startswith(PART_PREFIX)):
            context[key] = values[key]
        for hole in holes:
            if hole.kind == "part" and n in hole.part_pages:
                context[hole.part] = hole.values[n].replace(BODY_MARKER, body_text[n])
        result.contexts[n] = context
    _derive_jstopic(result, by_name, names)
    return result


def _derive_jstopic(result: Derivation, by_name: dict, names: list) -> None:
    """The phase-2 template that builds jstopics/X.js from a page's JSON, and
    the "js_" keys it needs where a field's jstopics value is not the page's."""
    forms = Counter(result.body_form[n].name for n in names)
    body_form = next(f for f in BODY_FORMS if f.name == forms.most_common(1)[0][0])

    def jstopic_value(field, value):
        return body_form.to_jstopic(value) if field == BODY_FIELD else value

    needs_override = set()
    for n in names:
        context = result.contexts[n]
        for f in result.fields:
            stored = by_name[n].fields[f]
            if jstopic_value(f, context[f]) != stored:
                context[JS_PREFIX + f] = stored
                needs_override.add(f)
    frames = Counter(by_name[n].frame for n in names if by_name[n].frame)
    if not frames:
        return  # no jstopics file left to take the file's frame from
    frame = frames.most_common(1)[0][0]
    segments, pos = [], 0
    for match in _JS_FIELD_SLOT.finditer(frame):
        field = match.group(1)
        if field not in result.fields:
            continue
        quote = match.end() - 2
        segments.append(text_seg(frame[pos:quote + 1]))
        expression = field + (body_form.pebble_filter if field == BODY_FIELD else "")
        if field in needs_override:
            expression = f"({JS_PREFIX}{field} is null ? ({expression}) : {JS_PREFIX}{field})"
        segments.append(tag_seg("{{ " + expression + " | escape('js') }}"))
        pos = quote + 1
    segments.append(text_seg(frame[pos:]))
    result.jstopic_template = emit(segments)


def jstopic_fields_from_context(context: dict, fields: list, body_form: BodyForm) -> dict:
    """What the phase-2 template builds for each jstopics field, before
    JavaScript escaping - so a topic converted earlier can be re-derived when
    its jstopics row is gone."""
    out = {}
    for f in fields:
        if JS_PREFIX + f in context:
            out[f] = context[JS_PREFIX + f]
        elif f == BODY_FIELD:
            out[f] = body_form.to_jstopic(context.get(f, ""))
        else:
            out[f] = context.get(f, "")
    return out


def context_json(context: dict) -> bytes:
    """A context as stored: compact, UTF-8, the way populate_db.py writes its
    JSON pages."""
    return json.dumps(context, ensure_ascii=False, separators=(",", ":")).encode("utf-8")


# --- the renderer ------------------------------------------------------------

_JARS = (
    ("io.pebbletemplates", "pebble", "4.1.1"),
    ("org.unbescape", "unbescape", None),
    ("org.slf4j", "slf4j-api", None),
    ("com.google.code.gson", "gson", "2.10.1"),
)


def _version_key(version: str) -> tuple:
    return tuple(int(p) if p.isdigit() else -1 for p in re.split(r"[.\-]", version))


def find_classpath(extra=()) -> str:
    """Pebble 4.1.1 (the app's version) and what Render.java needs beside it,
    from the Gradle and Maven caches. Raises naming whatever is missing."""
    home = Path.home()
    roots = [home / ".gradle/caches/modules-2/files-2.1", home / ".m2/repository"]
    found, missing = [], []
    for group, artifact, version in _JARS:
        hits = []
        for root in roots:
            base = root / group / artifact if "gradle" in str(root) else root / group.replace(".", "/") / artifact
            if not base.is_dir():
                continue
            for jar in base.glob(f"{version or '*'}/**/{artifact}-*.jar"):
                ver = jar.name[len(artifact) + 1:-4]
                if (version is None or ver == version) and not ver.endswith(("sources", "javadoc")):
                    hits.append((_version_key(ver), str(jar)))
        if hits:
            found.append(max(hits)[1])
        else:
            missing.append(f"{group}:{artifact}:{version or '*'}")
    if missing:
        raise RuntimeError("cannot find " + ", ".join(missing) + " in the Gradle or Maven cache; pass --classpath")
    return os.pathsep.join(list(extra) + found)


class PebbleHarness:
    """Render.java, kept running: templates are defined over its stdin and
    pages rendered with the engine settings the app uses."""

    def __init__(self, classpath: str, java: str = "java", database: Path = None):
        self._log = tempfile.TemporaryFile()
        command = [java, "-cp", classpath, str(RENDER_JAVA)] + (["--db", str(database)] if database else [])
        self._proc = subprocess.Popen(command, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=self._log)
        self._lock = threading.Lock()

    def _request(self, command: str, name: str, payload: bytes) -> tuple:
        with self._lock:
            try:
                self._proc.stdin.write(f"{command} {name} {len(payload)}\n".encode("ascii") + payload)
                self._proc.stdin.flush()
                header = self._proc.stdout.readline().decode("ascii").split()
            except (BrokenPipeError, OSError):
                header = []
            if len(header) != 3:
                self._log.seek(0)
                raise RuntimeError("the Pebble renderer stopped: "
                                   + self._log.read().decode(errors="replace").strip()[-2000:])
            body = self._proc.stdout.read(int(header[1]))
            return header[0] == "OK", body, int(header[2])

    def define(self, name: str, text: str) -> None:
        ok, body, _ = self._request("T", name, text.encode("utf-8"))
        if not ok:
            raise RuntimeError(body.decode(errors="replace"))

    def render(self, name: str, context: bytes) -> tuple:
        """(True, page bytes, nanoseconds) or (False, error message, 0)."""
        ok, body, nanos = self._request("R", name, context)
        return ok, (body if ok else body.decode(errors="replace")), nanos

    def close(self) -> None:
        try:
            self._proc.stdin.write(b"Q - 0\n")
            self._proc.stdin.close()
            self._proc.wait(timeout=30)
        except Exception:  # noqa: BLE001 - shutting down regardless
            self._proc.kill()
        self._log.close()


def template_text(content) -> str:
    """A Templates row's content: a UTF-8 BLOB as written here, or TEXT if
    something else wrote it - the app reads either."""
    return content if isinstance(content, str) else bytes(content).decode("utf-8")


def first_difference(a: bytes, b: bytes) -> int:
    for i, (x, y) in enumerate(zip(a, b)):
        if x != y:
            return i
    return min(len(a), len(b))


# --- the run -----------------------------------------------------------------

def run(cfg: dict) -> int:
    db_path = cfg["db_path"]
    if not db_path.is_file():
        print(f"error: {db_path} does not exist", file=sys.stderr)
        return 1
    prefix = cfg["prefix"]
    log = (lambda m: print(m)) if cfg["verbose"] else (lambda m: None)

    harness = cfg.get("renderer")
    own_harness = harness is None
    if own_harness:
        try:
            classpath = cfg["classpath"] or find_classpath()
        except RuntimeError as exc:
            print(f"error: {exc}", file=sys.stderr)
            return 1
        harness = PebbleHarness(classpath, cfg["java"])

    conn = sqlite3.connect(db_path)
    codec = None
    changes = []
    try:
        codec = BrotliCodec(load_dictionary(conn))
        stored_template = conn.execute("SELECT id, content FROM Templates WHERE name = ?",
                                       (PAGE_TEMPLATE,)).fetchone()
        stored_id = stored_template[0] if stored_template else None
        stored_text = template_text(stored_template[1]) if stored_template else None
        stored_jstopic = conn.execute("SELECT content FROM Templates WHERE name = ?", (JSTOPIC_TEMPLATE,)).fetchone()
        stored_jstopic_text = template_text(stored_jstopic[0]) if stored_jstopic else None
        rows = conn.execute(
            "SELECT c.path, c.content, c.languageID, c.contentTypeID, c.templateId, ct.compression "
            "FROM Content c JOIN ContentTypes ct ON c.contentTypeID = ct.id "
            "WHERE ct.value = ? AND c.path LIKE ? ORDER BY c.path", (CONTENT_TYPE, prefix + "%")
        ).fetchall()
        lengths = {row[0]: len(row[1]) for row in rows}
        jstopic_rows = conn.execute(
            "SELECT c.path, ct.compression, LENGTH(c.content) FROM Content c "
            "JOIN ContentTypes ct ON c.contentTypeID = ct.id WHERE c.path LIKE ?", (prefix + "jstopics/%",)).fetchall()
        jstopic_lengths = {path: length for path, _c, length in jstopic_rows}
        jstopics = {path: compression for path, compression, _l in jstopic_rows
                    if not is_continuation_path(jstopic_lengths, path)}

        candidates, skipped = [], []
        for path, blob, language_id, type_id, template_id, compression in rows:
            if is_continuation_path(lengths, path) or "/" in path[len(prefix):]:
                continue
            stem = path[len(prefix):].rsplit(".", 1)[0]
            jstopic_path = f"{prefix}jstopics/{stem}.js"
            ours = stored_id is not None and template_id == stored_id
            if template_id and not ours:
                skipped.append((path, f"rendered through another template (templateId {template_id})"))
                continue
            if jstopic_path not in jstopics and not ours:
                continue  # not a topic: hmftsearch.html, _hmXtopic.htm, ...
            candidates.append((path, stem, reassemble(conn, path, blob), len(blob), language_id, type_id,
                               template_id, compression, jstopic_path))
        jstopic_blobs = {}
        for c in candidates:
            if c[8] in jstopics:
                row = conn.execute("SELECT content FROM Content WHERE path = ?", (c[8],)).fetchone()
                jstopic_blobs[c[8]] = (reassemble(conn, c[8], row[0]), jstopics[c[8]], len(row[0]))
        print(f"Found {len(candidates)} Help+Manual topic page(s) under {prefix} "
              f"({sum(1 for c in candidates if c[6])} already stored as JSON).")

        def decode(c):
            path, stem, full, base_len, lang, ctid, tid, compression, jpath = c
            try:
                page = codec.decompress(full, compression)
                jst = None
                if jpath in jstopic_blobs:
                    jst = codec.decompress(*jstopic_blobs[jpath][:2]).decode("utf-8")
                return path, page, jst, None
            except Exception as exc:  # noqa: BLE001 - one unreadable row is not the run
                return path, None, None, f"could not be read: {exc}"

        with ThreadPoolExecutor(max_workers=cfg["workers"]) as pool:
            decoded = list(pool.map(decode, candidates))

        unreadable = [(path, why) for path, _p, _j, why in decoded if why]
        info = {c[0]: c for c in candidates}
        topics, original, unfit = [], {}, {}
        if stored_text is not None:
            harness.define("stored:" + PAGE_TEMPLATE, stored_text)
        for path, page, jst, why in decoded:
            if why:
                continue
            stem = info[path][1]
            if info[path][6]:
                try:
                    context = json.loads(page.decode("utf-8"))
                except ValueError as exc:
                    unreadable.append((path, f"its stored JSON does not parse: {exc}"))
                    continue
                ok, out, _ = harness.render("stored:" + PAGE_TEMPLATE, page)
                if not ok:
                    unreadable.append((path, f"its stored JSON does not render: {out}"))
                    continue
                page = out
                if jst is None:
                    fields = list(k for k in context if k != FILE_KEY and not k.startswith((PART_PREFIX, JS_PREFIX)))
                    form = BODY_FORMS[0]
                    parsed = (jstopic_fields_from_context(context, fields, form), None)
                else:
                    parsed = parse_jstopic(jst)
            else:
                parsed = parse_jstopic(jst) if jst is not None else None
            original[stem] = page
            if parsed is None:
                unfit[stem] = "its jstopics file has no hmBody field"
                continue
            try:
                text = page.decode("utf-8")
            except UnicodeDecodeError as exc:
                unfit[stem] = f"the page is not valid UTF-8 ({exc.reason} at byte {exc.start:,})"
                continue
            frame = parsed[1] if parsed[1] is not None else ""
            topics.append(Topic(stem, path, text, parsed[0], frame))

        derivation = derive(topics)
        unfit.update(derivation.unfit)
        template = derivation.template

        # The render check: each converted page must come back byte for byte.
        nanos = []
        jsons = {}
        if template is not None:
            harness.define(PAGE_TEMPLATE, template)
            for stem, context in derivation.contexts.items():
                data = context_json(context)
                ok, out, took = harness.render(PAGE_TEMPLATE, data)
                if not ok:
                    unfit[stem] = f"the template does not render it: {out}"
                elif out != original[stem]:
                    unfit[stem] = (f"its rendered page differs from the stored one at byte "
                                   f"{first_difference(out, original[stem]):,}")
                else:
                    jsons[stem] = data
                    nanos.append(took)

        # Phase 2: does the jstopics template rebuild each topic's stored file? Compared field by field
        # after JavaScript unescaping, and outside the fields too, so the file is the same program.
        jstopic_template = derivation.jstopic_template or stored_jstopic_text
        droppable, kept = [], {}
        with_row = [t for t in topics if f"{prefix}jstopics/{t.name}.js" in jstopic_blobs]
        if jstopic_template is not None and with_row:
            harness.define(JSTOPIC_TEMPLATE, jstopic_template)
            for t in with_row:
                if t.name not in jsons:
                    kept[t.name] = "its page stays HTML, so nothing can build its jstopics file"
                    continue
                ok, out, _ = harness.render(JSTOPIC_TEMPLATE, jsons[t.name])
                parsed = parse_jstopic(out.decode("utf-8")) if ok else None
                if not ok:
                    kept[t.name] = f"the jstopics template does not render it: {out}"
                elif parsed is None or parsed[0] != t.fields:
                    differ = [f for f in t.fields if parsed is None or parsed[0].get(f) != t.fields[f]]
                    kept[t.name] = f"the built file differs in {', '.join(differ)}"
                elif parsed[1] != t.frame:
                    kept[t.name] = "the built file differs outside its field values"
                else:
                    droppable.append(t.name)
        elif with_row:
            for t in with_row:
                kept[t.name] = "no jstopics template could be derived"
        # Topics whose row is already gone: what the app builds for them must still hold the fields
        # their JSON holds, or in-page navigation to them breaks.
        without_row = [t for t in topics if t not in with_row and t.name in jsons]
        broken = {}
        if without_row:
            if jstopic_template is None:
                broken = {t.name: f"its jstopics row is gone and there is no {JSTOPIC_TEMPLATE}" for t in without_row}
            else:
                harness.define(JSTOPIC_TEMPLATE, jstopic_template)
                for t in without_row:
                    ok, out, _ = harness.render(JSTOPIC_TEMPLATE, jsons[t.name])
                    parsed = parse_jstopic(out.decode("utf-8")) if ok else None
                    if parsed is None or parsed[0] != t.fields:
                        broken[t.name] = "the jstopics file built from its JSON does not hold the JSON's fields"
        orphans = sorted(set(jstopics) - {f"{prefix}jstopics/{t.name}.js" for t in topics}
                         - {c[8] for c in candidates})
        if cfg.get("jstopic_template_out") and derivation.jstopic_template is not None:
            Path(cfg["jstopic_template_out"]).write_text(derivation.jstopic_template, encoding="utf-8", newline="")

        for hole in derivation.holes:
            pages = len(hole.values)
            if hole.kind == "part":
                log(f"  [HOLE] after {hole.context[-60:]!r}: kept as page text ({hole.part}) on "
                    f"{len(hole.part_pages)} of {pages} page(s)")
            elif hole.kind == "choice":
                conds = ", ".join(f"{p[0]} is {'not ' if p[1] else ''}empty" for p, _s in hole.branches if p)
                log(f"  [HOLE] after {hole.context[-60:]!r}: {len(hole.branches)} shapes chosen by {conds}")
            else:
                log(f"  [HOLE] after {hole.context[-60:]!r}: one shape")

        # What changes.
        template_changes = bool(template is not None and jsons and template != stored_text)
        drop = cfg.get("drop_jstopics", False)
        jstopic_changes = bool(drop and droppable and derivation.jstopic_template is not None
                               and derivation.jstopic_template != stored_jstopic_text)
        drops = []
        if drop:
            for stem in droppable:
                jpath = f"{prefix}jstopics/{stem}.js"
                drops.append((jpath, jstopic_blobs[jpath][2], len(jstopic_blobs[jpath][0])))
        for stem, page in original.items():
            c = info[f"{prefix}{stem}.html"] if f"{prefix}{stem}.html" in info else None
            if c is None:
                continue
            path, _stem, full, base_len, lang, ctid, tid, compression, _j = c
            if stem in jsons:
                if tid and codec.decompress(full, compression) == jsons[stem]:
                    continue  # already this JSON; the template renders it the same way
                changes.append(("json", path, base_len, jsons[stem], lang, ctid, compression, len(full),
                                len(page)))
            elif tid:
                # Stored as JSON for a template it no longer fits: back to HTML.
                changes.append(("html", path, base_len, page, lang, ctid, compression, len(full), len(page)))

        def encode(change):
            kind, path, base_len, data, lang, ctid, compression, old_stored, old_decoded = change
            return change + (codec.compress(data, compression),)

        with ThreadPoolExecutor(max_workers=cfg["workers"]) as pool:
            changes = list(pool.map(encode, changes))

        converted = [c for c in changes if c[0] == "json"]
        reverted = [c for c in changes if c[0] == "html"]
        stored_before = sum(c[7] for c in changes)
        stored_after = sum(len(c[9]) for c in changes)
        decoded_before = sum(c[8] for c in changes)
        decoded_after = sum(len(c[3]) for c in changes)

        print(f"Derived a {human(len(template.encode('utf-8')) if template else 0)}-byte page template "
              f"from {len(derivation.contexts)} topic(s): {len(derivation.holes)} hole(s), "
              f"{sum(1 for h in derivation.holes if h.kind == 'choice')} with {{% if %}}, "
              f"{sum(1 for h in derivation.holes if h.kind == 'part')} kept as page text.")
        print(f"{len(jsons)} of {len(original)} topic page(s) render back byte for byte and are stored as JSON; "
              f"{len(unfit)} stay HTML.")
        if nanos:
            ms = sorted(n / 1e6 for n in nanos)
            print(f"Render time per page: median {statistics.median(ms):.2f} ms, worst {ms[-1]:.2f} ms.")
        if with_row:
            print(f"Phase 2 check: the jstopics template rebuilds {len(droppable)} of {len(with_row)} stored "
                  f"jstopics file(s) exactly.")
        elif drop:
            print("No jstopics rows left to drop.")
        if without_row:
            print(f"Phase 2 check: {len(without_row) - len(broken)} of {len(without_row)} topic(s) without a "
                  f"jstopics row get one built from their JSON with the fields it holds.")
        for stem in sorted(broken):
            print(f"  broken: {prefix}jstopics/{stem}.js - {broken[stem]}", file=sys.stderr)
        if drop:
            for stem in sorted(kept):
                print(f"  keeps {prefix}jstopics/{stem}.js - {kept[stem]}", file=sys.stderr)
            for path in orphans:
                print(f"  left alone: {path} - no topic page of that name", file=sys.stderr)
        else:
            for stem in sorted(kept):
                log(f"  [JSTOPIC] {stem}: {kept[stem]}")
        for stem in sorted(unfit):
            print(f"  stays HTML: {prefix}{stem}.html - {unfit[stem]}", file=sys.stderr)
        for path, why in skipped:
            print(f"  skipped {path}: {why}", file=sys.stderr)
        for path, why in unreadable:
            print(f"  skipped {path}: {why}", file=sys.stderr)
        if changes or template_changes:
            print(f"{'Would write' if cfg['dry_run'] else 'Writing'} the template"
                  f"{' (changed)' if template_changes else ' (unchanged)'}, {len(converted)} page(s) as JSON"
                  f"{f' and {len(reverted)} back as HTML' if reverted else ''}: {human(stored_before)} -> "
                  f"{human(stored_after)} stored byte(s), {human(decoded_before)} -> {human(decoded_after)} decoded.")
        if drops or jstopic_changes:
            print(f"{'Would drop' if cfg['dry_run'] else 'Dropping'} {len(drops)} jstopics row(s) "
                  f"({human(sum(d[2] for d in drops))} stored byte(s)), the app building each from its page with "
                  f"the {JSTOPIC_TEMPLATE} template{' (changed)' if jstopic_changes else ' (unchanged)'}.")
        anything = bool(changes or template_changes or drops or jstopic_changes)
        if not anything:
            print("Nothing to change.")

        wrote = False
        if cfg["dry_run"]:
            print("Dry run: nothing written.")
        elif anything:
            print(f"Backing up {db_path} ...")
            print(f"Backup written to {backup_database(db_path)}")
            conn.execute("BEGIN")
            try:
                template_id = stored_id
                if template_changes:
                    conn.execute("INSERT INTO Templates (name, content) VALUES (?, ?) "
                                 "ON CONFLICT(name) DO UPDATE SET content = excluded.content",
                                 (PAGE_TEMPLATE, template.encode("utf-8")))
                    template_id = conn.execute("SELECT id FROM Templates WHERE name = ?",
                                               (PAGE_TEMPLATE,)).fetchone()[0]
                for kind, path, base_len, data, lang, ctid, compression, _os, _od, stored in changes:
                    new_tid = template_id if kind == "json" else 0
                    replace_row(conn, path, base_len, stored, lang, ctid, new_tid,
                                lambda m: print(m, file=sys.stderr))
                    conn.execute("UPDATE Content SET templateId = ? WHERE path = ?", (new_tid, path))
                    log(f"  [{kind.upper()}] {path}")
                if jstopic_changes:
                    conn.execute("INSERT INTO Templates (name, content) VALUES (?, ?) "
                                 "ON CONFLICT(name) DO UPDATE SET content = excluded.content",
                                 (JSTOPIC_TEMPLATE, derivation.jstopic_template.encode("utf-8")))
                for jpath, base_len, _stored in drops:
                    delete_media_row(conn, jpath, base_len)
                    log(f"  [DROP] {jpath}")
                conn.commit()
                wrote = True
            except Exception:
                conn.rollback()
                raise
    finally:
        if codec is not None:
            codec.close()
        conn.close()
        if own_harness:
            harness.close()

    if wrote:
        print("Vacuuming database...")
        vac = sqlite3.connect(db_path)
        try:
            vac.execute("VACUUM")
        finally:
            vac.close()
    if unreadable:
        print(f"error: {len(unreadable)} row(s) could not be read and were left as they were (listed above).",
              file=sys.stderr)
    failed_drops = bool(cfg.get("drop_jstopics") and kept)
    if failed_drops:
        print(f"error: {len(kept)} jstopics row(s) kept because the template does not rebuild them (listed above).",
              file=sys.stderr)
    if broken:
        print(f"error: {len(broken)} topic(s) without a jstopics row would get a wrong one (listed above).",
              file=sys.stderr)
    return 1 if unreadable or failed_drops or broken else 0


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("db_path", type=Path, help="SQLite database to edit in place, e.g. documentation.db")
    p.add_argument("--dry-run", action="store_true",
                   help="Do all the work, the render check included, and report what would change without writing")
    p.add_argument("--workers", type=int, default=8,
                   help="Threads used to decompress and recompress rows (default: 8)")
    p.add_argument("--verbose", action="store_true", help="Describe every hole and log every page written")
    p.add_argument("--prefix", default="i/", help="Where the Help+Manual export is stored (default: i/)")
    p.add_argument("--classpath", default=os.environ.get("HM_RENDER_CLASSPATH"),
                   help="Classpath for Render.java: Pebble 4.1.1, unbescape, slf4j-api and Gson jars "
                        "(default: $HM_RENDER_CLASSPATH, else found in the Gradle or Maven cache)")
    p.add_argument("--java", default="java", help="The java launcher, 17 or later (default: java)")
    p.add_argument("--jstopic-template", type=Path, metavar="FILE",
                   help="Also write the phase-2 template that builds jstopics/X.js from a page's JSON to FILE")
    p.add_argument("--drop-jstopics", action="store_true",
                   help="Phase 2: store the jstopics template and drop every jstopics row it rebuilds exactly. "
                        "Needs an app that builds jstopics/X.js from the template")
    return p


def main() -> None:
    args = build_parser().parse_args()
    sys.exit(run({"db_path": args.db_path, "dry_run": args.dry_run, "workers": args.workers,
                  "verbose": args.verbose, "prefix": args.prefix, "classpath": args.classpath,
                  "java": args.java, "jstopic_template_out": args.jstopic_template,
                  "drop_jstopics": args.drop_jstopics}))


if __name__ == "__main__":
    main()
