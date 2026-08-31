# Process Kotlin Website JSON

Converts a `kotlin-web-site/docs` checkout (JetBrains Writerside-flavored
Markdown) into the JSON block schema this project's templating engine
renders, then (optionally) builds the sidebar nav and loads everything
straight into a `documentation.db`-schema SQLite database.

## Scripts

| Script | Purpose |
|---|---|
| [`md_to_json.py`](md_to_json.py) | Converts every `topics/**/*.md` page into one JSON file. Writes `theme.json` and copies `images/` into the output directory. See "Page JSON schema" below. |
| [`build_nav.py`](build_nav.py) | Builds `nav.json`/`nav.html` sidebar navigation from `kr.tree`, resolving each `<toc-element topic="...">` against `md_to_json.py`'s output. |
| [`find_missing_assets.py`](find_missing_assets.py) | QA pass: reports cross-page links, images, and `<include>` targets in the source tree that don't resolve to anything. Reuses `md_to_json.py`'s own resolution logic, so it flags exactly what would end up broken on the rendered site. |
| [`populate_db.py`](populate_db.py) | The database path: converts the docs tree the same way `md_to_json.py` does, builds nav the same way `build_nav.py` does, and inserts pages + nav + images + CSS/JS directly into `documentation.db` (replacing everything under `k/html/` and `assets/`). Supports pruning whole `kr.tree` subtrees via `--blacklisted-element-titles`. |
| [`optimize_media.py`](optimize_media.py) | Standalone media optimizer: downscales/recompresses a directory of images (pngquant, Pillow, Scour/cairosvg for SVG) into a mirrored output directory. |
| [`insert_optimized_media.py`](insert_optimized_media.py) | Runs `optimize_media.py`'s pipeline over a directory of raw media, then replaces the corresponding `k/html/images/*` rows in an existing database, rewriting any page that referenced a renamed file and deleting anything left unreferenced. |

## Requirements

- Python 3.10+ and [`uv`](https://docs.astral.sh/uv/getting-started/installation/) — every command below is run as `uv run --with-requirements <repo-root>/requirements.txt <script>.py ...`, which installs `markdown-it-py`, `Pillow`, `scour`, `cairosvg`, `brotli`, and the rest of the repo's dependencies into an ephemeral environment; there's no separate `pip install` step.
- `pngquant` on `PATH` (e.g. `apt install pngquant`) — required by `optimize_media.py`/`insert_optimized_media.py`. `populate_db.py` does *not* need it: it inserts the Writerside export's images as-is, since `insert_optimized_media.py` (step 3) re-optimizes and replaces every one of those rows straight afterwards.

`populate_db.py` also expects, relative to its own location, and already
included in this directory:

- `templates/page.peb`, `templates/nav.peb` — Pebble templates upserted into the `Templates` table.
- `assets/docs.css`, `assets/tabs.js`, `assets/sidebar.js` — static assets inserted at `assets/<name>`.

## Inputs you need before starting

- A checkout of `kotlin-web-site/docs` (the `<docs-root>` argument below) — contains `topics/`, `images/`, `v.list`, and `kr.tree`.
- A config JSON with theming colors, e.g. [`config.json`](config.json):
  ```json
  {"broken-ext-link-color": "#cc0000", "menu-no-link-color": "#999999"}
  ```
- Writerside's own image export zip (e.g. `webHelpImages.zip`, found next to `kr.tree`) if you're using `populate_db.py`.

## Page JSON schema

`md_to_json.py`'s output (one file per source `.md` page):

```json
{
  "id": "enum-classes",
  "sourceFile": "topics/enum-classes.md",
  "title": "Enum classes",
  "blocks": [ { "type": "heading", "level": 2, "id": "...", "html": "..." }, "..." ]
}
```

Block types: `heading`, `paragraph`, `code`, `blockquote`, `list`, `table`,
`hr`, `tabs`, `note`/`tip`/`warning`, `html` (raw passthrough). See the
module docstring in [`md_to_json.py`](md_to_json.py) for full shapes and
known limitations (nested tabs, `<include>` resolution, variable
substitution). There is no standalone `image` block type - an image is
always inline content inside whatever block contains it (typically
`paragraph`), rendered straight into that block's own `html` string.

A heading's `id` is `slugify()`'d from its text, unless the source line has
an explicit `{id="..."}` (which overrides it directly). Cross-page links
that carry a source `#anchor` are passed through verbatim rather than
re-slugified, so a link and its target agree as long as both derive their id
the same way; a hand-written `#anchor` that doesn't match either path (e.g.
because Writerside's own anchor algorithm diverges from `slugify()` on
headings with inline code or punctuation) will resolve to the right page but
land on no anchor. Not currently detected - worth spot-checking if a page's
in-page anchors stop scrolling to the right place.

