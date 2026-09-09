#!/usr/bin/env python3
"""Turns the scraped developer.android.com reference HTML into a JSON documentation tree.

The scrape under ProcessDocs/AndroidDocs holds two families of page, written by two different
doc tools:

  android/   doclava output   -- the framework reference, versioned by API level
  androidx/  Dackka output    -- the Jetpack reference, versioned by artifact release

The markup differs, but the *documentation* in it does not: both give a class a signature, an
inheritance chain, summary tables and a detail entry per member. This module reads either flavor
into one schema, so a single template renders both. See README.md for the schema.

Output mirrors the input tree file-for-file with `.json` in place of `.html`, which is what makes
the links inside the JSON work: every `/reference/...` URL that the scrape actually contains is
rewritten to a relative `.json` path, and the renderer swaps that extension for `.html`. URLs
pointing outside the scrape are made absolute back to developer.android.com, so they stay
clickable rather than dangling.

Two files per package are synthesised rather than scraped: `package-summary.json` for the packages
that have no scraped package page (Dackka writes none at all), and a root `index.json`. Without
them the tree has no entry point and no way to walk from a class up to its neighbours.

    ./android_docs_to_json.py ../android ../androidx --out ../../../build/android-json
"""

from __future__ import annotations

import argparse
import html as html_entities
import json
import os
import posixpath
import re
import sys
import time
from concurrent.futures import ProcessPoolExecutor
from pathlib import Path

from bs4 import BeautifulSoup, Comment, Tag

SITE = "https://developer.android.com"

# Devsite's own widgets. None of them carry documentation -- they are the surrounding chrome of the
# live site -- so they go before anything else is read.
NOISE_TAGS = (
    "script", "style", "devsite-toc", "devsite-hats-survey", "devsite-thumb-rating",
    "devsite-recommendations", "devsite-recommendations-dropdown", "devsite-feature-tooltip",
    "devsite-bookmark", "devsite-page-rating", "devsite-select",
)
NOISE_SELECTORS = (
    ".nocontent", ".devsite-actions", ".devsite-article-meta", ".devsite-breadcrumb-list",
    ".data-reference-resources-wrapper", ".sum-details-links", "#naMessage",
    ".devsite-floating-action-buttons",
)
# Wrappers that exist only to give devsite something to hook behaviour onto; their children are
# the real content, so they are unwrapped rather than dropped.
UNWRAP_TAGS = ("devsite-code", "devsite-expandable", "devsite-selector", "colgroup")

# Everything else -- track-*, data-*, translate, dir, tabindex, itemprop, inline style -- is devsite
# bookkeeping that would otherwise be copied into every one of the 12,000 output files.
KEEP_ATTRS = frozenset((
    "href", "src", "alt", "title", "id", "class", "colspan", "rowspan", "width", "start", "value",
))

_REFERENCE = re.compile(r"^(?:https?://developer\.android\.com)?/reference/(.+)$")
_OTHER_SCHEME = re.compile(r"^[a-z][a-z0-9+.-]*:(?!//)", re.I)
_ADDED_IN = re.compile(r"Added in\s+(?:API level\s+)?(\S+)")
_DEPRECATED_IN = re.compile(r"Deprecated in\s+(?:API level\s+)?(\S+)")
_CONSTANT_VALUE = re.compile(r"^Constant Value:\s*(.*)$", re.S)
_FIRST_SENTENCE = re.compile(r"^(.*?[.!?])(?:\s|$)", re.S)
# Rules and line breaks at the edge of a fragment are the seams left where the site's own layout
# was removed; inside a fragment they are the author's.
_EDGE_RULES = re.compile(r"^(?:\s|<br\s*/?>|<hr\s*/?>)+")
_EDGE_RULES_END = re.compile(r"(?:\s|<br\s*/?>|<hr\s*/?>)+$")
# A link's class, once it has been resolved. The scraped pages carried these two and the app
# styles them red, so a reader can see which links need the network and which lead nowhere rather
# than finding out by tapping.
EXTERNAL_LINK_CLASS = "external-link"
BROKEN_LINK_CLASS = "broken-link"
_SCHEME = re.compile(r"^[a-z][a-z0-9+.-]*:", re.I)

_CODE_PADDING = re.compile(r"(<code[^>]*>)\s+")
_CODE_PADDING_END = re.compile(r"\s+(</code>)")
# The sentence both tools write for a deprecation notice: "This class was deprecated in API
# level 21.", "This enum value is deprecated.", "This @interface was deprecated in API level 30."
# Devsite uses the same `.caution` markup for any warning, so the notice has to be recognised by
# its wording -- a caution in a guide section that merely mentions a deprecated API is not this
# page's, and matching on the word alone hoisted those out of the prose they belonged to.
_DEPRECATION_NOTICE = re.compile(r"\bThis\s+\S+(?:\s+\w+)?\s+(?:was|is)\s+deprecated\b", re.I)

# The detail tables a member carries, by the heading the source gives them.
_DETAIL_TABLES = {"parameters": "parameters", "returns": "returns", "throws": "throws",
                  "see also": "seeAlso"}

# How a generated package summary groups its types, titled and ordered the way doclava's own
# package pages are. A kind not named here is listed with the classes.
_PACKAGE_GROUPS = {
    "interface": ("interfaces", "Interfaces"), "class": ("classes", "Classes"),
    "enum": ("enums", "Enums"), "annotation": ("annotations", "Annotations"),
    "object": ("objects", "Objects"), "record": ("records", "Records"),
}

# Section ids as they appear in the scrape, mapped to the order a reference page shows them in.
# doclava and Dackka disagree on a couple of names for the same section ("fields" vs
# "public-fields"), so both spellings appear.
SECTION_ORDER = {
    "nested-classes": 0, "nested-types": 0,
    "enum-values": 1,
    "constants": 2, "inherited-constants": 3,
    "xml-attributes": 4, "inherited-xml-attributes": 5,
    "fields": 6, "public-fields": 6, "protected-fields": 7, "inherited-fields": 8,
    "public-constructors": 9, "protected-constructors": 10,
    "public-methods": 11, "protected-methods": 12, "extension-functions": 13,
    "inherited-methods": 14,
}


# --------------------------------------------------------------------------------------------
# Page discovery and link rewriting
# --------------------------------------------------------------------------------------------

