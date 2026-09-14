# Android reference docs -> JSON -> HTML

Turns the scraped developer.android.com reference HTML under `ProcessDocs/AndroidDocs` into a JSON
documentation tree, and renders that tree back to browsable HTML with Pebble templates.

```
                       android_docs_to_json.py          renderer/render.sh
android/ androidx/  ------------------------->  JSON  ------------------->  browsable HTML
scraped HTML                                     |     load_android_json_db.py
                                                 +--------------------------->  documentation.db
```

Two steps rather than one because the JSON is the deliverable: it is what a tooltip generator, a
search index, or the documentation database can read without parsing HTML again. The renderer
exists to show that the JSON kept everything, to give the corpus a browsable form, and -- since it
runs the same templates the database stores -- to preview what the app will serve.

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

```bash
# JSON -> documentation.db (about 90 seconds; the source database is never written to)
./load_android_json_db.py /tmp/android-json \
    --source ~/Desktop/documentation.db --out /tmp/staged.db
./verify_android_json_db.py /tmp/staged.db --sample 40

# Retrain the shared Brotli dictionary over the changed corpus and recompress every row
# (about 6 minutes), then prove nothing changed but the encoding.
cp /tmp/staged.db /tmp/preremint.db
../../ProcessKotlinDocs/ProcessKotlinWebsiteJSON/remint_dictionary.py /tmp/staged.db --no-backup
../../ProcessKotlinDocs/ProcessKotlinWebsiteJSON/verify_remint_dictionary.py \
    /tmp/preremint.db /tmp/staged.db
mv /tmp/staged.db ~/Desktop/documentation_androidjson.db
```

Reminting is not optional if size matters: see below.

Tests: `python -m pytest` for the extractor and the loader, and
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
Any other site-absolute path (`/guide/...`, `/jetpack/...`) is absolutised the same way. Nothing is
left as a site-absolute path, because those resolve against whatever host serves the output.

Links the reference makes to other people's documentation — `jspecify.dev`, `kotlinlang.org`,
`errorprone.info` and 128 other hosts — were already absolute and pass through untouched.

That makes the JSON tree self-contained: 1,390,377 internal links, of which 14 are broken across 4
distinct targets, every one because the source HTML has a malformed `href`. The renderer swaps
`.json` for `.html` as it writes, which is why the same relative paths work in both trees.

## Links the reader should not trust

Every link is classified as it is resolved, and the two kinds that are not a plain hop within this
documentation are marked so the stylesheet can colour them red -- the same `broken-link` and
`external-link` classes the scraped pages carried, and the same reason: a reader can see which
links need the network and which lead nowhere without tapping one.

| | | across the corpus |
|---|---|---|
| plain | a relative path to another page here | 1,344,383 |
| `external-link` | leaves the app; needs a network connection | 335,197 |
| `broken-link` | names nothing, here or anywhere | 14 |

`link_class` reads the answer off the resolved URL rather than tracking it through the resolving,
because it is there to be read: a link into the tree is a relative `.json` path, since that is the
only thing the linker produces for a page it found; a fragment stays on this page; anything with a
scheme of its own leaves the app. What is left is a URL the linker could not place.

Where the off-site links actually go, counted rather than assumed — the page footer used to claim
they all went back to developer.android.com, and one in seven does not:

| | |
|---|---|
| developer.android.com | 269,735 (85.5%) |
| jspecify.dev | 30,406 |
| kotlinlang.org | 6,550 |
| errorprone.info, guava.dev, junit.org, reactivex.io, truth.dev, checkerframework.org | 12,486 |
| 122 other hosts | 6,162 |

All 14 of those are one defect in the source: the `href` value itself is wrapped in quote
characters, as in `href='"https://developer.android.com/guide/..."'`, so the URL a browser sees
starts with a `"` and resolves nowhere. They are marked rather than repaired -- stripping the
quotes would turn each into a perfectly good off-site link, which is a change to what the
documentation says rather than to how it is shown.

The class rides in the data: `linkClass` on a `{label, url}` cross-reference, and a `class`
attribute on an `<a>` inside a documentation fragment. It has to, because a template is handed one
page's JSON and cannot tell whether a URL names a row.

## Generated pages