## Workflow: generate JSON + nav for a static/templated preview

Use this to produce standalone JSON pages and nav data (not the database)
for local inspection or a different renderer.

```bash
UV_RUN="uv run --with-requirements ../../../requirements.txt"

# 1. Convert every topic .md into JSON, one file per page
$UV_RUN md_to_json.py <docs-root> <output-dir> config.json

# 2. Build the sidebar nav from kr.tree against that JSON output
$UV_RUN build_nav.py <docs-root> <output-dir> <output-dir>

# 3. (optional) Check for broken links/images/includes in the source tree
$UV_RUN find_missing_assets.py <docs-root> missing-assets-report.md
```

`<output-dir>` ends up containing everything `md_to_json.py` writes, plus:
- `nav.json` / `nav.html` — sidebar tree and a pre-rendered static copy

## Workflow: generate + insert directly into the documentation database

This is the path that actually populates `documentation.db`. It performs
the same conversion as `md_to_json.py`/`build_nav.py` internally — you don't
run those scripts first.

```bash
uv run --with-requirements ../../../requirements.txt populate_db.py <docs-root> config.json <webHelpImages.zip> [db-path]
```

- `db-path` defaults to `documentation.db` in the current directory, and must already exist with the expected schema (`Languages`, `ContentTypes`, `Templates` tables populated).
- A timestamped backup (`<db-path>.backup-<timestamp>`) is written before any changes, via SQLite's `VACUUM INTO`. It is taken after conversion — the last step that can still refuse to proceed — so a run that bails (e.g. on a conversion failure, without `--allow-conversion-failures`) doesn't leave a full-size copy of the database behind for nothing.
- Everything under `k/html/` and `assets/` is deleted and re-inserted in a single transaction (rolled back on error), then the database is `VACUUM`ed.

### Pruning documentation you don't want (ADFA-4737)

To leave a whole `kr.tree` subtree out of the database entirely — nav
entry, converted pages, and all — pass `--blacklisted-element-titles` with
the full `toc-title` path from a top-level element down to the one you want
to drop. Levels are joined with `\/` (backslash-slash), not a bare `/`,
since a bare `/` commonly appears inside a real title. The example below is
illustrative only — open `<docs-root>/kr.tree` and copy the actual
`toc-title` chain for whatever section you're dropping (e.g. Kotlin/Wasm):

```bash
uv run --with-requirements ../../../requirements.txt populate_db.py \
  <docs-root> config.json <webHelpImages.zip> documentation.db \
  --blacklisted-element-titles \
    "<Top-Level Title>\/<Nested Title>"
```

Any other page's in-content link to a pruned topic renders as a styled
"broken" link (via `broken-ext-link-color`) rather than a dead link with no
indication anything changed. Run with `--blacklisted-element-titles` first
against a scratch copy of the database and check the warnings on stderr for
any path that didn't match — that usually means the toc-title or ancestor
chain was copied wrong.

## Workflow: optimizing and inserting media

Two options, depending on whether the database already has pages loaded:

**Standalone optimization only** (no database involved):

```bash
uv run --with-requirements ../../../requirements.txt optimize_media.py <input-dir> <output-dir> [--max-width 500] [--webp] [...]
```

**Optimize and update an existing database's images in place:**

```bash
uv run --with-requirements ../../../requirements.txt insert_optimized_media.py <media-dir> <db-path> [work-dir] [options]
```

This re-runs `optimize_media.py`'s pipeline, backs up the database first,
replaces each `k/html/images/<name>` row with the optimized bytes, rewrites
any page/nav reference to a file that got renamed during optimization (e.g.
`--webp` conversion or SVG rasterization), and deletes any image no page
references anymore. Both scripts share the same tuning flags
(`--max-width`, `--jpeg-quality`, `--webp`, `--webp-quality`,
`--pngquant-speed`, `--svg-precision`, `--svg-rasterize-threshold`,
`--verbose`, `--log-file`), settable via `--config <file>` instead of the
command line — see either script's module docstring for the full option
reference.

## Recommended order for a full refresh

1. `find_missing_assets.py` against the new `<docs-root>` — fix anything broken in the source before converting it.
2. `populate_db.py`, with `--blacklisted-element-titles` for anything you don't want documented (e.g. Kotlin/Wasm per ADFA-4737).
3. `insert_optimized_media.py` against the raw media directory, if you want optimized (resized/compressed) images rather than Writerside's own export as-is.

## Trying it out

[`review_build_json.sh`](review_build_json.sh) is a throwaway helper for
reviewers — it clones `kotlin-web-site` and runs `md_to_json.py` against it
via `uv run` so you can look at real output without any other setup. It's
not part of the actual pipeline:

```bash
./review_build_json.sh
```
