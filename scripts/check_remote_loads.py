#!/usr/bin/env python3
# /// script
# requires-python = ">=3.9"
# dependencies = [
#     "brotli",
#     "Pillow",
# ]
# ///
"""
check_remote_loads.py

Fails the build if anything in the documentation database would still make
the app's WebView load a file from off the device (ADFA-6589). It only reads:
nothing is written, backed up or vacuumed.

The steps before it rewrite the shapes they were written for, and each reports
only near-misses of its own shapes, so an <iframe> embed, a stylesheet <link>
or a CSS url() on another host passes all three with exit 0 and nothing said.
This step looks for every kind of load instead, in everything the WebView
renders, and exits 1 naming each one it finds: the row, the URL, and the shape
(element and attribute, or where the CSS is). On the 2026-10-13 database it
reports 90 loads on 35 rows before those steps and none after them.

A load is an address that is absolute http(s) - also written with backslashes
or without its slashes, which a browser reads the same way - or
protocol-relative (//host/...), on any host but localhost, 127.x.x.x and
[::1], in one of these places:

  - src, srcset, data-src or data-srcset on any element; poster and
    background on any element; <object data>;
  - <link> href and imagesrcset, unless every word of its rel is one that
    loads nothing (NON_LOADING_RELS below: canonical, alternate, author,
    license, next, prev and the like); stylesheet, icon, preload, prefetch,
    preconnect, dns-prefetch, manifest, modulepreload and any rel not listed
    count;
  - <base href>, which would send every relative address on the page off the
    device;
  - <meta http-equiv="refresh">'s URL;
  - href and xlink:href on SVG <image>, <use>, <feImage> and <script>;
  - <a ping>, <form action> and formaction on any element: like a link they
    need a tap, but the app is not known to ask before sending them, so they
    count, failing safe;
  - CSS url(), @import and image-set() strings, in style attributes, SVG
    presentation attributes that take a url() (fill, stroke, filter,
    clip-path, mask, marker-*, cursor), <style> blocks and text/css rows,
    with CSS comments skipped;
  - the markup in <iframe srcdoc>, checked like a page's;
  - <?xml-stylesheet href?> in SVG and other XML rows;
  - in a JSON page (a text/html row whose body starts with "{", which a Pebble
    template renders): every "src" string, as in page.peb's image blocks, and
    the markup in every string holding a "<", JSON-escaped in the stored row.
    Not every such string is written out unescaped - page.peb escapes a code
    block - but reading them all depends on no one template's field names,
    and a remote <img> shown only as code text fails safe; the 2026-10-13
    database has none.

Counting: one attribute on one element is one load, however many addresses it
holds (a srcset naming three remote files is one), as is one url(), @import or
image-set() address in CSS and one JSON "src". Distinct URLs are not counted:
the same diagram on two pages is two loads.

Not loads:
  - <a href> and <area href>: the app asks before it opens an off-device
    link;
  - <link rel=canonical> and rel=alternate, which no browser fetches;
  - anything inside an HTML comment, which loads nothing;
  - XML namespace URIs (xmlns=...) and DOCTYPE identifiers, which browsers do
    not fetch.

JavaScript is decoded, so an unreadable script fails the run, but not searched.
A URL in a script's text is not a load by itself, and a script can build one
that no pattern sees. In the 2026-10-13 database 27 of the 412 script rows name
an http(s) address, on 15 hosts: copyright and license comments, XML namespace
strings, dummy bases for parsing URLs (example.com, foo.bar), and Help+Manual's
share buttons, which open a page only when tapped. Counting each would fail
every build, and an allow-list would hide a new one as surely as a pattern
misses it. A remote <script src> is caught above; what a script does once it
runs is for the app to stop, which CodeOnTheGo's side of ADFA-6589 does by
answering a documentation WebView's off-device requests itself. Markdown and
plain-text rows are not rendered as markup and are not read.

Besides Content, it reads the Templates table - Pebble templates wrapped
around pages, so a load in one would be on every page it renders - and
Tooltips' summary and detail, which the editor shows in a WebView. A row that
cannot be decompressed, or a JSON page that does not parse, cannot be checked:
it is named and fails the run too. A page over the 1 MiB chunk size is read
whole across its "<path>-1", "-2", ... rows.

Where it goes in the pipeline: last of the ADFA-6589 steps -
remove_skin_font_comment.py, bundle_remote_images.py, remove_remote_loads.py,
then this - before the `cp DB migrated.db` snapshot, so the database the media
pipeline starts from has nothing left to load from the internet. It writes
nothing, so it can be run again at any later point.

    uv run scripts/check_remote_loads.py documentation.db
    uv run scripts/check_remote_loads.py documentation.db --verbose
"""
from __future__ import annotations