def json_path(relative: str) -> str:
    """A scraped page's place in the JSON tree: the same path with the extension swapped.

    Only the extension. `replace(".html", ".json")` would rewrite a `.html` anywhere in the path,
    and the two trees mirroring each other file-for-file is what makes every relative link in the
    JSON resolve.
    """
    return relative.removesuffix(".html") + ".json"


def version_scheme(flavor: str, library: str) -> str:
    """What a page's version numbers count.

    The framework is versioned by API level and Jetpack by the release of the artifact a type
    ships in, so the tool that wrote a page is what says which of the two a version number is.
    The library prefix is only a fallback for a page with no recognisable flavor: it is not the
    same question, because `android/support/v4/media` sits under `android/` and is Dackka-written
    Jetpack documentation, versioned 1.1.0 rather than by API level.
    """
    if flavor == "doclava":
        return "api-level"
    if flavor == "dackka":
        return "library-version"
    return "api-level" if library == "android" else "library-version"


def find_pages(roots: list[Path]) -> list[tuple[Path, str]]:
    """Every scraped .html file, as (root, path relative to the root's parent).

    The relative path keeps the `android/` or `androidx/` prefix, because that prefix is what a
    `/reference/...` URL names, and cross-library links have to resolve.
    """
    pages: list[tuple[Path, str]] = []
    for root in roots:
        base = root.parent
        for path in sorted(root.rglob("*.html")):
            pages.append((base, path.relative_to(base).as_posix()))
    return pages


class Linker:
    """Rewrites the scrape's site-absolute URLs into links that work in the generated tree.

    A `/reference/...` URL whose page the scrape contains becomes a relative `.json` path; one it
    does not contain -- `java.lang.Object`, the Kotlin view of a page, a guide -- becomes an
    absolute developer.android.com URL. Nothing is left as a site-absolute path, because those
    resolve against whatever host serves the output.
    """

    def __init__(self, known: set[str]):
        # Keyed the way a reference URL names a page: no extension, no `/reference/` prefix.
        self.known = known
        # And again case-folded, for the pages whose path on disk is not the case the site uses.
        # `android.os.strictmode` is a package and `android.os.StrictMode` a class; a
        # case-insensitive filesystem cannot give both their own directory, so the package's 25
        # pages are stored under `StrictMode/`. Every link in the corpus spells the package
        # lowercase, so without this fallback all 25 look like pages the scrape does not have and
        # are linked to developer.android.com instead of to the copy sitting right there.
        self._folded = {key.casefold(): key for key in known}

    def resolve(self, url: str | None, from_page: str) -> str | None:
        if not url:
            return url
        url = url.strip()
        if url.startswith("#"):
            return url
        if url.startswith("//"):
            return "https:" + url
        # Anything with a scheme of its own is left alone. The scrape contains a handful of
        # hand-written `ftp:` and `mailto:` links, and treating one of those as a relative path
        # would turn it into a link into this tree.
        if _OTHER_SCHEME.match(url):
            return url
        path, sep, fragment = url.partition("#")
        fragment = sep + fragment
        match = _REFERENCE.match(path)
        if match:
            key = match.group(1).rstrip("/")
            if key.endswith(".html"):
                key = key[: -len(".html")]
            if key in self.known:
                return self._relative(key, from_page) + fragment
            folded = self._folded.get(key.casefold())
            if folded is not None:
                return self._relative(folded, from_page) + fragment
            return f"{SITE}/reference/{key}{fragment}"
        if path.startswith("/"):
            return SITE + path + fragment
        return url

    def _relative(self, key: str, from_page: str) -> str:
        from_dir = posixpath.dirname(from_page) or "."
        return posixpath.relpath(key + ".json", from_dir)


# --------------------------------------------------------------------------------------------
# Fragment cleaning
# --------------------------------------------------------------------------------------------

class Document:
    """One scraped page, ready to read.

    Holds the tree with the site's chrome already stripped, the path the JSON goes to, and the
    linker that rewrites this page's URLs -- the three things every fragment helper needs, which
    is why they take a Document rather than passing a linker and a path around in pairs.

    `read` is the only way to get one, and it strips the chrome. That is what makes the
    precondition the helpers depend on -- that no devsite widget, comment or bookkeeping attribute
    is still in the tree -- impossible to skip: a helper takes a Document, and a Document has been
    stripped. Before this existed the helpers stripped their own fragment, which meant re-walking
    every subtree two dozen times for chrome that was already gone.
    """

    __slots__ = ("root", "flavor", "relative", "linker", "own_file")

    def __init__(self, root: Tag, flavor: str, relative: str, linker: Linker) -> None:
        self.root = root
        self.flavor = flavor
        self.relative = relative
        self.linker = linker
        # The name this page is written under, for telling its own anchors from other pages'.
        self.own_file = json_path(posixpath.basename(relative))

    @classmethod
    def read(cls, source: Path, relative: str, linker: Linker) -> Document | None:
        """Parses a scraped page, or None if it holds nothing recognisable."""
        markup = source.read_text(encoding="utf-8", errors="replace")
        # The scrape inlines the site's whole stylesheet into every page -- tens of kilobytes of
        # CSS that the parser would otherwise have to tokenise 12,000 times.
        markup = re.sub(r"<style\b.*?</style>", "", markup, flags=re.S)
        soup = BeautifulSoup(markup, "lxml")
        root, flavor = content_root(soup)
        if root is None:
            return None
        # The itemscope block ahead of a Dackka page repeats every member name as a <meta>.
        for block in root.find_all("div", attrs={"itemscope": True}):
            block.decompose()
        strip_noise(root)
        return cls(root, flavor, relative, linker)

    def link(self, url: str | None) -> str | None:
        """This page's view of a URL: relative into the JSON tree, or absolute off it."""
        return self.linker.resolve(url, self.relative)


def link_class(resolved: str | None) -> str | None:
    """What kind of link a resolved URL is: off-site, going nowhere, or into this tree.

    Read off the resolved URL rather than tracked through the resolving, because the answer is
    there to be read. A link into the tree is a relative path to a `.json` file, since that is the
    only thing the linker produces for a page it found; a fragment stays on this page; anything
    with a scheme of its own leaves the app. What is left is a URL the linker could not place -- a
    malformed `href` in the source, of which the corpus has a handful -- and that is what "broken"
    means here: it names nothing, in this tree or anywhere.
    """
    if not resolved or resolved.startswith("#"):
        return None
    if _SCHEME.match(resolved):
        return EXTERNAL_LINK_CLASS
    if resolved.partition("#")[0].endswith(".json"):
        return None
    return BROKEN_LINK_CLASS


