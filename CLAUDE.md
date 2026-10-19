# CLAUDE.md

Guidance for Claude (and anyone else) working in this repository.

## What this repository is

App Dev For All builds **Code on the Go**, an Android IDE aimed at users with no or limited
internet access
(code: [appdevforall/CodeOnTheGo](https://github.com/appdevforall/CodeOnTheGo)). To support that,
Java/Kotlin/Android API documentation is bundled into the app as a single SQLite file — the
**documentation database** — rather than fetched from the web.

The documentation database serves two distinct features in the IDE:

1. **Tooltips (Tier 1/2).** When a user selects a keyword/symbol in the code editor, a dialog
   shows short (Tier 1) and detailed (Tier 2) tooltip text if the selection matches an entry in
   the DB. This lookup happens elsewhere in the CodeOnTheGo Android code (not in this repo, and
   not in `WebServer.kt` — see below).
2. **Content pages (Tier 3).** From a tooltip, the user can click through to a full documentation
   page. Those pages (and other static content — HTML, images, PDFs) are served over HTTP by
   **`WebServer.kt`**
   ([CodeOnTheGo/app/src/main/java/com/itsaky/androidide/localWebServer/WebServer.kt](https://github.com/appdevforall/CodeOnTheGo/blob/stage/app/src/main/java/com/itsaky/androidide/localWebServer/WebServer.kt)),
   which runs inside the app and reads directly from the `Content` (and, as of recently,
   `Templates`/`Bookshelf`/`BookCategories`) tables of the same database.

**This repository (`OfflineDocumentationTools`) is the collection of offline tools that build and
edit that database** — it contains no part of the production Android app itself.

> **Alex's standing caveat, worth repeating at the top of every session:** nothing in this
> repository is guaranteed to work against the *current* production database. The schema has moved
> forward (in the app / by hand) faster than the tooling in this repo has been updated. See
> "Schema: current vs. what this repo expects" below — that gap is the most important thing to
> understand before making changes here.

## Schema: current vs. what this repo expects

> ### Schema 2.0.0: every Brotli row uses a shared dictionary
>
> As of 2026-08-20 `~/documentation.db` reports **2.0.0** in `DocumentationDatabaseVersion`
> ("Add CompressionDictionary table", David) — a deliberately *incompatible* major bump. Every
> `brotli` Content row is compressed against the shared 256 KiB raw LZ77 dictionary held in
> `CompressionDictionary` (id 1). Measured on that database: **0 of 24 sampled brotli rows decode
> with plain Brotli; all 24 require `brotli -D`.** A dictionary stream and a plain one are not
> interchangeable, so anything reading or writing `Content` has to go through the dictionary.
>
> No Python Brotli binding exposes a custom dictionary (the `brotli` package has no such parameter;
> `brotlicffi` dropped `BrotliDecoderSetCustomDictionary` in 1.2), so all three writers shell out to
> the **`brotli` CLI** — which therefore has to be installed wherever the pipeline runs. Both
> workflows install it.
>
> | Writer | Dictionary handling |
> |---|---|
> | `ProcessKotlinWebsiteJSON/populate_db.py` | `DictionaryCompressor` + `load_or_create_dictionary`; trains one only if the table is absent/empty, and **never** retrains. |
> | `ProcessKotlinWebsiteJSON/insert_optimized_media.py` | Reads the existing dictionary via `load_dictionary`; never creates one. |
> | `scripts/sync_kotlin_stdlib_docs/sync_kdoc_json_to_db.py` | `DictionaryBrotli` + `load_compression_dictionary`; falls back to plain Brotli only for pre-2.0.0 databases. |
>
> Retraining an existing dictionary would orphan every row already compressed against the old one,
> which is why all three only ever read what is already stored.
> `migrate_content_to_dictionary_brotli.py` recompresses any remaining plain-Brotli rows.
>
> Note also that `image/webp` (id 26) and `video/quicktime` (id 28) now exist in `ContentTypes`, so
> the workflows' `INSERT OR IGNORE ... image/webp` step is a no-op against current copies.

> ### Schema 3.0.0: keyed tooltip tables and compressed templates, in the shipped copy only
>
> `scripts/migrate_tooltip_schema.py` (ADFA-3305) rewrites the tooltip tables and declares **3.0.0**:
> `Tooltips(id, categoryId, tag)` WITHOUT ROWID, keyed on `(categoryId, tag)`;
> `TooltipTexts(id AUTOINCREMENT, summary, detail)`; `TooltipButtons(tooltipId, buttonNumberId,
> descriptionId, uriId)` WITHOUT ROWID, keyed on `(tooltipId, buttonNumberId)`; and each button
> string once in `TooltipButtonDescriptions` and `TooltipButtonUris`. Code On The Go from
> CodeOnTheGo #2163 on reads only these tables, and refuses tooltips from a database declaring less.
>
> The same step stores **every `Templates` row Brotli-compressed** against `CompressionDictionary`,
> with no per-row marker: in 3.0.0 all templates are compressed, and the app always decodes them.
> Every other tool that reads or writes Templates expects them plain: `populate_db.py`,
> `sync_javadoc_json_to_db.py`, `ProcessDocs/AndroidDocs/AndroidDocsToJson/load_android_json_db.py`
> and `verify_android_json_db.py`, and in open PRs `minify_text.py` (#44), the `a/`
> pages-to-templates step (`android_pages_to_templates.py`, #49) and `hm_pages_to_templates.py`
> (#43). On the master the templates stay plain. Some of these run on the copy that ships as well
> (the minify and the `a/` step do), so there every one of them runs before the migration. The order
> on that copy is: minify, row drops, Kotlin trim, `a/` pages-to-templates,
> `migrate_tooltip_schema.py`, PR #48's `remint_dictionary.py --seeds 4` (which recompresses
> templates too in 3.0.0), then `rebuild_database.py --pack Content,TooltipTexts`;
> `hm_pages_to_templates.py` goes after the `i/` page steps it lists, before the migration like
> every other Templates writer. PR #48 has to merge no later than this step's PRs (#45, #50): the
> re-mint and verifier before it handle Content only, so they refuse a database declaring 3.0.0.
>
> `populate_db.py`, `sync_javadoc_json_to_db.py` and `load_android_json_db.py` write plain templates,
> so each refuses a database declaring 3.0.0 or later before writing anything, backup included.
> `load_android_json_db.py` keeps the major it finds, so a late run would otherwise leave a file
> declaring 3.1.0 with plain templates in it, which the app cannot read.
>
> **Every tool in this repo still reads and writes the 2.x shape**: `check-tools` (whose daily
> `docdb-regression-test.yaml` run selects `TooltipButtons.uri` and `Tooltips.detail`),
> `sync_kdoc_json_to_db.py` (step 5 of `build-kotlin-docs.yaml`), `TooltipManager.py` and
> `docdb-studio`. So the step runs on the copy that ships, after all of them, and never on the
> master database they edit; nothing converts 3.0 back. It needs the `brotli` CLI on PATH.
>
> In the 3.0 shape `Tooltips.id` must be unique and name a `TooltipTexts` row: a writer takes the
> id its `TooltipTexts` insert hands out, never one of its own. Nothing enforces it (a `UNIQUE (id)`
> index would cost about the 613 pages the conversion saves), and two rows sharing an id share one
> text, so deleting either key's text deletes the other's.

The schema below is what `~/documentation.db` (Alex's current production copy) actually contains,
as of 2026-08-05 — predating the 2.0.0 bump described above, which additionally adds the
`CompressionDictionary` and `DocumentationDatabaseVersion` tables:

```sql
CREATE TABLE Languages (id INTEGER PRIMARY KEY AUTOINCREMENT, value TEXT NOT NULL UNIQUE);
CREATE TABLE ContentTypes (id INTEGER PRIMARY KEY AUTOINCREMENT, value TEXT NOT NULL UNIQUE, compression TEXT NOT NULL);
CREATE TABLE TooltipCategories (id INTEGER PRIMARY KEY, category TEXT NOT NULL);
CREATE TABLE TooltipButtonNumbers (id INTEGER UNIQUE); -- manually assigned display order
CREATE TABLE Content (
    id INTEGER PRIMARY KEY AUTOINCREMENT, path TEXT NOT NULL, languageID INTEGER NOT NULL,
    content BLOB NOT NULL, contentTypeID INTEGER NOT NULL, templateId INTEGER NOT NULL DEFAULT 0,
    FOREIGN KEY (languageID) REFERENCES Languages(id), FOREIGN KEY (contentTypeID) REFERENCES ContentTypes(id),
    UNIQUE('path')
);
CREATE TABLE Tooltips (
    id INTEGER PRIMARY KEY AUTOINCREMENT, categoryId INTEGER NOT NULL, tag TEXT NOT NULL,
    summary TEXT NOT NULL, detail TEXT NOT NULL, UNIQUE (categoryId, tag),
    FOREIGN KEY(categoryId) REFERENCES TooltipCategories(id)
);
CREATE TABLE TooltipButtons (
    tooltipId INTEGER, buttonNumberId INTEGER, description TEXT, uri TEXT,
    FOREIGN KEY(tooltipId) REFERENCES Tooltips(id), FOREIGN KEY(buttonNumberId) REFERENCES TooltipButtonNumbers(id)
);
CREATE TABLE LastChange (documentationSet TEXT, changeTime TIMESTAMP DEFAULT CURRENT_TIMESTAMP, who TEXT);
CREATE TABLE Templates (id INTEGER PRIMARY KEY AUTOINCREMENT, name TEXT NOT NULL, content BLOB NOT NULL, UNIQUE('name'));
CREATE TABLE BookCategories (id INTEGER PRIMARY KEY AUTOINCREMENT, category STRING, description STRING DEFAULT '', UNIQUE('category'));
CREATE TABLE Bookshelf (contentID INTEGER NOT NULL, title STRING DEFAULT '', description STRING DEFAULT '',
    bookCategoryID INTEGER, FOREIGN KEY (bookCategoryID) REFERENCES BookCategories(id), UNIQUE(title, bookCategoryId));
-- Triggers keep Bookshelf in sync when a .pdf row is added to/removed from Content.
CREATE TABLE PUCC_Students (...), PUCC_Classes (...), PUCC_Sections (...), PUCC_Professors (...),
             PUCC_StudentAssignments (...), PUCC_ProfessorAssignments (...)
-- Unrelated to documentation tooling (confirmed by Alex) — ignore, leave as-is, do not
-- document or maintain further in this repo.
```

**`Templates`, `BookCategories`, and `Bookshelf` are not documentation cruft — `WebServer.kt`
actively depends on them.** Its `/pr/bs` endpoint builds a JSON "bookshelf" payload straight from
`Content` + `Bookshelf` + `BookCategories`, looks up a template named `'bookshelf'` in `Templates`,
and renders it with the Pebble template engine. More generally, any `Content` row with a non-zero
`templateId` gets its stored (decompressed) content run through the matching row in `Templates` as
a Pebble template before being served. This is a real, current feature of the shipped server, not
a placeholder.

**This repo writes `Templates`, but not `Bookshelf` or `BookCategories`.** `populate_db.py`
(`page.peb`, `nav.peb`), `sync_javadoc_json_to_db.py` (`javadoc.peb`) and `load_android_json_db.py`
(the `android-*.peb` templates) write rows there and point the pages they write at them via
`templateId`; `verify_android_json_db.py` reads them; and `migrate_tooltip_schema.py` compresses
every one for 3.0.0 (above). `insert_optimized_media.py` reads and writes `templateId` too.
`Bookshelf` and `BookCategories` come from App Dev For All's plugin system, which writes into the
documentation database - the bookshelf feature from
[appdevforall/bookshelf-plugin](https://github.com/appdevforall/bookshelf-plugin), now archived on
GitHub. Nothing here writes them, except that `Content`'s `AddBook` trigger adds a `Bookshelf` row
for each `.pdf` path inserted (see `migrate_content_to_dictionary_brotli.py`). Concretely, relative
to the schema above:

| Piece | What it thinks the schema is | Consequence |
| --- | --- | --- |
| `scripts/DocumentationDatabase.py` (used by `scripts/ingest.py`, and hence by `.github/workflows/publish-doc-db.yaml`) | `Content` / `Languages` / `ContentTypes` only, plus an optional `ide_tooltip_table`. Its constructor explicitly **raises `ValueError`** if it opens a DB containing any table outside that whitelist. | **This will refuse to open the current production `documentation.db` at all** — it will list `Tooltips`, `TooltipCategories`, `TooltipButtons`, `TooltipButtonNumbers`, `LastChange`, `Templates`, `BookCategories`, `Bookshelf`, and every `PUCC_*` table as "unexpected." This is the single biggest blocker to reusing this script as-is. |
| `docdb-studio/SCHEMA.md` / `AGENTS.md` (states the schema is "locked," no migrations) | `Content` (no `templateId`, no `UNIQUE(path)`), `Tooltips`, `TooltipButtons`, `TooltipCategories`, `TooltipButtonNumbers`, `LastChange` (with a *different* shape: `documentationSet`/`changeTime`/`who` — this part does match current), plus a legacy `ide_tooltip_table`. Missing `templateId`, `Templates`, `BookCategories`, `Bookshelf`, `PUCC_*`. | Closest of the three documented schemas to reality, but still out of date. `docdb_studio.py`'s own "never change the schema" policy is itself now stale, since the live schema has already changed underneath it. |
| `check-tools/README.md`'s embedded schema (and by extension the mental model behind `check-tools/db_health_checker.py`) | `Content` (no `templateId`, no `UNIQUE(path)`), `Tooltips`, `TooltipButtons`, `TooltipCategories`, `TooltipButtonNumbers`, and a *third* variant of `LastChange` (`now`/`who`). No `Templates`/`Bookshelf`/`BookCategories`/`PUCC_*`. | The health checker's required-table check still passes (it only checks that its known tables exist, not that no others do). Since `Bookshelf`/`BookCategories` are written outside this repo (see above) and check-tools reads no `Templates`, this is not being treated as something to fix right now. |

There also appear to be **two unrelated tooltip storage formats** in this repo's history, and it's
worth being deliberate about which one is current:

- The **normalized** format (`Tooltips` + `TooltipCategories` + `TooltipButtons` +
  `TooltipButtonNumbers`) — this is what's in the live schema above, what `docdb-studio` edits,
  what `check-tools/db_health_checker.py` validates, and what `scripts/TooltipManager.py`
  dumps/rebuilds via CSV.
- A **legacy flat** format, a single `ide_tooltip_table(tooltipCategory, tooltipTag,
  tooltipSummary, tooltipDetail, tooltipButtons)` table (button data packed as a JSON string in
  one column) — written by `scripts/tooltips.py` (`TooltipDatabase`, driven by
  `scripts/import_tooltips.py` from `SourceDocs/Tooltips/tooltips.xlsx`) and by
  `scripts/load_android_data.py` (fed by pickle files that `scripts/android_tooltips.py` /
  `scripts/java_tooltips.py` scrape from Android/Java HTML doc trees). **`ide_tooltip_table` does
  not exist in the current production schema at all.**

**`ide_tooltip_table` is officially dead (confirmed by Alex).** That means the entire chain that
targets it — `scripts/tooltips.py`, `scripts/import_tooltips.py`, `scripts/android_tooltips.py`,
`scripts/java_tooltips.py`, `scripts/android_html_page.py`, and `scripts/load_android_data.py` — is
**deprecated legacy code**. It's left in the repo for reference/history, but none of it should be
extended or relied on, and none of it writes to a table the shipped app or `docdb-studio` actually
uses. Any future Android/Java tooltip work should target the normalized `Tooltips` /
`TooltipCategories` / `TooltipButtons` / `TooltipButtonNumbers` tables instead (the same ones
`docdb-studio` and `scripts/TooltipManager.py` already use for Kotlin tooltips), in their 2.x
shape: every tool here reads and writes that, and only the shipped copy holds the 3.0.0 shape, after
`migrate_tooltip_schema.py` (see "Schema 3.0.0" above).

## Repository tour

- **`docdb-studio/`** — a Flet (Flutter-for-Python) desktop GUI for browsing/editing `Tooltips` /
  `TooltipCategories` / `TooltipButtons` and importing `Content`. Has its own `CLAUDE.md`,
  `AGENTS.md`, `SCHEMA.md`, and a real pytest suite. Actively maintained (most recent commits in
  the repo touch this tool), but per the table above, its documented schema is behind the live one.
  That's an accepted state, not an active problem: schema evolution happens outside
  `docdb-studio` (and outside this repo, e.g. via plugins — see below), and `docdb-studio` is
  expected to catch up after the fact rather than lead. Its `AGENTS.md`/`SCHEMA.md` "never migrate
  the schema" language should be read as "don't migrate it from in here," not as a claim that the
  schema never changes.
- **`check-tools/`** — `db_health_checker.py` (schema/integrity/referential checks against the
  *old* normalized schema) plus `download_database.py`, a working Google Drive downloader
  authenticated via GCP Workload Identity Federation (no long-lived keys). Wired into
  `.github/workflows/docdb-regression-test.yaml`, which runs it daily against the production DB on
  Drive.
- **`scripts/`** — the original CLI toolbox. Live/current: `DocumentationDatabase.py` (Content
  ingestion — see whitelist issue above), `ingest.py` (thin CLI over it, used by
  `publish-doc-db.yaml`), `TooltipManager.py` (CSV ⇄ normalized-Tooltips round-trip),
  `create_empty_database.py`, `list_database_documents.py`, `migrate_tooltip_schema.py` (ADFA-3305:
  converts to documentation schema 3.0.0, which Code On The Go reads from 3.0.0 on: the WITHOUT ROWID,
  lookup-table tooltip shape, and every template Brotli-compressed against the dictionary; run it on the
  copy that ships, after every step that writes tooltips or templates, since they all write the old
  shape and plain templates, never on the master they edit; re-mint afterwards, then pack
  `Content,TooltipTexts`, not `Tooltips`), `svg_images_to_webp.py` (ADFA-3305: re-encodes the raster
  images SVG rows embed as data: URIs as WebP, image by image, by a quality rule; run on the shipped
  copy before the final dictionary re-mint; `compare_svg_renders.py` renders each changed SVG before and
  after in headless Chrome, through `<img>` and `<object>`, as its visual check).
  **Deprecated/dead** (target the
  removed `ide_tooltip_table` — see above, kept for reference only): `tooltips.py`,
  `import_tooltips.py`, `android_tooltips.py`, `java_tooltips.py`, `android_html_page.py`,
  `load_android_data.py`.
- **`scripts/myServer.py`** — a minimal Python `http.server` reference implementation that predates
  `WebServer.kt`. It queries a differently-cased `Documentation.db`, doesn't implement Brotli
  decompression (there's a literal `TODO: Replace this function with Brotli decompression`), and
  knows nothing about compression-aware content types, templates, or fragmentation. **This is not
  what ships in the app** — treat it as historical/reference only, not as documentation of current
  server behavior. `WebServer.kt` is the real thing.
- **`Dokka-plugin-kdoc2json/`** — the Dokka `JsonRenderer`/`ModelMapper`/`LinkPostProcessor` plugin,
  its test suite, and the `kotlin-stdlib-docs` build scripts, merged to `main` via `fix/ADFA-4514`
  (`4c6b8aef`). Consumed by `Dokka-plugin-kdoc2json/scripts/kotlin/build-stdlib-json-docs.sh` and
  `scripts/sync_kotlin_stdlib_docs/sync_kdoc_json_to_db.py` (ADFA-4739) to generate and load
  kotlin-stdlib/-reflect/-test JSON docs.
- **`ProcessDocs/`** — HTML-processing pipelines that predate the "build docs as JSON" goal:
  `ProcessKotlinDocs/` (turns Kotlin's HTML doc export into a self-contained HTML set + table of
  contents, used by `.github/workflows/automate-kotlin.yaml`), `ProcessAndroidDevSite/`, `AndroidDocs/`
  (holds `android-tooltips.pkl`, the pickle consumed by the now-deprecated `load_android_data.py`),
  `ProcessPDFs/`.
- **`SourceDocs/`** — raw inputs: `KotlinDocs/html`, `JavaDocs/html` + `java_keywords.html`,
  `Tooltips/tooltips.xlsx`, `KotlinDocs/kotlin-spec.pdf`.
- **`DocumentationAnalysis/`, `DocAnalysis/`, `png_optimization/`, `androidxtooltips/`** — Jupyter
  notebooks and one-off scripts for doc-set size analysis, image/PNG compression experiments, and a
  one-time AndroidX tooltip import (ADFA-1419). Not part of the critical build path.
- **`.github/workflows/`** — `automate-kotlin.yaml` (tag-triggered, builds the
  Kotlin HTML doc bundle as a GitHub release asset), `publish-doc-db.yaml` (tag-triggered, runs the
  `scripts/ingest.py` pipeline and releases the resulting `.sqlite`), `docdb-regression-test.yaml`
  (daily cron, downloads the production DB from Google Drive via WIF and runs
  `check-tools/main.py` against it), `build-kotlin-docs.yaml` and its local-filesystem counterpart
  `build-kotlin-docs-local.yaml` (manual dispatch, the five-step Kotlin docs pipeline — these two
  are the only ones with Slack notifications), and `python-tests.yaml` (push/PR, runs the pytest
  suites).

## Decisions log

Settled with Alex on 2026-08-05, folded into the sections above; recorded here so the reasoning
isn't lost:

- `ide_tooltip_table` and everything that targets it are dead. Treat as deprecated, not as a gap.
- `Templates`/`Bookshelf`/`BookCategories` are populated by App Dev For All's plugin system
  (e.g. [bookshelf-plugin](https://github.com/appdevforall/bookshelf-plugin)), not by this repo.
  Not a gap to fill here.
- `PUCC_*` tables are unrelated to documentation tooling. Ignore; leave as-is.
- `docdb-studio`'s schema is expected to lag the live schema and catch up after the fact; that's
  fine, no urgent update needed.
- `check-tools/db_health_checker.py` is not being extended with `Templates`/`Bookshelf` checks
  right now — deliberately out of scope for the moment.

The one piece of this document that still describes an *active* problem rather than a settled
scope boundary is `scripts/DocumentationDatabase.py`'s hard failure on unrecognized tables (see the
table above) — that will need to be addressed before `scripts/ingest.py` /
`publish-doc-db.yaml` can run against a current-schema database.