import argparse
import json
import multiprocessing.util
import re
import sqlite3
import sys
from collections import Counter
from concurrent.futures import ProcessPoolExecutor
from html.parser import HTMLParser
from pathlib import Path

from optimize_db_media import BrotliCodec, human, is_continuation_path, load_dictionary, reassemble

# How each content type is read. "markup" is HTML (or a JSON page, by its first
# character); "xml" is markup that may also carry <?xml-stylesheet?>; "script"
# is decoded only (see the docstring). Templates are read as "template":
# markup, though a Pebble comment opens with "{".
READERS = {
    "text/html": "markup", "application/xhtml+xml": "xml", "image/svg+xml": "xml", "application/xml": "xml",
    "text/xml": "xml", "text/css": "css", "text/javascript": "script", "application/javascript": "script",
    "application/json": "json",
}

# rel words that never make a browser fetch the link's href. A <link> counts
# unless every word of its rel is here, so a rel this list does not know fails
# safe.
NON_LOADING_RELS = frozenset({
    "alternate", "author", "bookmark", "canonical", "external", "help", "license", "me", "next", "nofollow",
    "noopener", "noreferrer", "opener", "prev", "tag",
})
# Attributes that hold CSS: style, and the SVG presentation attributes that
# take a url().
CSS_ATTRIBUTES = ("style", "fill", "stroke", "filter", "clip-path", "mask", "marker-start", "marker-mid",
                  "marker-end", "cursor")
URL_ATTRIBUTES = ("src", "data-src", "poster", "background", "formaction")
SRCSET_ATTRIBUTES = ("srcset", "data-srcset")
SVG_HREF_ELEMENTS = ("image", "use", "feimage", "script")

_SCHEME = re.compile(r"([A-Za-z][A-Za-z0-9+.-]*):")
_AUTHORITY = re.compile(r"/*([^/?#]*)")
_LOOPBACK = re.compile(r"127\.[0-9]{1,3}\.[0-9]{1,3}\.[0-9]{1,3}")
_CONTROL_OR_SPACE = "".join(map(chr, range(33)))

_CSS_COMMENT = re.compile(r"/\*.*?(?:\*/|\Z)", re.S)
_CSS_URL = re.compile(r"""(?<![\w-])url\(\s*(?:"([^"]*)"|'([^']*)'|([^"')\s]*))""", re.I)
_CSS_URL_WHOLE = re.compile(r"""(?<![\w-])url\((?:\s*"[^"]*"|\s*'[^']*'|[^)]*)\s*\)""", re.I)
_CSS_IMPORT = re.compile(r"""@import\s*(?:"([^"]*)"|'([^']*)')""", re.I)
_CSS_IMAGE_SET = re.compile(r"(?<![\w-])(?:-webkit-)?image-set\(", re.I)
_CSS_STRING = re.compile(r""""([^"]*)"|'([^']*)'""")
_REFRESH_URL = re.compile(r"""\s*[0-9.]*\s*[;,]?\s*(?:url\s*=\s*)?["']?([^"']*)""", re.I)
_PSEUDO_HREF = re.compile(r"""\bhref\s*=\s*(?:"([^"]*)"|'([^']*)')""")