def add_class(tag: Tag, name: str | None) -> None:
    """Adds a class to a tag, keeping any it already has."""
    if not name:
        return
    existing = tag.get("class") or []
    if isinstance(existing, str):
        existing = existing.split()
    if name not in existing:
        tag["class"] = existing + [name]


def strip_noise(scope: Tag) -> None:
    """Removes devsite chrome and unwraps its behavioural wrappers, in place."""
    for comment in scope.find_all(string=lambda text: isinstance(text, Comment)):
        comment.extract()
    for name in NOISE_TAGS:
        for node in scope.find_all(name):
            node.decompose()
    for selector in NOISE_SELECTORS:
        for node in scope.select(selector):
            node.decompose()
    for name in UNWRAP_TAGS:
        for node in scope.find_all(name):
            node.unwrap()


def _empty(node: Tag) -> bool:
    """True for a container left holding nothing after its content was extracted."""
    if node.name not in ("div", "p", "span", "code", "li", "ul", "aside", "a"):
        return False
    if node.get_text(strip=True):
        return False
    return not node.find(["img", "table", "hr", "pre", "input"])


def html_of(node: Tag | None, doc: Document, *, inner: bool = True) -> str | None:
    """A cleaned HTML string for a subtree: links rewritten, chrome attributes off, whitespace
    squeezed.

    Takes a subtree of `doc.root`, which is stripped of the site's chrome by construction -- see
    Document. That is why nothing is stripped here: re-walking each subtree two dozen times for
    chrome that cannot be there costs thousands of traversals on a page the size of
    `android.app.Activity`.

    Mutates `node`, which is deliberate: the callers extract the pieces they understand and then
    serialise whatever is left as prose, so there is no value in preserving the original tree.
    """
    if node is None:
        return None
    for tag in [node] + node.find_all(True):
        if tag.has_attr("href"):
            tag["href"] = doc.link(tag["href"])
            if tag.name == "a":
                add_class(tag, link_class(tag["href"]))
        if tag.has_attr("src"):
            tag["src"] = doc.link(tag["src"])
        for attr in [a for a in tag.attrs if a not in KEEP_ATTRS]:
            del tag[attr]
    # Innermost-first, so a div holding nothing but emptied divs is itself removed.
    for tag in reversed(node.find_all(["div", "p", "span", "code", "li", "ul", "aside", "a"])):
        if _empty(tag):
            tag.decompose()
    html = node.decode_contents() if inner else node.decode()
    # A <pre> is the only place the source's whitespace carries meaning.
    if "<pre" not in html:
        html = re.sub(r"\s+", " ", html)
    html = _EDGE_RULES.sub("", html)
    html = _EDGE_RULES_END.sub("", html)
    # The source indents inside <code>, where the leading space is visible once rendered.
    html = _CODE_PADDING.sub(r"\1", html)
    html = _CODE_PADDING_END.sub(r"\1", html)
    return html.strip() or None


def code_html(node: Tag | None, doc: Document) -> str | None:
    """A type or member fragment, unwrapped from the <code> the source puts around it.

    Every one of these lands inside a <code> in the output too, so carrying the source's wrapper
    through the JSON would only mean each template had to remember which fields already had one.
    Dackka adds a spacer <div> around the same <code>, which unwraps the same way.
    """
    if node is None:
        return None
    children = [child for child in node.children
                if isinstance(child, Tag) or str(child).strip()]
    if len(children) == 1 and isinstance(children[0], Tag) and children[0].name in ("code", "div"):
        return code_html(children[0], doc)
    return html_of(node, doc)


def text_of(node: Tag | None) -> str | None:
    """The node's text, with element boundaries that show as whitespace treated as whitespace.

    `get_text()` alone would run a signature's annotations together, because Dackka separates them
    with `<br>` rather than with text. Reading the tree instead of mutating it matters: callers
    take a fragment's text and then its HTML, and the `<br>` has to survive for the second.
    """
    if node is None:
        return None
    parts = []
    for descendant in node.descendants:
        if isinstance(descendant, Comment):
            continue
        if isinstance(descendant, Tag):
            if descendant.name in ("br", "p", "div", "li", "tr", "pre", "h1", "h2", "h3", "h4"):
                parts.append(" ")
        else:
            parts.append(str(descendant))
    return re.sub(r"\s+", " ", "".join(parts)).strip() or None


def link_of(anchor: Tag | None, doc: Document) -> dict | None:
    """A `{label, url}` cross-reference, with `linkClass` when the link is not a plain one.

    The class rides in the data rather than being decided by the template: a template is handed one
    page's JSON and cannot tell whether a URL names a row, and by the time it runs the question has
    already been answered here.
    """
    if anchor is None:
        return None
    label = text_of(anchor)
    if not label:
        return None
    entry = {"label": label}
    if anchor.has_attr("href"):
        entry["url"] = doc.link(anchor["href"])
        entry["linkClass"] = link_class(entry["url"])
    return {key: value for key, value in entry.items() if value is not None}


def first_sentence(html: str | None) -> str | None:
    """The javadoc summary rule: prose up to the first sentence-ending period, as plain text.

    Entities are resolved, so a brief reads as text rather than as `List&lt;String&gt;`. It is
    text and not HTML, so anything embedding it in HTML has to escape it again.
    """
    if not html:
        return None
    text = re.sub(r"\s+", " ", html_entities.unescape(re.sub(r"<[^>]+>", "", html))).strip()
    if not text:
        return None
    match = _FIRST_SENTENCE.match(text)
    return (match.group(1) if match else text).strip()


# --------------------------------------------------------------------------------------------
# Shared row / member parsing
# --------------------------------------------------------------------------------------------

def _versions(scope: Tag) -> dict:
    """The "added in" / "deprecated in" pair, from whichever of the two markups is present.

    doclava puts machine-readable API levels in `data-version-*` attributes; Dackka writes only
    prose, in a `#added-in` / `#deprecated-in` block. Both are read here so a caller does not care
    which flavor it has.
    """
    result: dict[str, str] = {}
    if scope.has_attr("data-version-added"):
        result["addedIn"] = scope["data-version-added"]
    if scope.has_attr("data-version-deprecated"):
        result["deprecatedIn"] = scope["data-version-deprecated"]
    for node_id, key in (("added-in", "addedIn"), ("deprecated-in", "deprecatedIn")):
        node = scope.find(id=node_id)
        if node is not None and key not in result:
            text = text_of(node) or ""
            pattern = _ADDED_IN if key == "addedIn" else _DEPRECATED_IN
            match = pattern.search(text)
            if match:
                result[key] = match.group(1)
    level = scope.find(class_="api-level")
    if level is not None:
        text = text_of(level) or ""
        for pattern, key in ((_ADDED_IN, "addedIn"), (_DEPRECATED_IN, "deprecatedIn")):
            match = pattern.search(text)
            if match and key not in result:
                result[key] = match.group(1)
    return result


