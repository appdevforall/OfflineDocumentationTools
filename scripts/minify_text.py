#!/usr/bin/env python3
"""
Minifies the documentation's text rows and HTML templates without changing what
a page shows (ADFA-6627).

What changes:
- Comments that carry a copyright or licence notice stay, in every kind of
  row: copies must keep them.
- HTML (Content rows of type text/html that are not templated): comments go,
  apart from IE conditional comments, and apart from a comment whose removal
  would join the text on its two sides (which could make a tag or a character
  reference). Each run of whitespace in text becomes
  one space, or one newline where the run held a line break, and each run
  between a tag's attributes becomes one space. A browser collapses whitespace
  the same way except where it is preserved, so nothing changes inside <pre>,
  <listing>, the raw-text elements (<script>, <style>, <textarea>, <title> and
  the like), an element with xml:space="preserve", or an element that a
  stylesheet in the database, a <style> in the page or its own style attribute
  gives a preserving white-space. Form feeds, which a browser draws, and CDATA
  sections stay as they are. Attribute values are never touched.
- HTML templates (Templates rows that only text/html rows use): the same, with
  Pebble's {{ }}, {% %} and {# #} tags left whole, and comments that hold one,
  or follow one directly, kept. Pebble drops the one newline (\r\n, \n\r, \r
  or \n) right after a tag, so a run that follows a tag keeps that newline, and
  a run that does not start with one becomes a space, never a newline Pebble
  would then drop.
- SVG: comments go, and so does whitespace-only text outside <text>, <tspan>,
  <textPath>, <title>, <desc>, <style>, <script>, <foreignObject> and
  xml:space="preserve" elements. Runs between attributes become one space.
- CSS: comments go, apart from /*! ones, leaving /**/ where two tokens would
  otherwise touch; whitespace runs become one space, and go next to { } ; , (
  and ). Strings, url() and escapes, with the space that ends a hex escape, are
  left alone. A stylesheet that cannot be read stops the run before anything is
  written.
- JS: terser re-prints each script with no compression and no renaming,
  keeping /*! and notice comments and the pipeline's own ADFA- headers. A
  script terser cannot parse is left as it is.

Templated rows hold JSON for Pebble; collapsing their whitespace saved nothing
measurable, so they are left alone, as are templates used by anything but HTML.

Measured on the 2026-10-18 database: the step takes 31.9 MB out of the decoded
text, but the shared Brotli dictionary was trained on the pages as they were, so
against it the minified HTML compresses 0.9 MB worse. Run remint_dictionary.py
(ProcessDocs/ProcessKotlinDocs/ProcessKotlinWebsiteJSON) straight after this
step, then verify_remint_dictionary.py. Packed as the pipeline packs it, the file
then comes out 794,624 bytes smaller, and its .br 814,295 bytes smaller, than a
re-mint alone leaves it.

Pipeline position: after every step that writes page content, and before the
dictionary re-mint and rebuild_database.py.

Usage:
    ./minify_text.py documentation.db [--dry-run] [--workers N] [--verbose]
"""
from __future__ import annotations

import argparse
import re
import shutil
import sqlite3
import subprocess
import sys
from collections import defaultdict
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from optimize_db_media import (
    BrotliCodec, backup_database, human, is_continuation_path, load_dictionary, reassemble,
)
from update_media_references import replace_row

HTML, SVG, CSS, JS = "text/html", "image/svg+xml", "text/css", "text/javascript"
TYPES = (HTML, SVG, CSS, JS)

# terser keeps these comments; ADFA- matches the headers pipeline steps write
# into their own scripts, which later runs of those steps look for.
TERSER_COMMENTS = r"/^!|@preserve|@license|@lic|@cc_on|copyright|licen[cs]e|\(c\)|all rights reserved|\u00a9|ADFA-/i"
# A comment that carries a copyright or licence notice stays, in every kind of row: Apache-2.0,
# MIT and Oracle's headers all require the notice in copies.
_NOTICE = re.compile(rb"copyright|licen[cs]e|@lic|\(c\)|all rights reserved|\xc2\xa9", re.I)

# Form feed is HTML whitespace but not CSS white space: Chrome draws it, so no run takes it in.
_COLLAPSIBLE = re.compile(rb"[ \t\n\r]+")
# Pebble's Syntax.POSSIBLE_NEW_LINE, the newline it drops right after a tag. It also drops U+0085,
# U+2028 and U+2029, but no run holds those.
_PEBBLE_NEWLINE = re.compile(rb"\r\n|\n\r|\r|\n")