def remote_url(value):
    """`value`, stripped, if a browser reading it as an address would fetch it
    from a host other than this device; None otherwise. A browser drops tabs
    and line breaks anywhere in an address, reads a backslash as a slash, and
    takes an http(s) host after any number of slashes - even none, unless the
    page is on that same scheme, when "http:x" is relative; it counts either
    way, failing safe. The host is what follows the last "@" of the authority.
    An http(s) address with no host counts too."""
    if not value:
        return None
    url = value.strip(_CONTROL_OR_SPACE)
    normal = re.sub(r"[\t\n\r]", "", url).replace("\\", "/")
    scheme = _SCHEME.match(normal)
    if scheme:
        if scheme.group(1).lower() not in ("http", "https"):
            return None
        rest = normal[scheme.end():]
    elif normal.startswith("//"):
        rest = normal
    else:
        return None
    host = _AUTHORITY.match(rest).group(1).rpartition("@")[2].lower()
    host = host[:host.find("]") + 1] if host.startswith("[") else host.partition(":")[0]
    if host in ("localhost", "[::1]") or _LOOPBACK.fullmatch(host):
        return None
    return url


def srcset_urls(value: str) -> list:
    """The address of each candidate in a srcset, split as the HTML standard
    splits one: a candidate's address runs to whitespace, and its descriptors
    run to a comma outside parentheses."""
    urls, at, end = [], 0, len(value)
    while True:
        while at < end and (value[at].isspace() or value[at] == ","):
            at += 1
        if at >= end:
            return urls
        start = at
        while at < end and not value[at].isspace():
            at += 1
        url = value[start:at]
        urls.append(url.rstrip(","))
        if url.endswith(","):
            continue
        depth = 0
        while at < end and (value[at] != "," or depth):
            depth += {"(": 1, ")": -1 if depth else 0}.get(value[at], 0)
            at += 1


def css_loads(text: str, where: str, found: list) -> None:
    """Appends (shape, url, True) for each remote url(), @import string or
    image-set() string in `text`."""
    text = _CSS_COMMENT.sub(" ", text)
    for match in _CSS_URL.finditer(text):
        url = remote_url(next((group for group in match.groups() if group is not None), ""))
        if url:
            found.append((f"{where} url()", url, True))
    for match in _CSS_IMPORT.finditer(text):
        url = remote_url(match.group(1) if match.group(1) is not None else match.group(2))
        if url:
            found.append((f"{where} @import", url, True))
    for match in _CSS_IMAGE_SET.finditer(text):
        depth, at = 1, match.end()
        while at < len(text) and depth:
            depth += {"(": 1, ")": -1}.get(text[at], 0)
            at += 1
        # Its url() entries were counted above; only bare strings are left.
        for string in _CSS_STRING.finditer(_CSS_URL_WHOLE.sub(" ", text[match.end():at])):
            url = remote_url(string.group(1) if string.group(1) is not None else string.group(2))
            if url:
                found.append((f"{where} image-set()", url, True))


class _Markup(HTMLParser):
    """Appends (shape, url, counted) to `found` for every remote address in one
    piece of markup that a browser would load (counted) or that is only a link
    (not counted, kept for --verbose)."""

    def __init__(self, found: list, where: str = "", xml: bool = False):
        super().__init__(convert_charrefs=True)
        self.found, self.where, self.xml = found, where, xml
        self.style = None

    def _add(self, shape: str, value, counted: bool = True) -> None:
        url = remote_url(value)
        if url:
            self.found.append((shape, url, counted))

    def handle_starttag(self, tag, attrs):
        values = {}
        for name, value in attrs:
            values.setdefault(name, value or "")  # a browser keeps the first of a repeated attribute
        element = f"{self.where}<{tag}"
        for name in URL_ATTRIBUTES:
            self._add(f"{element} {name}>", values.get(name))
        for name in SRCSET_ATTRIBUTES + (("imagesrcset",) if tag == "link" else ()):
            remote = [url for url in srcset_urls(values.get(name, "")) if remote_url(url)]
            if remote:
                self.found.append((f"{element} {name}>", remote[0].strip(), True))
        if tag == "object":
            self._add(f"{element} data>", values.get("data"))
        if tag in SVG_HREF_ELEMENTS:
            for name in ("href", "xlink:href"):
                self._add(f"{element} {name}>", values.get(name))
        if tag == "link":
            rels = values.get("rel", "").lower().split()
            self._add(f'{element} rel="{" ".join(rels)}" href>', values.get("href"),
                      counted=not set(rels) <= NON_LOADING_RELS)
        if tag == "base":
            self._add(f"{element} href>", values.get("href"))
        if tag in ("a", "area"):
            self._add(f"{element} href>", values.get("href"), counted=False)
            for url in values.get("ping", "").split():
                self._add(f"{element} ping>", url)
        if tag == "form":
            self._add(f"{element} action>", values.get("action"))
        if tag == "meta" and values.get("http-equiv", "").lower() == "refresh":
            self._add(f'{element} http-equiv="refresh">', _REFRESH_URL.match(values.get("content", "")).group(1))
        if tag == "iframe" and "srcdoc" in values:
            scan_markup(values["srcdoc"], self.found, f"{element} srcdoc>: ", self.xml)
        for name in CSS_ATTRIBUTES:
            if name in values:
                css_loads(values[name], f"{element} {name}>", self.found)
        if tag == "style":
            self.style = []

    def handle_endtag(self, tag):
        if tag == "style":
            self._end_style()

    def handle_data(self, data):
        if self.style is not None:
            self.style.append(data)

    def handle_pi(self, data):
        if self.xml and data.startswith("xml-stylesheet"):
            href = _PSEUDO_HREF.search(data)
            if href:
                self._add(f"{self.where}<?xml-stylesheet href?>",
                          href.group(1) if href.group(1) is not None else href.group(2))

    def _end_style(self):
        if self.style is not None:
            css_loads("".join(self.style), f"{self.where}<style>", self.found)
            self.style = None

    def close(self):
        super().close()
        self._end_style()  # a <style> never closed runs to the end of the page


