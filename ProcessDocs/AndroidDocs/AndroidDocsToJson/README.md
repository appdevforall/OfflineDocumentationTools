# Android reference docs -> JSON -> HTML

Turns the scraped developer.android.com reference HTML under `ProcessDocs/AndroidDocs` into a JSON
documentation tree, and renders that tree back to browsable HTML with Pebble templates.

```
android/  androidx/          android_docs_to_json.py         renderer/render.sh
scraped HTML  ---------->    JSON documentation tree  ---->   browsable HTML
```

Two steps rather than one because the JSON is the deliverable: it is what a tooltip generator, a
search index, or the documentation database can read without parsing HTML again. The renderer
exists to show that the JSON kept everything, and to give the corpus a browsable form.

## Running it

The extractor needs `beautifulsoup4` and `lxml`, both already in the repository's
`requirements.txt`:

```bash
pip install -r ../../../requirements.txt
```

```bash
# HTML -> JSON (about 60 seconds for all 12,106 pages on 10 cores)
./android_docs_to_json.py ../android ../androidx --out /tmp/android-json

# JSON -> HTML (about 4 seconds)
./renderer/render.sh /tmp/android-json /tmp/android-html
(cd /tmp/android-html && python3 -m http.server 8000)   # then browse /index.html
```

Useful while working on either half:

| Flag | |
|---|---|
| `--only SUBSTRING` | convert just the paths containing it, still linking correctly into the rest |
| `--limit N` | stop after N pages |
| `--indent 2` | pretty-print, for reading the output |
| `--workers N` | defaults to the core count; `--workers 1` to get a traceback out of a crash |
| `--no-navigation` | skip the generated package summaries and root index |

Tests: `python -m pytest test_android_docs_to_json.py` for the extractor, and
`(cd renderer && ./gradlew test)` for the renderer and its templates.

## The two flavors of scrape

The scrape holds pages written by two different documentation tools, and they do not look alike:

| | `android/` | `androidx/` |
|---|---|---|
| Written by | doclava | Dackka |
| Content root | `#jd-content` | the parent of `#header-block` |
| Versioned by | API level (`data-version-added="34"`) | artifact release (`Added in 1.4.0`) |
| Member entries | bare `<div>`s after `<h2 id="...._1">` | `div.api-item` |
| Parameters | name and type in separate cells | one declaration per cell |
| Package pages | three, in the whole tree | none |

The *documentation* in them is the same shape, though, so the extractor reads both into one schema
and one template renders both. Where a difference cannot be reconciled it is recorded rather than
papered over: `flavor` says which tool wrote the page, and `versionScheme` says what its version
numbers count, so a template can word "Added in API level 34" and "Added in 1.4.0" correctly
without either being baked into the data.

Two properties of the source are worth knowing about before changing the extractor:

- **doclava never closes a summary table.** The scrape has exactly one `</table>` fewer than it has
  `<table>`, so every parser nests the rest of the page — the other summary tables, and all the
  detail sections — inside the first one. Anything that walks rows or controls has to test
  containment rather than use `find_all`, which is what `own_rows` and `own_nodes` are for.
- **Anchors are the member's erased signature, percent-encoded where doclava wrote them that way**
  (`#addContentView(android.view.View,%20android.view.ViewGroup.LayoutParams)`). The links in the
  scrape point at exactly those strings, so both sides are carried through untouched. Re-encoding
  or decoding either one would break every inbound link in the corpus.

## Links

Every `/reference/...` URL in the scrape is rewritten. If the scrape contains the page, the link
becomes a relative path to its `.json`; if it does not — `java.lang.Object`, the Kotlin view of a
page, a guide, a release note — it becomes an absolute `https://developer.android.com/...` URL.
Nothing is left as a site-absolute path, because those resolve against whatever host serves the
output.

That makes the JSON tree self-contained: 1.38 million internal links, of which four are broken,
all four because the source HTML has a malformed `href`. The renderer swaps `.json` for `.html` as
it writes, which is why the same relative paths work in both trees.

## Generated pages

Two kinds of page are synthesised rather than scraped, because without them the tree has no entry
point and no way to walk from a class to its neighbours:

- `<package>/package-summary.json` for every package the scrape has no page for — 799 of them.
  The rows come from the class pages themselves, grouped by kind, carrying each type's brief and
  its versions. Links in the scraped
  `androidx/packages.html` point at exactly these paths, so they resolve.
- `index.json` at the root: every library, its packages, and the scraped indexes.

Both are marked `"generated": true`.

## Schema

One JSON file per HTML page, at the same relative path with a `.json` extension. Empty and null
values are dropped rather than written out, so a template has to be defensive about missing keys —
most pages carry only a handful of the fields below.

Every page has `page` (which template renders it), `library` and `name`. A type page and a
package page also have `packageName` and `qualifiedName`; a class or package index has neither,
because its file name names no package.

A type's identity is read off its path, which is authoritative in a way the heading is not:
`app/ActionBar.LayoutParams.html` says the class is nested, and the heading does not. A package
page's identity is the directory it sits in, so both the three scraped package pages and the 799
generated ones are named `android.database` rather than `package-summary`.

### `page: "android-class"`

A class, interface, enum or annotation.