def _member_cell(cell: Tag, doc: Document) -> tuple[str | None, str | None, str | None, str | None]:
    """Splits a summary cell into (member HTML, name, anchor, description HTML).

    The member itself is the cell's first `<code>` or `<div>`; everything after it is the summary
    sentence. Both flavors put the member's own anchor in that first element, which is what lets a
    summary row link into the detail section below it.
    """
    lead = None
    # A package summary and a class index put the link in a bare <a>, with no <code> around it.
    for child in cell.find_all(["code", "div", "a"], recursive=False):
        lead = child
        break
    if lead is None:
        return None, None, None, html_of(cell, doc)
    anchor_tag = lead if lead.name == "a" and lead.has_attr("href") else lead.find("a", href=True)
    name = text_of(anchor_tag) or text_of(lead)
    lead.extract()
    # A <code> or a spacer <div> is packaging and comes off; a link is the content itself.
    member = (code_html(lead, doc) if lead.name in ("code", "div")
              else html_of(lead, doc, inner=False))
    # Serialising rewrote the href in place, so the resolved URL can simply be read back.
    anchor = _own_anchor(_href(anchor_tag), doc)
    return member, name, anchor, html_of(cell, doc)


def _href(tag: Tag | None) -> str | None:
    """A tag's href, or None -- including for a tag that serialising left empty and removed."""
    if tag is None or tag.attrs is None:
        return None
    return tag.attrs.get("href")


def _own_anchor(resolved: str | None, doc: Document) -> str | None:
    """The member's anchor -- but only when the link really points at the page being parsed.

    An inherited row links to the member on the class that declares it, and that fragment is an id
    on *that* page. Recording it as this row's anchor would invite a consumer to build
    `<this page>#<anchor>`, which resolves nowhere.

    Takes the already-resolved URL rather than resolving again: the caller has just serialised the
    link, which rewrote this very href in place, so a second resolve would repeat a regex match
    and a relpath for each of half a million summary rows.
    """
    if not resolved:
        return None
    path, _, fragment = resolved.partition("#")
    if not fragment:
        return None
    return fragment if path in ("", doc.own_file) else None


def own_rows(table: Tag) -> list[Tag]:
    """The table's own rows, ignoring any nested table's.

    doclava never closes a summary table -- the scrape has one `</table>` fewer than it has
    `<table>` -- so every table after the first, and every detail section, is parsed as a child of
    it. Containment has to be tested rather than assumed: `find_all("tr")` alone would read the
    whole rest of the page as rows of the nested-classes table.
    """
    return [tr for tr in table.find_all("tr") if tr.find_parent("table") is table]


def own_nodes(table: Tag, selector: str) -> list[Tag]:
    """Matches inside this table but not inside a table nested in it. See own_rows."""
    return [node for node in table.select(selector) if node.find_parent("table") is table]


def own_heading(table: Tag, selector: str = "th") -> Tag | None:
    """This table's own header cell, ignoring one belonging to a table nested in it. See own_rows."""
    return next(iter(own_nodes(table, selector)), None)


def drop_header_row(header: Tag | None) -> None:
    """Removes the row a header cell sits in, once its table's rows have been read."""
    row = header.find_parent("tr") if header is not None else None
    if row is not None:
        row.decompose()


def parse_rows(table: Tag, doc: Document) -> list[dict]:
    """The data rows of a summary table, as {type, member, name, anchor, description}."""
    rows: list[dict] = []
    for tr in own_rows(table):
        cells = tr.find_all("td", recursive=False)
        if not cells:
            continue  # the header row, or an inherited group's wrapper row
        versions = _versions(tr)
        if len(cells) == 1:
            member, name, anchor, description = _member_cell(cells[0], doc)
            type_html = None
        else:
            member, name, anchor, description = _member_cell(cells[1], doc)
            type_html = code_html(cells[0], doc)
            if member is None:
                # An XML attribute row: the name is in the first cell, the prose in the second.
                member, name, anchor, _ = _member_cell(cells[0], doc)
                if member is None:
                    member, type_html = type_html, None
        row = {"type": type_html, "member": member, "name": name, "anchor": anchor,
               "description": description}
        row.update(versions)
        row = {k: v for k, v in row.items() if v is not None}
        if row.keys() - {"addedIn", "deprecatedIn"}:
            rows.append(row)  # an emptied wrapper row, left behind by a lifted-out group, is not one
    return rows


def parse_summary_table(table: Tag, doc: Document) -> dict | None:
    """One summary section. Inherited sections come back grouped by declaring class."""
    heading = own_heading(table, "th h3[id]")
    if heading is None:
        return None
    section = {"id": heading["id"], "title": text_of(heading) or heading["id"]}
    drop_header_row(heading)

    # The inherited groups are lifted out first so that what is left of the table is only the
    # rows the class declares itself. doclava's nested-classes table carries both at once.
    groups = []
    for control in own_nodes(table, ".expand-control"):
        container = control.parent
        inner = container.find("table") if container is not None else None
        source = link_of(control.find("a", href=True), doc) or {
            "label": re.sub(r"^From (class|interface)\s+", "", text_of(control) or "")
        }
        groups.append({"from": source,
                       "rows": parse_rows(inner, doc) if inner is not None else []})
        (container if container is not None else control).decompose()
    if groups:
        section["groups"] = groups
    rows = parse_rows(table, doc)
    if rows:
        section["rows"] = rows
    return section


def _drop_wrapper(node: Tag, keep: Tag) -> None:
    """Removes a table's leftover wrapper div -- unless it is the element being parsed."""
    if node is not None and node is not keep and node.name == "div" and _empty(node):
        node.decompose()