# HTML ends a comment at its first "-->" or "--!>", "<!-->" and "<!--->" are
# whole comments, and an unclosed one runs to the end of the document.
_COMMENT = rb"<!--(?:-?>|(?!-?>)(?:(?!--!?>).)*(?:--!?>|\Z))"
_COMMENT_TOKEN = re.compile(_COMMENT, re.S)
_HTML_WS = b" \t\n\r\f"
_HTML_WS_BYTES = (b" ", b"\t", b"\n", b"\r", b"\f")
_ATTR_PART = re.compile(rb"\"[^\"]*\"|'[^']*'|[^\"' \t\n\r\f]+|[ \t\n\r\f]+|.", re.S)
_ATTR = re.compile(rb"(?:^|[ \t\n\r\f])(class|id|style)[ \t\n\r\f]*=[ \t\n\r\f]*(?:\"([^\"]*)\"|'([^']*)'|([^ \t\n\r\f\"'>]+))", re.I)

# Elements whose content the parser takes as text up to their end tag.
RAW_TEXT = {b"script", b"style", b"textarea", b"title", b"xmp", b"iframe", b"noembed", b"noframes", b"noscript",
            b"plaintext"}
PRESERVING_TAGS = {b"pre", b"listing"}
# Elements the parser closes on its own when an ancestor's end tag comes.
IMPLIED_END = {b"p", b"li", b"dd", b"dt", b"option", b"optgroup", b"rb", b"rp", b"rt", b"rtc"}
VOID = {b"area", b"base", b"br", b"col", b"embed", b"hr", b"img", b"input", b"link", b"meta", b"param", b"source",
        b"track", b"wbr"}

_PRESERVING_VALUE = (rb"white-space(?:-collapse)?\s*:\s*(?:pre|pre-wrap|pre-line|break-spaces|preserve|"
                     rb"preserve-breaks|preserve-spaces)\b")
_PRESERVING_DECL = re.compile(_PRESERVING_VALUE, re.I)
_CSS_STRING = re.compile(rb"\"(?:\\.|[^\"\\\n])*\"|'(?:\\.|[^'\\\n])*'")
# At-rules whose blocks hold style rules; any other at-rule's block holds none.
_GROUPING_AT_RULES = (b"@media", b"@supports", b"@layer", b"@container", b"@scope", b"@document", b"@-moz-document")
_MATCHES_ANY = re.compile(rb":(?:is|where|matches|any|-webkit-any|-moz-any)\(", re.I)
# A CSS escape: up to six hex digits and the one whitespace that may end them, or any other character.
_CSS_ESCAPE_TEXT = rb"\\(?:([0-9A-Fa-f]{1,6})(?:\r\n|[ \t\n\r\f])?|([^\n\r\f0-9A-Fa-f]))"
_CSS_ESCAPE = re.compile(_CSS_ESCAPE_TEXT)
_CSS_NAME = (rb"-?(?:[_A-Za-z\x80-\xff]|\\(?:[0-9A-Fa-f]{1,6}(?:\r\n|[ \t\n\r\f])?|[^\n\r\f0-9A-Fa-f]))"
             rb"(?:[\w\x80-\xff-]|\\(?:[0-9A-Fa-f]{1,6}(?:\r\n|[ \t\n\r\f])?|[^\n\r\f0-9A-Fa-f]))*")
_CLASS_NAME = re.compile(rb"\.(" + _CSS_NAME + rb")")
_ID_NAME = re.compile(rb"#(" + _CSS_NAME + rb")")
_CSS_COMMENT = re.compile(rb"/\*.*?(?:\*/|\Z)", re.S)
_PSEUDO_ELEMENT = re.compile(rb"::?(?:before|after|first-line|first-letter|marker|placeholder|selection|backdrop|"
                             rb"file-selector-button|cue|part\(|slotted\()", re.I)
_STYLE_BLOCK = re.compile(rb"<style\b[^>]*>(.*?)</style\s*>", re.S | re.I)

# A Pebble tag, its quoted strings included, so "}}" inside one does not end it.
_PEBBLE_TAG = re.compile(
    rb"\{\{(?:\"(?:\\.|[^\"\\])*\"|'(?:\\.|[^'\\])*'|[^\"'}]|\}(?!\}))*\}\}"
    rb"|\{%(?:\"(?:\\.|[^\"\\])*\"|'(?:\\.|[^'\\])*'|[^\"'%]|%(?!\}))*%\}"
    rb"|\{#.*?#\}",
    re.S,
)
# A Pebble tag stands in the markup as \x00<n>\x01 while it is minified.
_PLACEHOLDER = re.compile(rb"\x00(\d+)\x01")

SVG_PRESERVING = {b"text", b"tspan", b"textpath", b"title", b"desc", b"style", b"script", b"foreignobject"}
_XML_TOKEN = re.compile(
    rb"(?P<comment><!--.*?-->)"
    rb"|(?P<decl><!\[CDATA\[.*?\]\]>|<!(?:[^>\[]|\[[^\]]*\])*>|<\?.*?\?>)"
    rb"|(?P<endtag></(?P<end>[^\s/>]+)\s*>)"
    rb"|<(?P<start>[^\s/>!?]+)(?P<attrs>(?:[^>\"']|\"[^\"]*\"|'[^']*')*?)(?P<close>/?)>",
    re.S,
)
_XML_SPACE_PRESERVE = re.compile(rb"\bxml:space\s*=\s*[\"']?preserve\b", re.I)