Two kinds of page are synthesised rather than scraped, because without them the tree has no entry
point and no way to walk from a class to its neighbours:

- `<package>/package-summary.json` for every package the scrape has no page for — 799 of them.
  The rows come from the class pages themselves, grouped by kind, carrying each type's brief and
  its versions. Links in the scraped
  `androidx/packages.html` point at exactly these paths, so they resolve.
- `index.json` at the root: every library, its packages, and the scraped indexes. Each package
  row says what it holds rather than how many pages it has -- `6 interfaces, 19 classes · 15
  nested types, 11 constructors, 187 methods, 8 fields, 127 constants` -- counted from the pages
  themselves and carried on the row as `types` and `members` maps as well as the readable line, so
  a caller is not left parsing prose. Members are what a type declares; an inherited method is
  counted on the class that declares it.

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
| `linkClass` (on any `{label, url}`) | `external-link` or `broken-link`; absent for a plain link |
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

## Into documentation.db

The database already carried this reference as 12,106 rows of scraped HTML: each the whole page,
with the site's stylesheet inlined into it, and no structure a caller can address.
`load_android_json_db.py` replaces those rows with the JSON and adds the templates that render it,
so a page is served the way the database already serves the Kotlin docs -- a row's `templateId`
names a template, and the row's JSON is that template's context.

| What it writes | |
|---|---|
| `Templates` | `android-class.peb`, `android-package.peb`, `android-index.peb` |
| `assets/android-reference.css` | the one stylesheet all 12,906 pages link to |
| `Content` | one row per page, at the path the HTML row used, `templateId` set |
| `DocumentationDatabaseVersion` | a minor bump saying what changed |

The source database is never written to: it is copied first, and every write lands on the copy,
which is the artifact.

Three things the loader has to get right:

- **Paths keep the case the database uses.** The scrape is read off a case-insensitive filesystem,
  where `android.os.strictmode` (a package) and `android.os.StrictMode` (a class) cannot each have
  a directory: the package's 25 pages sit under `StrictMode/`. The database has them under
  `strictmode/`, which is what every link in the corpus says too, so the database's spelling wins
  for row paths and for links -- otherwise those 25 pages would be written beside the rows they
  replace, and linked to at a path holding nothing.
- **Links lose the `.json` the extractor wrote for its own tree** -- the same swap the renderer
  does, for the reason given under the renderer below.
- **The stylesheet, the root index and a page's own package summary go into the stored JSON**,
  because the server hands a template the row's JSON and nothing else. They are the only fields
  that say where pages are served from rather than what the documentation is.

### What it costs

Measured on the real database at each step, not estimated:

| | HTML | JSON | JSON, reminted |
|---|---|---|---|
| The same 12,106 pages, stored | 29.7 MB | 31.8 MB | **26.8 MB** |
| All Android pages (12,906 after) | 29.7 MB | 32.3 MB | **27.4 MB** |
| Whole database | — | — | same as its source, ±1 MB |
| Decompressed per page, which is what the device expands | 46 KB | **22 KB** | 22 KB |

The whole-database figure depends on which source you start from -- ADFA-5552's media
optimisation took it from 249 MB to 178 MB while leaving the Android pages alone -- so the row
that means anything is the Android one.

The middle column is why the remint is part of the job rather than a nicety. Content in this
database is Brotli-compressed against one shared dictionary, and the dictionary in the source was
trained on the corpus as it then was -- mostly HTML, with the site's stylesheet inlined thousands
of times over. It compresses those old rows extremely well and has never seen the shape of the new
ones, so simply swapping HTML for JSON makes the pages 7% *larger*.

Retraining it over the changed corpus takes that back and more: the same pages end up 10% smaller
than the HTML they replaced, all 12,906 pages together take less room than the 12,106 did, and the
whole database comes out the size it started. `remint_dictionary.py` does the retraining and
recompresses every row; `verify_remint_dictionary.py` then decompresses both databases and
compares plaintexts, because a row recompressed against a mismatched dictionary decodes without
error into *different* bytes -- 30,415 of 30,415 byte-identical is the check that matters.

Beside the size: half the bytes to decompress and parse per page, one stylesheet fetched once
instead of a copy inlined into all 12,906 pages, and fields a caller can address without parsing
HTML.