def _detail_tables(item: Tag, doc: Document) -> dict:
    """The Parameters / Returns / Throws / See also tables attached to one member.

    A table this does not recognise is documentation the author wrote, and is left exactly as it
    stands -- header row included. Reading the heading has to come before touching anything, or a
    table that turns out to be prose has already lost the row naming its columns.
    """
    found: dict[str, list] = {}
    for table in item.find_all("table"):
        header = own_heading(table)
        key = _DETAIL_TABLES.get((text_of(header) or "").lower())
        if key is None:
            continue  # a table inside the prose, left where it is
        drop_header_row(header)
        rows = [cells for cells in
                (tr.find_all("td", recursive=False) for tr in own_rows(table)) if cells]
        if key == "parameters":
            values = [_parameter(cells, doc) for cells in rows]
        elif key == "seeAlso":
            values = [link for link in (link_of(cells[0].find("a", href=True), doc)
                                        for cells in rows) if link]
        else:
            values = [_typed_row(cells, doc) for cells in rows]
        # Extended rather than assigned: the scrape does emit a member with two tables under one
        # heading, and assigning would drop the first table's rows silently.
        found.setdefault(key, []).extend(values)
        wrapper = table.parent
        table.extract()
        _drop_wrapper(wrapper, item)
    return {key: value for key, value in found.items() if value}


def _parameter(cells: list[Tag], doc: Document) -> dict:
    """One parameter row, normalised across the two flavors.

    doclava writes the name and the type in separate cells (`view` | `View`: prose). Dackka writes
    a single declaration (`@NonNull MenuProvider provider`) and the prose beside it. Both end up
    with a `declaration` to print and, where it can be told apart, a `name`.

    Which shape applies is decided by the flavor the page was written in, not by sniffing the
    markup: "does the prose cell open with a <code>" also fires on a Dackka description that
    happens to begin with a code reference, and then reads that reference as the parameter's type
    and tears it out of the prose.
    """
    if len(cells) < 2:
        return {"declaration": code_html(cells[0], doc)}
    if doc.flavor == "doclava":
        name = text_of(cells[0])
        body = cells[1]
        lead = body.find("code", recursive=False)
        type_html = code_html(lead.extract(), doc) if lead is not None else None
        description = html_of(body, doc)
        if description:
            # doclava writes "<code>Type</code>: prose"; the type is now its own field.
            description = re.sub(r"^\s*:\s*", "", description) or None
        return {"name": name, "type": type_html,
                "declaration": f"{type_html} {name}" if type_html and name else (type_html or name),
                "description": description}
    declaration = code_html(cells[0], doc)
    plain = text_of(cells[0]) or ""
    return {"name": plain.split()[-1] if plain else None, "declaration": declaration,
            "description": html_of(cells[1], doc)}


def _typed_row(cells: list[Tag], doc: Document) -> dict:
    """One Returns or Throws row: the type, and what it means."""
    if len(cells) < 2:
        return {"type": code_html(cells[0], doc)}
    return {"type": code_html(cells[0], doc),
            "description": html_of(cells[1], doc)}


def _extract_deprecation(scope: Tag, target: dict, doc: Document) -> None:
    """Lifts a deprecation notice out of the prose and into its own fields.

    Devsite uses `.caution` for any warning, not only for deprecation, so the wording has to say
    so before the block is treated as one. A general caution stays where the author put it.
    """
    for caution in scope.find_all(class_="caution"):
        # Searched, not anchored, and over the whole block: the sentence is boilerplate the doc
        # tool emits, and it does not always come first -- "<strong>Note:</strong> This class was
        # deprecated in API level 21." is the same notice with a lead-in.
        if not _DEPRECATION_NOTICE.search(text_of(caution) or ""):
            continue
        label = caution.find("strong")
        target["deprecated"] = True
        if label is not None:
            target["deprecationLabel"] = text_of(label.extract())
        target["deprecationNote"] = html_of(caution.extract(), doc)
        return


def parse_member(item: Tag, doc: Document) -> dict | None:
    """One entry from a detail section: signature, prose, parameters, returns, throws, see also.

    Works by subtraction. Every part with a known shape is extracted from the item; whatever is
    still there afterwards is the member's prose, which is the only way to keep the free-form
    documentation intact -- it contains headings, lists, tables and code samples that no fixed
    schema would survive.
    """
    heading = item.find(["h3", "h4"], id=True)
    if heading is None:
        return None
    member = {"name": text_of(heading), "anchor": heading["id"]}
    member.update(_versions(item))
    heading.extract()

    for block in item.find_all(class_="api-level"):
        block.decompose()
    for block in item.find_all(id="metadata-info-block"):
        block.decompose()

    signature = item.find("pre")
    if signature is not None:
        member["signature"] = text_of(signature)
        member["signatureHtml"] = html_of(signature.extract(), doc)

    _extract_deprecation(item, member, doc)

    for anchors in item.select("ul.nolist"):
        links = [link for link in (link_of(li.find("a", href=True), doc)
                                   for li in anchors.find_all("li")) if link]
        if links:
            member.setdefault("seeAlso", []).extend(links)
        heading_p = anchors.find_previous_sibling("p")
        if heading_p is not None and (text_of(heading_p) or "").rstrip(":") == "See also":
            heading_p.decompose()
        anchors.decompose()

    for paragraph in item.find_all("p"):
        match = _CONSTANT_VALUE.match(text_of(paragraph) or "")
        if match:
            member["constantValue"] = re.sub(r"\s+", " ", match.group(1)).strip() or None
            paragraph.decompose()

    tables = _detail_tables(item, doc)
    for key, value in tables.items():
        if key == "seeAlso":
            member.setdefault("seeAlso", []).extend(value)
        else:
            member[key] = value

    member["description"] = html_of(item, doc)
    if "deprecatedIn" in member:
        member["deprecated"] = True
    return {k: v for k, v in member.items() if v is not None}


def parse_detail_sections(doc: Document) -> list[dict]:
    """Every detail section, extracted from the content root.

    Both flavors head a section with `<h2 id="<section>_1">` and follow it with one element per
    member -- `div.api-item` in Dackka, a bare `div` in doclava -- up to the next `h2`.
    """
    sections = []
    for heading in doc.root.select('h2[id$="_1"]'):
        items = []
        for sibling in list(heading.find_next_siblings()):
            if not isinstance(sibling, Tag):
                continue
            if sibling.name in ("h1", "h2"):
                break
            if sibling.name != "div":
                continue
            member = parse_member(sibling.extract(), doc)
            if member:
                items.append(member)
        if not items:
            continue  # a prose section that happens to end in _1
        section_id = heading["id"][: -len("_1")]
        sections.append({"id": section_id, "title": text_of(heading) or section_id,
                         "members": items})
        heading.decompose()
    return sections


