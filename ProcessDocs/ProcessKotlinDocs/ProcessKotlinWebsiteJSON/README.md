# Process Kotlin Website JSON

Converts a `kotlin-web-site/docs` checkout (JetBrains Writerside-flavored
Markdown) into the JSON block schema this project's templating engine
renders.

This PR (ADFA-5039) covers only [`md_to_json.py`](md_to_json.py) — the
conversion step itself. Building the sidebar nav from `kr.tree`
(`build_nav.py`), QA-ing the source tree for broken links/images
(`find_missing_assets.py`), and loading any of this into `documentation.db`
(`populate_db.py`, `insert_optimized_media.py`) are a separate ticket
(ADFA-4739) and land in a later PR.

## Requirements

- Python 3.10+
- `markdown-it-py` (now in the repo's root `requirements.txt`)

## Usage

```bash
python3 md_to_json.py <docs-root> <output-dir> <config> [--topics-subdir topics] [--images-subdir images] [--allow-failures]
```

- `<docs-root>` — a checkout of `kotlin-web-site/docs` (contains `v.list`, `topics/`, `images/`).
- `<config>` — a JSON file with theming colors, e.g. [`config.json`](config.json):
  ```json
  {"broken-ext-link-color": "#cc0000", "menu-no-link-color": "#999999"}
  ```

`<output-dir>` ends up containing:
- `topics/**/*.json` — one page per source `.md` file (schema below)
- `theme.json` — the two theming colors, carried through from `<config>`
- `images/` — copied straight from `<docs-root>/images/`

### Page JSON schema

```json
{
  "id": "enum-classes",
  "sourceFile": "topics/enum-classes.md",
  "title": "Enum classes",
  "blocks": [ { "type": "heading", "level": 2, "id": "...", "html": "..." }, "..." ]
}
```

Block types: `heading`, `paragraph`, `code`, `blockquote`, `list`, `table`,
`image`, `hr`, `tabs`, `note`/`tip`/`warning`, `html` (raw passthrough). See
the module docstring in [`md_to_json.py`](md_to_json.py) for full shapes and
known limitations (nested tabs, `<include>` resolution, variable
substitution).

A heading's `id` is `slugify()`'d from its text, unless the source line has
an explicit `{id="..."}` (which overrides it directly). Cross-page links
that carry a source `#anchor` are passed through verbatim rather than
re-slugified, so a link and its target agree as long as both derive their id
the same way; a hand-written `#anchor` that doesn't match either path (e.g.
because Writerside's own anchor algorithm diverges from `slugify()` on
headings with inline code or punctuation) will resolve to the right page but
land on no anchor. Not currently detected - worth spot-checking if a page's
in-page anchors stop scrolling to the right place.

## Trying it out

[`review_build_json.sh`](review_build_json.sh) is a throwaway helper for
reviewers — it clones `kotlin-web-site` and runs `md_to_json.py` against it
via `uv run` so you can look at real output without any other setup. It's
not part of the actual pipeline (that's ADFA-4739):

```bash
./review_build_json.sh
```