`verify_android_json_db.py` answers the question the loader cannot. It pulls the stored JSON and
the stored template back out of the database and renders them through Pebble, configured the way
the server configures it, then checks that no page is left without a template and no link names a
row that is not there.

The row-writing itself is `populate_db.py`'s and `migrate_content_to_dictionary_brotli.py`'s:
dictionary compression, the chunk boundary `WebServer.kt` reassembles on, the update-in-place rule
the `AddBook` trigger imposes. Those are imported rather than reimplemented, which needed one fix
in `populate_db.py` first -- it imported `build_nav` and `md_to_json` at module scope, and both
land with ADFA-4739, so on `main` the module could not be imported at all. They are used only
inside its `main()`, converting the Writerside sources, so that is where the imports now sit.

`store_page` is the one piece the pipeline does not provide: which of the two writers to call.
`write_item` UPDATEs a row that exists, `insert_chunked_content` INSERTs one that does not, and
`write_item`'s UPDATE on an absent path succeeds while writing nothing -- so guessing loses the
page silently. It also rewrites only the bytes, correctly for what it was built for (recompressing
a row whose type and template are not changing) but not here, where a scraped page is `templateId`
0 and has to come out templated. So the columns that say how to serve a row are set afterwards, on
the base row and on any continuation row.

## Running it in CI

Two workflows, mirroring the Kotlin and Java pairs:

| | |
|---|---|
| `.github/workflows/build-android-docs.yaml` | reads and writes the real database on Google Drive |
| `.github/workflows/build-android-docs-local.yaml` | the same five steps against a path on disk |
| `run-build-android-docs-with-act.sh` | drives the local one under [act](https://github.com/nektos/act) |

The steps between the two are identical -- convert, load, verify, re-mint,
verify the re-mint -- and only the ends differ: Drive download/upload behind
Workload Identity Federation, or `cp` from and to `db_path`. The local workflow
exists because WIF validates the OIDC token's issuer against GitHub's own token
endpoint, so act can never get past the Drive workflow's auth step no matter
what secrets it is given.

Unlike the Kotlin workflow (which clones kotlin-web-site) and the Java one
(which unpacks a JDK's `lib/src.zip`), there is no external source to fetch: the
12,106 derived HTML pages are committed in this repository, so a run is
reproducible from a commit alone and there is no ref to pin. The 9 GB raw scrape
they came from stays on Drive and is not needed.

Two inputs are worth knowing about. `only` converts a substring of the corpus
and turns a twelve-minute run into seconds, which is how to smoke-test a change;
it leaves the rest of the rows as they were, so it needs `verify_complete=false`
beside it. `skip_remint` drops the two slowest steps, which is right for a
structural check and for a re-run against a database already minted for this
corpus -- re-minting is not cumulative, and a second pass over an already-minted
database measured -0.7%.

## The renderer

`renderer/` is a small Gradle project: Pebble templates, and just enough Java to walk the tree and
pick a template per page.

| | |
|---|---|
| `templates/_macros.peb` | summary tables, inherited groups, member entries, version notes |
| `templates/class.peb` | a type page |
| `templates/package.peb` | a package summary |
| `templates/index.peb` | an index |
| `static/stylesheet.css` | |
| `AndroidDocRenderer.java` | walks the JSON tree, `page` field -> template |
| `HtmlLinks.java` | swaps `.json` for `.html` through a whole page document |
| `TemplateCheck.java` | renders one stored template against one stored page |
| `AndroidDocRendererTest.java` | renders a page of each kind through the real templates |

The `page` field selects the template and the parsed JSON becomes the template context directly, so
a template reads the same field names that appear in the JSON.

**The templates are the ones the database stores**, which constrains how they are written: a
`Templates` row is one self-contained template evaluated against a row's JSON, so there is no
`extends`, no `import`, and no filter beyond `raw` -- the server has nothing to resolve a parent
against and no place to register a filter. Macros are only visible inside the file that defines
them, so a page template and `_macros.peb` are concatenated into one source before compiling; the
database's own `page.peb` keeps its macros at the bottom of the same file for the same reason.

That is also why the `.json` -> `.html` link swap is no longer a filter. It happens once, when a
page document is loaded -- `HtmlLinks.java` here, `rewrite_document` in the loader -- which is the
only way a template with no filters can still emit working links.

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