# --------------------------------------------------------------------------------------------
# Whole-page parsing
# --------------------------------------------------------------------------------------------

def content_root(soup: BeautifulSoup) -> tuple[Tag | None, str]:
    """The element holding the documentation, and which tool wrote it.

    doclava wraps a page in `#jd-content`; Dackka has no such wrapper, but every Dackka page opens
    with a `#header-block`, whose parent is the equivalent container.
    """
    node = soup.find(id="jd-content")
    if node is not None:
        return node, "doclava"
    header = soup.find(id="header-block")
    if header is not None:
        return header.parent, "dackka"
    body = soup.find("article") or soup.find("body")
    return body, "unknown"


def page_identity(relative: str) -> dict:
    """Package, name and qualified name, read off the path.

    The path is authoritative in a way the page text is not: `app/ActionBar.LayoutParams.html`
    says both that the package is `android.app` and that the class is nested, which the heading
    (just "ActionBar.LayoutParams") does not distinguish from a top-level class of that name.
    """
    parts = relative.removesuffix(".html").split("/")
    name = parts[-1]
    package = ".".join(parts[:-1])
    return {"library": parts[0], "packageName": package or None,
            "name": name, "qualifiedName": ".".join(parts) if package else name}


def class_kind(signature: str | None) -> str | None:
    if not signature:
        return None
    if "@interface" in signature:
        return "annotation"
    # doclava writes Java ("public @interface X", "public final class X"); Dackka writes the
    # declaration in Kotlin terms ("public annotation X", "public object X").
    for keyword in ("annotation", "enum", "object", "record", "interface", "class"):
        if re.search(rf"\b{keyword}\b", signature):
            return keyword
    return None


def _implemented_interfaces(parts: list[Tag], doc: Document) -> list[dict]:
    """The interfaces named after `implements` in the signature.

    `inheritance` records the superclass chain and stops there, so without this the interface list
    exists only as text inside `signatureHtml`. Both flavors write the keyword and then the links,
    doclava in a `code.api-signature` block of its own and Dackka inline in one `<pre>`, so
    reading the signature in document order covers both.

    A type argument is a link too, and it sits after the keyword like the interfaces do: without
    counting the brackets, `implements Comparable<Rational>` reads as though Rational implemented
    itself, and `Iterable<T>, Iterator<T>` names T twice. Both flavors write the brackets as
    text, so only links at bracket depth zero are interfaces.
    """
    interfaces: list[dict] = []
    seen: set[tuple[str, str | None]] = set()
    past_keyword, depth = False, 0
    for part in parts:
        for node in part.descendants:
            if isinstance(node, Tag):
                if past_keyword and depth == 0 and node.name == "a" and node.has_attr("href"):
                    link = link_of(node, doc)
                    if link and (link["label"], link.get("url")) not in seen:
                        seen.add((link["label"], link.get("url")))
                        interfaces.append(link)
                continue
            text = str(node)
            if "implements" in text:
                past_keyword = True
            depth = max(0, depth + text.count("<") - text.count(">"))
    return interfaces


def parse_class_page(doc: Document) -> dict:
    """A class / interface / enum / annotation page.

    Order matters: the detail sections and summary tables are lifted out first, then the header
    parts, and the content root is serialised last. What remains at that point is exactly the
    class-level prose, which on a page like `android.app.Activity` is several thousand words of
    guide material that no amount of schema would capture.
    """
    root, relative = doc.root, doc.relative
    page = {"page": "android-class"}
    page.update(page_identity(relative))
    page["flavor"] = doc.flavor
    page["versionScheme"] = version_scheme(doc.flavor, page["library"])

    details = parse_detail_sections(doc)

    summary, inherited = [], []
    for table in root.find_all("table"):
        if own_heading(table, "th h3[id]") is None:
            continue
        section = parse_summary_table(table, doc)
        wrapper = table.parent
        table.extract()
        _drop_wrapper(wrapper, root)
        if section is None:
            continue
        (summary if "rows" in section else inherited).append(section)
    by_order = lambda section: SECTION_ORDER.get(section["id"], 99)
    page["summary"] = sorted(summary, key=by_order)
    page["inherited"] = sorted(inherited, key=by_order)
    page["details"] = sorted(details, key=by_order)

    for heading in root.find_all(["h2"], id="summary"):
        heading.decompose()

    heading = root.find("h1")
    if heading is not None:
        page["title"] = text_of(heading)
        heading.decompose()

    # doclava spreads a signature over several `code.api-signature` blocks (modifiers, extends,
    # implements); Dackka writes one `<pre>`. Either way the whole of it is one signature.
    parts = root.select("code.api-signature")
    if parts:
        page["signature"] = " ".join(filter(None, (text_of(part) for part in parts)))
        page["implements"] = _implemented_interfaces(parts, doc)
        holder = parts[0].find_parent("p")
        page["signatureHtml"] = " ".join(
            filter(None, (html_of(part, doc) for part in parts)))
        for part in parts:
            part.extract()
        if holder is not None and _empty(holder):
            holder.decompose()
    else:
        signature = root.find("pre")
        if signature is not None:
            page["signature"] = text_of(signature)
            page["implements"] = _implemented_interfaces([signature], doc)
            holder = signature.find_parent("p")
            page["signatureHtml"] = html_of(signature.extract(), doc)
            if holder is not None and _empty(holder):
                holder.decompose()
    page["kind"] = class_kind(page.get("signature"))

    tree = root.find("table", class_="jd-inheritance-table")
    if tree is not None:
        chain = []
        for row in tree.find_all("tr"):
            cells = row.find_all("td")
            if not cells:
                continue
            entry = link_of(cells[-1].find("a", href=True), doc) or {
                "label": text_of(cells[-1])}
            if entry.get("label"):
                chain.append(entry)
        page["inheritance"] = chain
        tree.extract()

    for node_id, key in (("subclasses-direct", "knownDirectSubclasses"),
                         ("subclasses-indirect", "knownIndirectSubclasses")):
        listing = root.find(id=node_id)
        if listing is None:
            continue
        page[key] = [link for link in (link_of(a, doc)
                                       for a in listing.find_all("a", href=True)) if link]
        # The expandable table beside the comma list repeats it with descriptions; the outer
        # container goes so neither copy lands in the prose.
        outer = listing.find_parent("table") or listing.parent
        if outer is not None:
            outer.decompose()
    for leftover in root.find_all(id=re.compile(r"^subclasses-(direct|indirect)-summary$")):
        container = leftover.find_parent("table") or leftover
        container.decompose()

    for block in root.find_all(id="api-info-block"):
        block.decompose()
    for block in root.find_all(id="header-block"):
        page.update(_header_metadata(block, doc))
        block.decompose()
    for block in root.find_all(class_="api-level"):
        block.decompose()

    page.update(_versions(root))
    _extract_deprecation(root, page, doc)
    if "deprecatedIn" in page:
        page["deprecated"] = True
    page["description"] = html_of(root, doc)
    page["brief"] = first_sentence(page["description"])
    return {k: v for k, v in page.items() if v not in (None, [], {})}