def scan_markup(text: str, found: list, where: str = "", xml: bool = False) -> None:
    parser = _Markup(found, where, xml)
    parser.feed(text)
    parser.close()


def json_document_loads(node, found: list) -> None:
    """Appends what a JSON page's template could load: each "src" string, and
    the markup in each string holding a "<"."""
    if isinstance(node, dict):
        src = node.get("src")
        url = remote_url(src) if isinstance(src, str) else None
        if url:
            found.append((f'JSON "src"{" of an image block" if node.get("type") == "image" else ""}', url, True))
        for value in node.values():
            json_document_loads(value, found)
    elif isinstance(node, list):
        for item in node:
            json_document_loads(item, found)
    elif isinstance(node, str) and "<" in node:
        scan_markup(node, found, "JSON string ")


def check_text(raw: bytes, reader: str) -> list:
    """[(shape, url, counted)] for one decoded row. Raises ValueError for a
    JSON page that does not parse."""
    found = []
    text = raw.decode("utf-8", "replace")
    if reader == "css":
        css_loads(text, "CSS", found)
    elif reader == "json" or (reader == "markup" and text.lstrip()[:1] == "{"):
        json_document_loads(json.loads(text), found)
    elif reader in ("markup", "xml", "template"):
        scan_markup(text, found, xml=reader == "xml")
    return found


_codec = None


def _start_worker(dictionary) -> None:
    """Gives a pool process its own codec. Parsing is Python, so it runs in
    processes rather than threads; a process has no other hook to remove the
    codec's temporary folder when it exits."""
    global _codec
    _codec = BrotliCodec(dictionary)
    multiprocessing.util.Finalize(_codec, _codec.close, exitpriority=0)


def check_row(item) -> tuple:
    """(path, [(shape, url, counted)], problem or None) for one row, in a pool
    process. Touches no database."""
    path, blob, reader, compression = item
    try:
        raw = _codec.decompress(blob, compression)
    except Exception as exc:  # noqa: BLE001 - one unreadable row is not the run
        return path, [], f"could not be read: {exc}"
    try:
        return path, check_text(raw, reader), None
    except ValueError as exc:
        return path, [], f"does not parse as JSON, so what its template renders cannot be checked ({exc})"