def collapse(run: bytes) -> bytes:
    """One space, or one newline where the run held a line break."""
    return b"\n" if b"\n" in run or b"\r" in run else b" "


def after_pebble_tag(run: bytes) -> bytes:
    """A run that follows a Pebble tag, minified so the rendered page keeps the same whitespace."""
    newline = _PEBBLE_NEWLINE.match(run)
    first = newline.group(0) if newline else b""
    rest = run[len(first):]
    if not rest:
        return first
    # Pebble drops a newline only right after a tag, so with none dropped the
    # rest must not become one.
    return first + (collapse(rest) if first else b" ")


def collapse_runs(text: bytes) -> bytes:
    def one(m):
        if m.start() > 0 and text[m.start() - 1:m.start()] == b"\x01":
            return after_pebble_tag(m.group(0))
        return collapse(m.group(0))
    return _COLLAPSIBLE.sub(one, text)


class Preserving:
    """What a stylesheet gives a preserving white-space: tags, classes and ids, or the whole document.

    For each rule that preserves white-space (nesting resolved), the content of
    the outermost element its selector names is kept: anything a descendant or
    child combinator reaches is inside it. A sibling combinator reaches outside,
    so naming starts after the last one. A rule for a pseudo-element only styles
    generated content and is left out. Matching on any one name, regardless of
    the rest of the selector, keeps more than a browser would, never less; a rule
    naming nothing but html, body, * or :root keeps the whole document.
    """

    def __init__(self):
        self.tags, self.classes, self.ids = set(), set(), set()
        self.whole = False

    def copy(self) -> Preserving:
        p = Preserving()
        p.tags, p.classes, p.ids, p.whole = set(self.tags), set(self.classes), set(self.ids), self.whole
        return p

    def add_css(self, css: bytes) -> None:
        for selector in preserving_selectors(css):
            self.add_selector(selector)

    def add_selector(self, selector: bytes) -> None:
        parts = compounds_of(selector.strip())
        if not parts or _PSEUDO_ELEMENT.search(parts[-1][1]):
            return
        start = max((i for i, (comb, _c) in enumerate(parts) if comb in (b"+", b"~")), default=0)
        for _comb, compound in parts[start:]:
            tag, classes, ids, inner = compound_names(compound)
            if classes or ids:
                self.classes.update(c.lower() for c in classes)
                self.ids.update(i.lower() for i in ids)
                return
            if tag is not None and tag not in (b"html", b"body"):
                self.tags.add(tag)
                return
            if tag is None and any(inner):
                self.tags.update(inner[0] - {b"html", b"body"})
                self.classes.update(c.lower() for c in inner[1])
                self.ids.update(i.lower() for i in inner[2])
                return
        self.whole = True

    def matches(self, name: bytes, attrs: bytes) -> bool:
        if name in PRESERVING_TAGS or name in self.tags:
            return True
        if b"\x00" in attrs:
            return True  # a template fills some of it in, so it could be anything
        if _XML_SPACE_PRESERVE.search(attrs):
            return True
        for m in _ATTR.finditer(attrs):
            key, value = m.group(1).lower(), m.group(2) or m.group(3) or m.group(4) or b""
            # Lower case on both sides: a page in quirks mode matches classes and ids that way.
            if key == b"style" and _PRESERVING_DECL.search(value):
                return True
            if key == b"class" and self.classes.intersection(value.lower().split()):
                return True
            if key == b"id" and value.lower() in self.ids:
                return True
        return False


def preserving_selectors(css: bytes) -> list:
    """The full selector, nesting resolved, of each rule in css that gives a preserving white-space."""
    css = _CSS_STRING.sub(b'""', _CSS_COMMENT.sub(b" ", css))
    found, stack, start = [], [], 0
    for i, c in enumerate(css):
        if c == 0x7B:  # {
            stack.append(css[start:i].strip())
            start = i + 1
        elif c in (0x3B, 0x7D):  # ; }
            if stack and _PRESERVING_DECL.search(css[start:i]):
                found.extend(expand_nesting(stack))
            if c == 0x7D and stack:
                stack.pop()
            start = i + 1
    return found


def expand_nesting(stack: list) -> list:
    """The selectors a stack of nested preludes stands for; none inside an at-rule that holds no style rules."""
    selectors = [b""]
    for prelude in stack:
        if prelude.startswith(b"@"):
            if prelude.lower().startswith(_GROUPING_AT_RULES):
                continue
            return []  # @keyframes, @font-face, @page and the like
        nested = []
        for parent in selectors:
            for child in split_top_level(prelude, b","):
                child = child.strip()
                if b"&" in child:
                    nested.append(child.replace(b"&", b":is(" + parent + b")" if parent else b""))
                else:
                    nested.append(parent + b" " + child if parent else child)
        selectors = nested[:256]
    return [s for s in selectors if s]