def _header_metadata(block: Tag, doc: Document) -> dict:
    """Dackka's header block: Maven coordinates, a source link, and the release versions."""
    result: dict = {}
    coordinates = block.find(id="maven-coordinates")
    if coordinates is not None:
        artifact = link_of(coordinates.find("a", href=True), doc)
        if artifact is None:
            label = re.sub(r"^Artifact:\s*", "", text_of(coordinates) or "")
            artifact = {"label": label} if label else None
        if artifact:
            result["artifact"] = artifact
    source = block.find(id="source-link")
    if source is not None:
        link = source.find("a", href=True)
        if link is not None:
            result["sourceUrl"] = doc.link(link["href"])
    result.update(_versions(block))
    return result


def listing_identity(kind: str, relative: str) -> dict:
    """Who a listing page is about.

    `page_identity` reads a type's identity off its path, which is the wrong question for these:
    a package page is about the directory it sits in, not about a file called `package-summary`,
    and a class or package index is about no package at all. Naming them the way `page_identity`
    would leaves a package called `androidx.packages` in the schema and, because the root index
    counts anything with a `packageName`, in every package's type count too.
    """
    identity = page_identity(relative)
    package = identity["packageName"]
    if kind == "android-package" and package:
        return {"library": identity["library"], "packageName": package,
                "name": package, "qualifiedName": package}
    return {"library": identity["library"], "name": identity["name"]}


def _group_table(heading: Tag) -> Tag | None:
    """The table belonging to this heading: the next one before the next heading, or none.

    Bounded deliberately. `find_next_sibling("table")` walks to the end of the page, so a heading
    with no table of its own -- a note, an introduction -- would claim the *next* heading's table,
    and that heading would then find its table already gone and be dropped entirely.
    """
    for sibling in heading.find_next_siblings():
        if not isinstance(sibling, Tag):
            continue
        if sibling.name in ("h1", "h2"):
            return None
        if sibling.name == "table":
            return sibling
        nested = sibling.find("table")
        if nested is not None:
            return nested
    return None


def parse_listing_page(doc: Document, kind: str) -> dict:
    """A page that is just grouped lists of links: a package summary, or a class/package index.

    All three share one shape -- `<h2>` naming the group, a table of (entry, description) rows
    under it -- so they share one parser and one template.
    """
    root, relative = doc.root, doc.relative
    page = {"page": kind}
    page.update(listing_identity(kind, relative))
    page["versionScheme"] = version_scheme(doc.flavor, page["library"])
    groups = []
    for heading in root.find_all("h2", id=True):
        table = _group_table(heading)
        if table is None:
            continue
        groups.append({"id": heading["id"], "title": text_of(heading) or heading["id"],
                       "rows": parse_rows(table, doc)})
        container = table.parent
        table.extract()
        _drop_wrapper(container, root)
        heading.decompose()
    if not groups:
        # A package with a single unheaded table, or an index with one list.
        table = root.find("table")
        if table is not None:
            groups.append({"id": "entries", "title": "Entries",
                           "rows": parse_rows(table, doc)})
            table.extract()
    page["groups"] = groups
    heading = root.find("h1")
    if heading is not None:
        page["title"] = text_of(heading)
        heading.decompose()
    page.update(_versions(root))
    for block in root.find_all(id=["api-info-block", "header-block"]):
        block.decompose()
    page["description"] = html_of(root, doc)
    return {k: v for k, v in page.items() if v not in (None, [], {})}


def page_kind(doc: Document) -> str:
    """Which of the three scraped page shapes this is."""
    root, flavor = doc.root, doc.flavor
    heading = root.find("h1") if root is not None else None
    heading_id = heading.get("id", "") if heading is not None else ""
    if heading_id in ("class-index", "package-index"):
        return "android-index"
    if flavor == "doclava" and (heading is None or "api-title" not in heading.get("class", [])):
        return "android-package"
    return "android-class"


def convert(source: Path, relative: str, linker: Linker) -> dict | None:
    """Reads one scraped page and returns its JSON document."""
    doc = Document.read(source, relative, linker)
    if doc is None:
        return None
    kind = page_kind(doc)
    if kind == "android-class":
        return parse_class_page(doc)
    return parse_listing_page(doc, kind)


# --------------------------------------------------------------------------------------------
# Generated navigation
# --------------------------------------------------------------------------------------------

def package_page(package: str, entries: list[dict], relative: str) -> dict:
    """A stand-in package summary for a package the scrape has no page for.

    Dackka writes none at all, so without this every Jetpack class page is an island and the
    scraped `packages.html` links nowhere.
    """
    by_kind: dict[str, list[dict]] = {}
    for entry in sorted(entries, key=lambda e: e["name"]):
        kind = entry.get("kind")
        by_kind.setdefault(kind if kind in _PACKAGE_GROUPS else "class", []).append(entry)
    groups = []
    for kind in _PACKAGE_GROUPS:
        if kind not in by_kind:
            continue
        rows = []
        for entry in by_kind[kind]:
            # `member` and `description` are HTML fields and `brief` is text, so it is
            # escaped on the way in.
            brief = entry.get("brief")
            row = {"member": f'<a href="{entry["file"]}">'
                             f'{html_entities.escape(entry["name"])}</a>',
                   "name": entry["name"],
                   "description": html_entities.escape(brief) if brief else None}
            for key in ("addedIn", "deprecatedIn"):
                if entry.get(key):
                    row[key] = entry[key]
            rows.append({k: v for k, v in row.items() if v is not None})
        groups.append({"id": _PACKAGE_GROUPS[kind][0], "title": _PACKAGE_GROUPS[kind][1],
                       "rows": rows})
    library = relative.split("/")[0]
    # Taken from the types the page lists rather than from the path: a package under `android/`
    # can be Dackka-written Jetpack documentation, versioned by release and not by API level.
    scheme = next((entry["versionScheme"] for entry in entries if entry.get("versionScheme")),
                  version_scheme("", library))
    return {"page": "android-package", "generated": True, "library": library,
            "versionScheme": scheme,
            "packageName": package, "name": package, "qualifiedName": package, "title": package,
            "groups": groups}