| Field | |
|---|---|
| `flavor` | `doclava` or `dackka` |
| `versionScheme` | `api-level` or `library-version` — what `addedIn` counts, decided by `flavor` and not by the path: `android/support/v4/media` sits under `android/` but is Dackka-written Jetpack documentation, versioned `1.1.0` |
| `kind` | `class`, `interface`, `enum`, `annotation`, `object`, `record` |
| `title` | the page's own heading |
| `signature`, `signatureHtml` | the declaration, as text and as linked HTML |
| `addedIn`, `deprecatedIn` | version numbers, unformatted |
| `artifact`, `sourceUrl` | Maven coordinates and a source link (Jetpack only) |
| `inheritance` | the superclass chain, `java.lang.Object` first, this type last and unlinked |
| `implements`, `knownDirectSubclasses`, `knownIndirectSubclasses` | `{label, url}` lists |
| `deprecated`, `deprecationLabel`, `deprecationNote` | set when the type itself is deprecated |
| `description` | the class-level prose, as HTML |
| `brief` | its first sentence, as plain text |
| `summary` | sections of members the type declares |
| `inherited` | sections of members it only inherits |
| `details` | the full entry for each declared member |

A deprecation notice is lifted out of the prose into `deprecationLabel` and `deprecationNote`,
which is what keeps `brief` the class's actual first sentence rather than "This class was
deprecated in API level 21." Devsite uses the same `.caution` markup for any warning, so only a
block whose wording says "deprecated" is treated as one.

A **summary section** is `{id, title, rows}`, and may also carry `groups` — doclava's nested-classes
table lists declared and inherited types together. A **row** is `{type, member, name, anchor,
description, addedIn, deprecatedIn}`, where `member` is HTML that already links to the member's own
detail entry. `anchor` is present only when that link points at this page: an inherited row links
to the member on the class that declares it, and that fragment is an id on *that* page, so there
is no anchor for it here. An **inherited section** is `{id, title, groups}`, each group `{from: {label, url},
rows}`.

A **detail member** is:

| Field | |
|---|---|
| `name`, `anchor` | the short name, and the id to link to |
| `signature`, `signatureHtml` | |
| `addedIn`, `deprecatedIn`, `deprecated`, `deprecationLabel`, `deprecationNote` | |
| `description` | the member's prose, as HTML |
| `parameters` | `{name, type, declaration, description}` |
| `returns`, `throws` | `{type, description}` |
| `seeAlso` | `{label, url}` |
| `constantValue` | e.g. `1 (0x00000001)` |

`declaration` is what a template should print for a parameter: the two flavors disagree about how
much of a parameter is its type, and `declaration` is where that is reconciled.

`implements` holds only the interfaces themselves. A generic interface's type arguments are links
in the same clause — `implements Comparable<Rational>` links both names — and only the ones outside
the brackets are interfaces; the full clause is still in `signature` and `signatureHtml`.

Fragments that name a type or a member — `type`, `declaration`, `member` — are stored without the
`<code>` the source wraps them in, because they land inside a `<code>` in the output anyway. A
template that prints one adds the wrapper.

### `page: "android-package"` and `page: "android-index"`

Both are grouped lists: `{groups: [{id, title, rows}]}`, with the same row shape, plus `title` and
an optional `description`. Package pages are the three scraped `package-summary.html` files and the
799 generated ones; index pages are the root `index.json` and the scraped `classes.html` /
`packages.html`.

## The renderer

`renderer/` is a small Gradle project: Pebble templates, and just enough Java to walk the tree and
pick a template per page.

| | |
|---|---|
| `templates/base.peb` | page skeleton, breadcrumb, footer |
| `templates/macros.peb` | summary tables, inherited groups, member entries, version notes |
| `templates/class.peb` | a type page |
| `templates/package.peb` | a package summary |
| `templates/index.peb` | an index |
| `AndroidDocRendererTest.java` | renders a page of each kind through the real templates |
| `static/stylesheet.css` | |
| `AndroidDocRenderer.java` | walks the JSON tree, `page` field -> template |
| `AndroidDocExtension.java` | the `href`, `doc` and `anchor` filters |

The `page` field selects the template and the parsed JSON becomes the template context directly, so
a template reads the same field names that appear in the JSON.

Autoescaping is on. The documentation fields are HTML already, and go through the `doc` filter,
which rewrites the links inside them and marks the result safe; everything else — names,
signatures, titles — is escaped by default rather than by memory.

The class names in the templates and the stylesheet are the ones the scrape used
(`api-signature`, `api-item`, `caution`, `jd-inheritance-table`, `responsive`). A fragment of
documentation prose carried through the JSON keeps the classes it was written with, so renaming
them would leave that prose unstyled.

The pages need no JavaScript: inherited members collapse with `<details>`.

The tests are there because the failures here are silent ones. Autoescaping aside, the engine runs
with `strictVariables(false)`, so a field the extractor stops writing renders as nothing rather
than as an error, and a macro called with a `macros.` prefix renders empty too (Pebble imports
macros into the namespace unprefixed). Either mistake would blank out part of every page while the
build still reported success, so the tests assert that content appears rather than that markup
matches byte for byte.

This is deliberately separate from the Pebble renderer that turns the Dokka plugin's
javadoc-mode JSON into HTML (`Dokka-plugin-kdoc2json/pebble-renderer`, where that pipeline lands).
That renderer's page skeleton and templates are built around javadoc's page structure; these are
built around the reference site's, which is a different shape (summary tables keyed by section,
API-level badges, collapsible inherited members). The two share only the `.json` -> `.html` link
convention, which is a dozen lines.