def compounds_of(selector: bytes) -> list:
    """(combinator before it, compound) for each compound selector, at the top level of selector."""
    parts, comb, buf, depth, quote, i = [], b" ", bytearray(), 0, None, 0
    while i < len(selector):
        ch = selector[i:i + 1]
        i += 1
        if ch == b"\\" and not quote:
            escape = _CSS_ESCAPE.match(selector, i - 1)
            buf += escape.group(0) if escape else ch
            i += len(escape.group(0)) - 1 if escape else 0
        elif quote:
            buf += ch
            quote = None if ch == quote else quote
        elif ch in (b"'", b'"'):
            quote = ch
            buf += ch
        elif ch in (b"(", b"["):
            depth += 1
            buf += ch
        elif ch in (b")", b"]"):
            depth = max(0, depth - 1)
            buf += ch
        elif depth == 0 and ch in (b" ", b"\t", b"\n", b"\r", b"\f", b">", b"+", b"~"):
            if buf:
                parts.append((comb, bytes(buf)))
                buf, comb = bytearray(), b" "
            if ch in (b">", b"+", b"~"):
                comb = ch
        else:
            buf += ch
    if buf:
        parts.append((comb, bytes(buf)))
    return parts


def compound_names(compound: bytes) -> tuple:
    """(tag or None, classes, ids) a compound names outside its pseudo-classes, and (tags, classes, ids)
    named inside its :is(), :where() and the like."""
    outside = re.sub(rb"\[[^\]]*\]", b"", strip_pseudo_classes(compound))
    tag = re.match(rb"[A-Za-z][A-Za-z0-9-]*", outside)
    classes = {css_unescape(c) for c in _CLASS_NAME.findall(outside)}
    ids = {css_unescape(i) for i in _ID_NAME.findall(outside)}
    inner = (set(), set(), set())
    for m in _MATCHES_ANY.finditer(compound):
        depth, i = 1, m.end()
        while i < len(compound) and depth:
            depth += {b"(": 1, b")": -1}.get(compound[i:i + 1], 0)
            i += 1
        args = re.sub(rb"\[[^\]]*\]", b"", compound[m.end():i - 1])
        inner[0].update(t.lower() for t in re.findall(rb"(?:^|[\s,>+~(])([A-Za-z][A-Za-z0-9-]*)", args))
        inner[1].update(css_unescape(c) for c in _CLASS_NAME.findall(args))
        inner[2].update(css_unescape(i) for i in _ID_NAME.findall(args))
    return (tag.group(0).lower() if tag else None), classes, ids, inner


def css_unescape(name: bytes) -> bytes:
    """A CSS identifier with its escapes decoded, so .sm\\:pre names the class "sm:pre"."""
    def one(m):
        if m.group(1) is None:
            return m.group(2)
        return chr(min(int(m.group(1), 16), 0x10FFFF)).encode("utf-8", "replace")
    return _CSS_ESCAPE.sub(one, name)


def split_top_level(text: bytes, sep: bytes) -> list:
    """Splits on sep outside (), [] and quotes."""
    parts, depth, quote, start, skip = [], 0, None, 0, 0
    for i, c in enumerate(text):
        ch = bytes([c])
        if skip:
            skip -= 1
        elif ch == b"\\":
            escape = _CSS_ESCAPE.match(text, i)
            skip = len(escape.group(0)) - 1 if escape else 0
        elif quote:
            if ch == quote:
                quote = None
        elif ch in (b"'", b'"'):
            quote = ch
        elif ch in (b"(", b"["):
            depth += 1
        elif ch in (b")", b"]"):
            depth = max(0, depth - 1)
        elif ch == sep and depth == 0:
            parts.append(text[start:i])
            start = i + 1
    parts.append(text[start:])
    return [p for p in parts if p.strip()]


def strip_pseudo_classes(compound: bytes) -> bytes:
    out, i = bytearray(), 0
    while i < len(compound):
        escape = _CSS_ESCAPE.match(compound, i) if compound[i:i + 1] == b"\\" else None
        if escape:
            out += escape.group(0)
            i += len(escape.group(0))
        elif compound[i:i + 1] == b":":
            i += 1
            while i < len(compound) and re.match(rb"[\w-]", compound[i:i + 1]):
                i += 1
            if compound[i:i + 1] == b"(":
                depth = 0
                while i < len(compound):
                    depth += {b"(": 1, b")": -1}.get(compound[i:i + 1], 0)
                    i += 1
                    if depth == 0:
                        break
        else:
            out += compound[i:i + 1]
            i += 1
    return bytes(out)