def run(cfg: dict) -> int:
    db_path = cfg["db_path"]
    if not db_path.is_file():
        print(f"error: {db_path} does not exist", file=sys.stderr)
        return 1

    # Read-only, so not even a journal is written.
    conn = sqlite3.connect(db_path.resolve().as_uri() + "?mode=ro", uri=True)
    try:
        dictionary = load_dictionary(conn)
        # Fails here, with its own message, if the brotli CLI is missing, rather
        # than in every pool process.
        BrotliCodec(dictionary).close()
        types = sorted(READERS)
        rows = conn.execute(
            "SELECT c.path, c.content, ct.value, ct.compression FROM Content c "
            "JOIN ContentTypes ct ON c.contentTypeID = ct.id "
            f"WHERE ct.value IN ({', '.join('?' * len(types))}) ORDER BY c.path", types
        ).fetchall()
        lengths = {row[0]: len(row[1]) for row in rows}
        bases = [row for row in rows if not is_continuation_path(lengths, row[0])]
        tables = {name for (name,) in conn.execute("SELECT name FROM sqlite_master WHERE type = 'table'")}
        # Templates and tooltips are stored as plain text.
        extra = []
        if "Templates" in tables:
            extra += [(f"Templates: {name}", bytes(content), "template", "none")
                      for name, content in conn.execute("SELECT name, content FROM Templates ORDER BY id")]
        if "Tooltips" in tables:
            for tooltip_id, tag, summary, detail in conn.execute(
                    "SELECT id, tag, summary, detail FROM Tooltips ORDER BY id"):
                for column, text in (("summary", summary), ("detail", detail)):
                    if text:
                        extra.append((f"Tooltips {tooltip_id} ({tag}) {column}", str(text).encode("utf-8"),
                                      "markup", "none"))
        by_type = Counter(row[2] for row in bases)
        print(f"Checking {human(len(bases))} Content row(s) ("
              + ", ".join(f"{human(n)} {value}" for value, n in sorted(by_type.items()))
              + f"; {len(rows) - len(bases)} chunk-continuation row(s) folded into their base), "
              f"{human(len(extra))} template and tooltip text(s), for loads from off the device...")

        work = [(path, reassemble(conn, path, content), READERS[value], compression)
                for path, content, value, compression in bases] + extra
    finally:
        conn.close()

    with ProcessPoolExecutor(max_workers=cfg["workers"], initializer=_start_worker,
                             initargs=(dictionary,)) as pool:
        results = list(pool.map(check_row, work, chunksize=64))

    loads = [(path, shape, url) for path, found, _problem in results for shape, url, counted in found if counted]
    ignored = Counter(shape for _path, found, _problem in results for shape, _url, counted in found if not counted)
    unreadable = [(path, problem) for path, _found, problem in results if problem]
    for path, shape, url in loads:
        print(f"  {path}: {shape} loads {url}", file=sys.stderr)
    for path, problem in unreadable:
        print(f"  {path}: {problem}", file=sys.stderr)
    if loads:
        print(f"{human(len(loads))} load(s) from off the device, by shape:")
        for shape, n in sorted(Counter(shape for _path, shape, _url in loads).items(), key=lambda kv: (-kv[1], kv[0])):
            print(f"  {shape}: {human(n)}")
    if cfg["verbose"]:
        print(f"Remote addresses seen and not counted as loads ({human(sum(ignored.values()))}):")
        for shape, n in sorted(ignored.items(), key=lambda kv: (-kv[1], kv[0])):
            print(f"  {shape}: {human(n)}")

    rows_with_loads = len({path for path, _shape, _url in loads})
    if not loads and not unreadable:
        print("\nDone. Nothing loads from off the device.")
        return 0
    print(f"\nerror: {human(len(loads))} load(s) from off the device on {human(rows_with_loads)} row(s), and "
          f"{len(unreadable)} row(s) that could not be checked (listed above). Each load would make the app "
          "connect off the device; fix them before shipping this database.", file=sys.stderr)
    return 1


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("db_path", type=Path, help="SQLite database to check (opened read-only), e.g. documentation.db")
    p.add_argument("--workers", type=int, default=8,
                   help="Processes used to decompress and read rows (default: 8)")
    p.add_argument("--verbose", action="store_true",
                   help="Also tally the remote addresses seen and not counted (links, canonical links)")
    return p


def main() -> None:
    args = build_parser().parse_args()
    sys.exit(run({"db_path": args.db_path, "workers": args.workers, "verbose": args.verbose}))


if __name__ == "__main__":
    main()