def overview_page(records: list[dict], extra_indexes: list[str]) -> dict:
    """The root index: every library, its packages, and the scraped indexes worth linking to."""
    libraries: dict[str, dict[str, int]] = {}
    for record in records:
        package = record.get("packageName")
        # Types only: a package page and an index page both sit in a package without being one of
        # the types it holds, and counting them overstates every package they appear in.
        if not package or record.get("page") != "android-class":
            continue
        counts = libraries.setdefault(record["library"], {})
        counts[package] = counts.get(package, 0) + 1
    groups = []
    for library in sorted(libraries):
        rows = []
        for package in sorted(libraries[library]):
            path = package.replace(".", "/") + "/package-summary.json"
            count = libraries[library][package]
            rows.append({"member": f'<a href="{path}">{package}</a>',
                         "name": package,
                         "description": f"{count} type{'s' if count != 1 else ''}"})
        groups.append({"id": library, "title": library, "rows": rows})
    if extra_indexes:
        rows = [{"member": f'<a href="{path}">{path}</a>', "name": path}
                for path in sorted(extra_indexes)]
        groups.append({"id": "indexes", "title": "Scraped indexes", "rows": rows})
    return {"page": "android-index", "generated": True, "name": "Android API reference",
            "title": "Android API reference", "groups": groups}


# --------------------------------------------------------------------------------------------
# Driver
# --------------------------------------------------------------------------------------------

_WORKER: dict = {}


def _init_worker(known: set[str], out: str, indent: int | None) -> None:
    _WORKER["linker"] = Linker(known)
    _WORKER["out"] = Path(out)
    _WORKER["indent"] = indent


def write_json(target: Path, document: dict, indent: int | None) -> None:
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(json.dumps(document, ensure_ascii=False, indent=indent), encoding="utf-8")


def _run_one(job: tuple[str, str]) -> dict | None:
    base, relative = job
    try:
        document = convert(Path(base) / relative, relative, _WORKER["linker"])
    except Exception as error:  # one malformed page must not lose the other 12,000
        print(f"  ! {relative}: {type(error).__name__}: {error}", file=sys.stderr)
        return None
    if document is None:
        print(f"  ! {relative}: no recognisable content", file=sys.stderr)
        return None
    write_json(_WORKER["out"] / json_path(relative), document, _WORKER["indent"])
    return {"path": relative, "page": document["page"], "library": document.get("library"),
            "packageName": document.get("packageName"), "name": document.get("name"),
            "kind": document.get("kind"), "brief": document.get("brief"),
            "addedIn": document.get("addedIn"),
            "deprecatedIn": document.get("deprecatedIn"),
            "versionScheme": document.get("versionScheme")}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Generate JSON documentation from the scraped Android reference HTML.")
    parser.add_argument("roots", nargs="+", type=Path,
                        help="scraped trees to convert, e.g. ../android ../androidx")
    parser.add_argument("--out", required=True, type=Path, help="output directory")
    parser.add_argument("--workers", type=int, default=os.cpu_count() or 4)
    parser.add_argument("--only", help="convert only paths containing this substring")
    parser.add_argument("--limit", type=int, help="stop after this many pages (for a smoke run)")
    parser.add_argument("--indent", type=int, help="pretty-print with this indent")
    parser.add_argument("--no-navigation", action="store_true",
                        help="skip the generated package summaries and root index")
    args = parser.parse_args(argv)

    for root in args.roots:
        if not root.is_dir():
            parser.error(f"not a directory: {root}")

    found = find_pages(args.roots)
    if not found:
        parser.error("no .html pages found")
    # Built from every page found, not just the ones being converted, so a `--only` run still
    # links correctly into the rest of the tree.
    known = {relative[: -len(".html")] for _, relative in found}

    pages = found
    if args.only:
        pages = [entry for entry in pages if args.only in entry[1]]
    if args.limit:
        pages = pages[: args.limit]
    if not pages:
        parser.error(f"no .html pages matched --only {args.only!r}")
    print(f"==> {len(pages)} pages -> {args.out}")

    start = time.time()
    jobs = [(str(base), relative) for base, relative in pages]
    records: list[dict] = []
    if args.workers > 1:
        with ProcessPoolExecutor(max_workers=args.workers, initializer=_init_worker,
                                 initargs=(known, str(args.out), args.indent)) as pool:
            for done, record in enumerate(pool.map(_run_one, jobs, chunksize=8), 1):
                if record:
                    records.append(record)
                if done % 500 == 0:
                    print(f"    {done}/{len(pages)}")
    else:
        _init_worker(known, str(args.out), args.indent)
        for done, job in enumerate(jobs, 1):
            record = _run_one(job)
            if record:
                records.append(record)
            if done % 500 == 0:
                print(f"    {done}/{len(pages)}")

    if not args.no_navigation:
        scraped_packages = {record["path"].rsplit("/", 1)[0] for record in records
                            if record["path"].endswith("/package-summary.html")}
        classes = [record for record in records if record["page"] == "android-class"
                   and record.get("packageName")]
        by_package: dict[str, list[dict]] = {}
        for record in classes:
            entry = dict(record)
            entry["file"] = json_path(record["path"].rsplit("/", 1)[-1])
            by_package.setdefault(record["packageName"], []).append(entry)
        generated = 0
        for package, entries in by_package.items():
            directory = package.replace(".", "/")
            if directory in scraped_packages:
                continue  # the scrape has a real page for this one
            relative = f"{directory}/package-summary.json"
            write_json(args.out / relative, package_page(package, entries, relative), args.indent)
            generated += 1
        indexes = [json_path(record["path"]) for record in records
                   if record["page"] == "android-index"]
        write_json(args.out / "index.json", overview_page(records, indexes), args.indent)
        print(f"==> generated {generated} package summaries and index.json")

    elapsed = time.time() - start
    print(f"==> wrote {len(records)} pages in {elapsed:.0f}s")
    return 0 if len(records) == len(pages) else 1


if __name__ == "__main__":
    sys.exit(main())