def scan_tag(page: bytes, j: int):
    """Reads a tag from its name at j as the HTML tokenizer does: (end, name, parts), where parts are its
    whitespace runs and attributes in order, or None if the page ends inside it. A quote opens a value only
    right after "=", and a value runs to its closing quote, past any ">" in it."""
    n, k = len(page), j
    while k < n and page[k] not in _HTML_WS + b"/>":
        k += 1
    name, parts, j = page[j:k], [], k
    while j < n:
        c = page[j]
        if c in _HTML_WS:
            k = j
            while k < n and page[k] in _HTML_WS:
                k += 1
            parts.append(page[j:k])
            j = k
        elif c == 0x3E:  # >
            return j + 1, name, parts
        elif c == 0x2F:  # /
            parts.append(b"/")
            j += 1
        else:
            k = j + 1  # a name may start with "=" or a quote, both parse errors the tokenizer allows
            while k < n and page[k] not in _HTML_WS + b"/>=":
                k += 1
            m = k
            while m < n and page[m] in _HTML_WS:
                m += 1
            if m < n and page[m] == 0x3D:  # =
                m += 1
                while m < n and page[m] in _HTML_WS:
                    m += 1
                if m < n and page[m] in (0x22, 0x27):
                    close = page.find(page[m:m + 1], m + 1)
                    if close < 0:
                        return None
                    m = close + 1
                else:
                    while m < n and page[m] not in _HTML_WS + b">":
                        m += 1
                k = m
            parts.append(page[j:k])
            j = k
    return None


def next_markup(page: bytes, pos: int):
    """The next markup at or after pos, as (kind, start, end, name, parts), or None. kind is "comment",
    "decl" (a doctype or bogus comment, which ends at the first >, or a CDATA section, kept whole to
    its ]]> as inline SVG and MathML read it), "start", "end", or "rest" when the page ends inside a
    tag."""
    i = page.find(b"<", pos)
    while i >= 0:
        after = page[i + 1:i + 2]
        if page.startswith(b"<!--", i):
            return "comment", i, _COMMENT_TOKEN.match(page, i).end(), None, None
        if page.startswith(b"<![CDATA[", i):
            close = page.find(b"]]>", i)
            return "decl", i, len(page) if close < 0 else close + 3, None, None
        if after in (b"!", b"?") or (after == b"/" and not page[i + 2:i + 3].isalpha()):
            close = page.find(b">", i)
            return "decl", i, len(page) if close < 0 else close + 1, None, None
        if after.isalpha() or after == b"/":
            scanned = scan_tag(page, i + (2 if after == b"/" else 1))
            if scanned is None:
                return "rest", i, len(page), None, None
            return ("end" if after == b"/" else "start"), i, *scanned
        i = page.find(b"<", i + 1)
    return None


def raw_text_end(page: bytes, pos: int, name: bytes) -> int:
    end = re.compile(rb"</" + re.escape(name) + rb"[\s/>]", re.I).search(page, pos)
    return end.start() if end else len(page)


def minify_tag(lead: bytes, name: bytes, parts: list) -> bytes:
    """A tag with each run between its attributes made one space, and none before its >."""
    out = []
    for part in parts:
        if part[0] in _HTML_WS:
            out.append(after_pebble_tag(part) if out and out[-1].endswith(b"\x01") else b" ")
        else:
            out.append(part)
    while out and out[-1][0] in _HTML_WS:
        out.pop()
    return lead + name + b"".join(out) + b">"


def matching_end(page: bytes, pos: int, name: bytes) -> int:
    """Where the end tag closing an element named name, opened before pos, starts; the end of page if none does.

    The parser ignores that end tag while an element opened inside is still open (a td or table
    blocks its scope, and a div blocks an inline element's end tag), and the element, with its
    white-space, goes on. So unless everything opened inside has closed, or is one the parser closes
    on its own, the rest of the page counts as inside.
    """
    depth, inner = 1, []
    while True:
        token = next_markup(page, pos)
        if token is None or token[0] == "rest":
            return len(page)
        kind, start, pos, tag, _parts = token
        tag = (tag or b"").lower()
        if kind == "start" and tag in RAW_TEXT:
            pos = raw_text_end(page, pos, tag)
        elif kind == "start" and tag == name:
            depth += 1
        elif kind == "start" and tag not in VOID:
            inner.append(tag)
        elif kind == "end" and tag == name:
            depth -= 1
            if depth == 0:
                return start if all(t in IMPLIED_END for t in inner) else len(page)
        elif kind == "end" and tag in inner:
            del inner[len(inner) - 1 - inner[::-1].index(tag):]


def minify_html(page: bytes, preserving: Preserving, template: bool = False) -> bytes:
    """The page with comments and collapsible whitespace minified; see the module docstring."""
    tags = []
    if template:
        def hide(m):
            tags.append(m.group(0))
            return b"\x00%d\x01" % (len(tags) - 1)
        page = _PEBBLE_TAG.sub(hide, page)

    local = preserving
    blocks = _STYLE_BLOCK.findall(page)
    if blocks:
        local = preserving.copy()
        for css in blocks:
            local.add_css(css)
    if local.whole:
        out = page
    else:
        out = _minify_html_body(page, local, template)
    if template:
        out = _PLACEHOLDER.sub(lambda m: tags[int(m.group(1))], out)
    return out


