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

> ### ⚠ BLOCKER for this branch: `~/documentation.db` is now schema 2.0.0 (dictionary Brotli)
>
> As of 2026-08-20 the production copy reports **2.0.0** in `DocumentationDatabaseVersion`
> ("Add CompressionDictionary table", David) — a deliberately *incompatible* major bump. Every
> `brotli` Content row is now compressed against the shared 256 KiB raw LZ77 dictionary held in
> `CompressionDictionary` (id 1). Measured on that database: **0 of 24 sampled brotli rows decode
> with plain Brotli; all 24 require `brotli -D`.** The two encodings are not interchangeable in
> either direction.
>
> This branch (`fix/ADFA-4739`) forked from `fix/ADFA-4737` **before** that work landed there via
> PRs #26 and #27, so two of its three database writers are still plain-Brotli and are broken
> against the current database:
>
> | File | Symptom against a 2.0.0 database |
> |---|---|
> | `ProcessKotlinWebsiteJSON/insert_optimized_media.py` | **Crashes.** `rewrite_pages` / `collect_referenced_media` call `brotli.decompress` on dictionary-compressed page rows. |
> | `ProcessKotlinWebsiteJSON/populate_db.py` | **Silent corruption.** Writes plain-Brotli rows the server cannot decode. |
> | `scripts/sync_kotlin_stdlib_docs/sync_kdoc_json_to_db.py` | Fixed — reads `CompressionDictionary` and compresses against it, falling back to plain Brotli only for pre-2.0.0 databases. |
>
> The fix for the first two already exists on `fix/ADFA-4737` (a `DictionaryCompressor` shelling out
> to the `brotli` CLI's `-D` flag, plus `load_or_create_dictionary` and
> `migrate_content_to_dictionary_brotli.py`). It is deliberately **not** duplicated here: reconciling
> this branch with `fix/ADFA-4737` is the right way to pick it up, and porting it by hand would
> guarantee a conflict with already-merged work. Until that reconciliation happens, do not run
> `populate_db.py` or `insert_optimized_media.py` against a 2.0.0 database.
>
> Note also that `image/webp` (id 26) and `video/quicktime` (id 28) now exist in `ContentTypes`, so
> the workflows' `INSERT OR IGNORE ... image/webp` step is a no-op against current copies.

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

**Nothing that currently builds or writes to the database in this repository knows about any of
that — and that's expected.** `Templates`/`Bookshelf`/`BookCategories` are populated by a separate
plugin system, not by anything in this repo: App Dev For All supports plugins that write into the
documentation database, including the bookshelf feature specifically —
[appdevforall/bookshelf-plugin](https://github.com/appdevforall/bookshelf-plugin). So the absence
of any `Templates`/`Bookshelf`/`BookCategories` handling here is not a gap to fill; it's out of
scope for this repo. (A repo-wide search for `Templates`, `Bookshelf`, `BookCategories`, or `PUCC`
turns up zero matches outside `WebServer.kt` itself, which is consistent with that division of
responsibility. `templateId` itself is a different story - `populate_db.py` and
`insert_optimized_media.py` both read/write it directly, since it's a plain column on `Content`
they populate; it's only the `Templates` table and the plugin system that reference it that stay
out of scope.) Concretely, relative to the schema above:

| Piece | What it thinks the schema is | Consequence |
| --- | --- | --- |
| `scripts/DocumentationDatabase.py` (used by `scripts/ingest.py`, and hence by `.github/workflows/publish-doc-db.yaml`) | `Content` / `Languages` / `ContentTypes` only, plus an optional `ide_tooltip_table`. Its constructor explicitly **raises `ValueError`** if it opens a DB containing any table outside that whitelist. | **This will refuse to open the current production `documentation.db` at all** — it will list `Tooltips`, `TooltipCategories`, `TooltipButtons`, `TooltipButtonNumbers`, `LastChange`, `Templates`, `BookCategories`, `Bookshelf`, and every `PUCC_*` table as "unexpected." This is the single biggest blocker to reusing this script as-is. |
| `docdb-studio/SCHEMA.md` / `AGENTS.md` (states the schema is "locked," no migrations) | `Content` (no `templateId`, no `UNIQUE(path)`), `Tooltips`, `TooltipButtons`, `TooltipCategories`, `TooltipButtonNumbers`, `LastChange` (with a *different* shape: `documentationSet`/`changeTime`/`who` — this part does match current), plus a legacy `ide_tooltip_table`. Missing `templateId`, `Templates`, `BookCategories`, `Bookshelf`, `PUCC_*`. | Closest of the three documented schemas to reality, but still out of date. `docdb_studio.py`'s own "never change the schema" policy is itself now stale, since the live schema has already changed underneath it. |
| `check-tools/README.md`'s embedded schema (and by extension the mental model behind `check-tools/db_health_checker.py`) | `Content` (no `templateId`, no `UNIQUE(path)`), `Tooltips`, `TooltipButtons`, `TooltipCategories`, `TooltipButtonNumbers`, and a *third* variant of `LastChange` (`now`/`who`). No `Templates`/`Bookshelf`/`BookCategories`/`PUCC_*`. | The health checker's required-table check still passes (it only checks that its known tables exist, not that no others do). Since `Templates`/`Bookshelf`/`BookCategories` are out of scope for this repo (see above), this is not being treated as something to fix right now. |

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
`docdb-studio` and `scripts/TooltipManager.py` already use for Kotlin tooltips).

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
  `create_empty_database.py`, `list_database_documents.py`. **Deprecated/dead** (target the
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
  (`4c6b8aef`). Consumed by `scripts/kotlin/build-stdlib-json-docs.sh` and
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
- **`.github/workflows/`** — three workflows: `automate-kotlin.yaml` (tag-triggered, builds the
  Kotlin HTML doc bundle as a GitHub release asset), `publish-doc-db.yaml` (tag-triggered, runs the
  `scripts/ingest.py` pipeline and releases the resulting `.sqlite`), `docdb-regression-test.yaml`
  (daily cron, downloads the production DB from Google Drive via WIF and runs
  `check-tools/main.py` against it). None of these have any Slack integration yet.

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