def _minify_html_body(page: bytes, preserving: Preserving, template: bool) -> bytes:
    out, pos, n = [], 0, len(page)

    def text(chunk: bytes) -> None:
        chunk = collapse_runs(chunk)
        # Comments dropped between runs leave them side by side; a run right after a Pebble tag
        # stays whole, since Pebble drops its first newline.
        if (out and chunk[:1] in (b" ", b"\n") and out[-1][-1:] in (b" ", b"\n")
                and not out[-1].rstrip(b" \t\n\r").endswith(b"\x01")):
            chunk = chunk[1:]
        if chunk:
            out.append(chunk)

    while pos < n:
        token = next_markup(page, pos)
        stop = token[1] if token else n
        if stop > pos:
            text(page[pos:stop])
        if token is None:
            break
        kind, start, pos, name, parts = token
        if kind == "comment":
            comment, before, after = page[start:pos], (out[-1][-1:] if out else b""), page[pos:pos + 1]
            keep = (comment.startswith(b"<!--[if") or comment.startswith(b"<!--<![endif]") or _NOTICE.search(comment)
                    # Pebble would drop the newline that follows the comment if the comment went.
                    or (template and (b"\x00" in comment or before == b"\x01"))
                    # The text on both sides would join, and could make a tag or a character reference.
                    or not (before in (b"", b">") or before in _HTML_WS_BYTES or after in (b"", b"<") or after in _HTML_WS_BYTES))
            if keep:
                out.append(comment)
            continue
        if kind in ("decl", "rest"):
            out.append(page[start:pos])
            continue
        out.append(minify_tag(b"<" if kind == "start" else b"</", name, parts))
        if kind == "end":
            continue
        lower = name.lower()
        if lower in RAW_TEXT:
            stop = raw_text_end(page, pos, lower)
        elif lower not in VOID and preserving.matches(lower, b"".join(parts)):
            stop = matching_end(page, pos, lower)
        else:
            continue
        out.append(page[pos:stop])
        pos = stop
    return b"".join(out)


def minify_svg(svg: bytes) -> bytes:
    """The SVG with comments and insignificant whitespace removed; see the module docstring."""
    out, pos, n = [], 0, len(svg)
    while pos < n:
        m = _XML_TOKEN.search(svg, pos)
        stop = m.start() if m else n
        text = svg[pos:stop]
        if text.strip(b" \t\n\r"):
            out.append(text)
        if m is None:
            break
        pos = m.end()
        if m.group("comment"):
            continue
        if m.group("decl") or m.group("endtag"):
            out.append(m.group(0) if m.group("decl") else re.sub(rb"\s+>$", b">", m.group(0)))
            continue
        name, attrs, close = m.group("start"), m.group("attrs"), m.group("close")
        parts = [b" " if p[0] in b" \t\n\r" else p for p in _ATTR_PART.findall(attrs)]
        while parts and parts[-1] == b" ":
            parts.pop()
        out.append(b"<" + name + b"".join(parts) + close + b">")
        local = name.split(b":")[-1].lower()
        if not close and (local in SVG_PRESERVING or _XML_SPACE_PRESERVE.search(attrs)):
            depth, stop = 1, n
            for e in _XML_TOKEN.finditer(svg, pos):
                if e.group("start") == name and not e.group("close"):
                    depth += 1
                elif e.group("end") == name:
                    depth -= 1
                    if depth == 0:
                        stop = e.start()
                        break
            out.append(svg[pos:stop])
            pos = stop
    return b"".join(out)


def minify_css(css: bytes) -> bytes:
    """The stylesheet with comments and extra whitespace removed; see the module docstring."""
    out, i, n, space, gap = bytearray(), 0, len(css), False, False

    def emit(token: bytes, glue: bool) -> None:
        nonlocal space, gap
        if space and out and not glue and out[-1:] not in b"{};,(" and out[-1:] != b"":
            out.extend(b" ")
        elif gap and not space and out and out[-1:] not in b"{};,(:" and token[:1] not in b"{};,):":
            # A comment parts two tokens without whitespace; with nothing there they could become
            # one (.a/**/.b is the compound .a.b, 0/**/auto two values), and a space would make a
            # descendant combinator.
            out.extend(b"/**/")
        space = gap = False
        out.extend(token)

    while i < n:
        c = css[i:i + 1]
        if css.startswith(b"/*", i):
            end = css.find(b"*/", i + 2)
            end = n if end < 0 else end + 2
            if css.startswith(b"/*!", i) or _NOTICE.search(css, i, end):
                emit(css[i:end], False)
            else:
                gap = True
            i = end
        elif c in b" \t\n\r\f":
            space = True
            i += 1
        elif c in (b"\"", b"'"):
            j = i + 1
            while j < n and css[j:j + 1] != c and css[j:j + 1] != b"\n":
                j += 2 if css[j:j + 1] == b"\\" else 1
            emit(css[i:j + 1], False)
            i = j + 1
        elif c == b"\\":
            # The whitespace that ends a hex escape belongs to it, not to the run after it.
            escape = _CSS_ESCAPE.match(css, i)
            token = escape.group(0) if escape else css[i:i + 2]
            emit(token, False)
            i += len(token)
        elif css[i:i + 4].lower() == b"url(" and not re.match(rb"url\(\s*[\"']", css[i:i + 64], re.I):
            end = css.find(b")", i)
            end = n if end < 0 else end + 1
            emit(css[i:end], False)
            i = end
        elif c in b"{};,)":
            if c == b"}" and out[-1:] == b";":
                out.pop()
            emit(c, True)
            i += 1
        else:
            j = i + 1
            while j < n and css[j:j + 1] not in b" \t\n\r\f{};,)\"'\\/" and css[j:j + 4].lower() != b"url(":
                j += 1
            emit(css[i:j], False)
            i = j
    return bytes(out)


def find_terser() -> str | None:
    return shutil.which("terser")


def minify_js(js: bytes, terser: str) -> bytes | None:
    """terser's re-print of the script, or None if terser cannot parse it as a script or a module."""
    for extra in ([], ["--module"]):
        result = subprocess.run([terser, "--comments", TERSER_COMMENTS, *extra], input=js, stdout=subprocess.PIPE,
                                stderr=subprocess.PIPE, check=False)
        if result.returncode == 0:
            return result.stdout
    return None


def html_templates(conn) -> tuple:
    """Templates rows that only text/html Content rows use, and the rest as name -> why left alone."""
    users = defaultdict(set)
    for template_id, mime in conn.execute(
            "SELECT DISTINCT c.templateId, ct.value FROM Content c JOIN ContentTypes ct ON c.contentTypeID = ct.id "
            "WHERE c.templateId > 0"):
        users[template_id].add(mime)
    chosen, left = [], {}
    for template_id, name, content in conn.execute("SELECT id, name, content FROM Templates ORDER BY id"):
        if users.get(template_id) == {HTML}:
            chosen.append((template_id, name, bytes(content)))
        else:
            left[name] = (f"used by {', '.join(sorted(users[template_id]))} rows" if template_id in users
                          else "no row names it")
    return chosen, left


def run(cfg: dict) -> int:
    db_path = cfg["db_path"]
    if not db_path.is_file():
        print(f"error: {db_path} does not exist", file=sys.stderr)
        return 1

    conn = sqlite3.connect(db_path)
    codec = None
    changes, template_changes, skipped, kept = [], [], {}, {}
    try:
        codec = BrotliCodec(load_dictionary(conn))
        rows = conn.execute(
            "SELECT c.path, c.content, c.languageID, c.contentTypeID, c.templateId, ct.compression, ct.value "
            "FROM Content c JOIN ContentTypes ct ON c.contentTypeID = ct.id "
            f"WHERE ct.value IN ({','.join('?' * len(TYPES))}) ORDER BY c.path", TYPES
        ).fetchall()
        lengths = {row[0]: len(row[1]) for row in rows}
        bases = [row for row in rows if not is_continuation_path(lengths, row[0]) and row[4] == 0]
        templated = sum(1 for row in rows if row[4] != 0 and not is_continuation_path(lengths, row[0]))
        terser = find_terser()
        if terser is None and any(row[6] == JS for row in bases):
            print("error: terser is not installed (apt install node-terser, or npm install -g terser); "
                  "nothing was changed", file=sys.stderr)
            return 1
        print(f"Minifying {len(bases)} row(s) ({templated} templated row(s) left as they are)...")

        work = [(row[0], reassemble(conn, row[0], row[1]), len(row[1]), *row[2:]) for row in bases]

        def read(item):
            path, full, base_len, language_id, content_type_id, template_id, compression, mime = item
            try:
                return item, codec.decompress(full, compression), None
            except Exception as exc:  # noqa: BLE001 - one unreadable row is not the run
                return item, None, f"could not be read: {exc}"

        with ThreadPoolExecutor(max_workers=cfg["workers"]) as pool:
            decoded = list(pool.map(read, work))
        skipped = {item[0]: problem for item, raw, problem in decoded if problem is not None}
        # What a stylesheet preserves decides what every page may lose, so one that cannot be read
        # leaves nothing safe to write.
        unread_css = sorted(item[0] for item, raw, problem in decoded if problem is not None and item[7] == CSS)
        if unread_css:
            for path in unread_css:
                print(f"  skipped {path}: {skipped[path]}", file=sys.stderr)
            print(f"error: {len(unread_css)} stylesheet(s) could not be read, so it is unknown what they keep "
                  "unminified; nothing was changed", file=sys.stderr)
            return 1

        # Every stylesheet in the database, whichever page links it: preserving
        # more than a page needs costs bytes, never a change in rendering.
        preserving = Preserving()
        for item, raw, _problem in decoded:
            if raw is not None and item[7] == CSS:
                preserving.add_css(raw)

        def minify(entry):
            item, raw, _problem = entry
            path, full, base_len, language_id, content_type_id, template_id, compression, mime = item
            if mime == HTML:
                new = minify_html(raw, preserving)
            elif mime == SVG:
                new = minify_svg(raw)
            elif mime == CSS:
                new = minify_css(raw)
            else:
                new = minify_js(raw, terser)
                if new is None:
                    return path, None, "terser could not parse it"
            if len(new) >= len(raw):
                return path, None, None
            stored = codec.compress(new, compression)
            return path, (base_len, stored, language_id, content_type_id, template_id, mime,
                          len(raw) - len(new), len(full) - len(stored)), None

        with ThreadPoolExecutor(max_workers=cfg["workers"]) as pool:
            results = list(pool.map(minify, [e for e in decoded if e[1] is not None]))
        changes = [(path, *change) for path, change, _note in results if change is not None]
        kept = {path: note for path, change, note in results if note is not None}

        chosen, left = html_templates(conn)
        for template_id, name, content in chosen:
            new = minify_html(content, preserving, template=True)
            if len(new) < len(content):
                template_changes.append((template_id, name, new, len(content) - len(new)))

        totals = defaultdict(lambda: [0, 0, 0])
        for change in changes:
            totals[change[6]][0] += 1
            totals[change[6]][1] += change[7]
            totals[change[6]][2] += change[8]
        for mime in TYPES:
            count, decoded_saved, stored_saved = totals[mime]
            print(f"  {mime}: {count} row(s), {human(decoded_saved)} decoded byte(s) saved, "
                  f"{human(stored_saved)} stored against the current dictionary")
        template_saved = sum(t[3] for t in template_changes)
        print(f"  templates: {len(template_changes)} of {len(chosen)} HTML template(s), {human(template_saved)} "
              "byte(s) saved")
        for name, why in sorted(left.items()):
            print(f"  template {name} left as it is: {why}")
        for path, note in sorted(kept.items()):
            print(f"  left {path} as it is: {note}")
        for path, problem in sorted(skipped.items()):
            print(f"  skipped {path}: {problem}", file=sys.stderr)

        if cfg["dry_run"]:
            print("Dry run: nothing written.")
        elif changes or template_changes:
            print(f"Backing up {db_path} ...")
            print(f"Backup written to {backup_database(db_path)}")
            conn.execute("BEGIN")
            try:
                for path, base_len, stored, lang, ctid, tid, mime, decoded_saved, stored_saved in changes:
                    replace_row(conn, path, base_len, stored, lang, ctid, tid, lambda m: print(m, file=sys.stderr))
                    if cfg["verbose"]:
                        print(f"  [MINIFY] {path}: {human(decoded_saved)} decoded byte(s) saved")
                for template_id, name, new, saved in template_changes:
                    conn.execute("UPDATE Templates SET content = ? WHERE id = ?", (new, template_id))
                    if cfg["verbose"]:
                        print(f"  [MINIFY] template {name}: {human(saved)} byte(s) saved")
                conn.commit()
            except Exception:
                conn.rollback()
                raise
    finally:
        if codec is not None:
            codec.close()
        conn.close()

    if not cfg["dry_run"] and (changes or template_changes):
        print("Vacuuming database...")
        vac = sqlite3.connect(db_path)
        try:
            vac.execute("VACUUM")
        finally:
            vac.close()

    verb = "would change" if cfg["dry_run"] else "changed"
    decoded_total = sum(c[7] for c in changes) + sum(t[3] for t in template_changes)
    print(f"\nDone. {verb} {len(changes)} row(s) and {len(template_changes)} template(s), saving "
          f"{human(decoded_total)} decoded byte(s); {len(skipped)} row(s) skipped.")
    if changes and not cfg["dry_run"]:
        print("Next: remint_dictionary.py, then verify_remint_dictionary.py. Until the dictionary is re-minted "
              "the minified rows compress worse than before.")
    if skipped:
        print(f"error: {len(skipped)} row(s) could not be read and were left as they were.", file=sys.stderr)
    return 1 if skipped else 0


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("db_path", type=Path, help="SQLite database to edit in place, e.g. documentation.db")
    p.add_argument("--dry-run", action="store_true",
                   help="Do all the work and report what would change, without writing or backing up")
    p.add_argument("--workers", type=int, default=8,
                   help="Threads used to decompress, minify and recompress rows (default: 8)")
    p.add_argument("--verbose", action="store_true", help="Log every row and template changed")
    return p


def main() -> None:
    args = build_parser().parse_args()
    sys.exit(run({"db_path": args.db_path, "dry_run": args.dry_run, "workers": args.workers,
                  "verbose": args.verbose}))


if __name__ == "__main__":
    main()
