# Documentation_Tools

Scripts for building the `documentation.db` SQLite database that Code On The Go's
local web server (`WebServer.kt`, port 6174) reads from — help docs, tooltips,
and the pdf.js viewer all live in its `Content` table.

## Updating pdf.js (ADFA-4721, and future version bumps)

Code On The Go and KnowledgeToGo both vendor Mozilla's pdf.js as a prebuilt release
dist, not the full source repo. KnowledgeToGo's `static/pdfjs/fetch-pdfjs.sh` is the
reference for which version to track — match its pinned version unless there's a
reason to diverge.

### 1. Fetch and trim the release dist

```
curl -fSL "https://github.com/mozilla/pdf.js/releases/download/v<VERSION>/pdfjs-<VERSION>-dist.zip" -o pdfjs.zip
unzip pdfjs.zip -d dist
cd dist
find . -name "*.map" -delete                                    # source maps, ~50% of the zip
find web/locale -mindepth 1 -maxdepth 1 -type d ! -name en-US -exec rm -rf {} +   # non-English locales
rm -f web/compressed.tracemonkey-pldi-09.pdf                    # demo PDF
rm -f web/debugger.mjs web/debugger.css                         # dev-only tool
```

Keep everything else (`web/cmaps`, `web/standard_fonts`, `web/wasm`, `web/images`,
`web/iccs`) — pdf.js needs those at runtime for CJK text, embedded-font fallback,
and image codecs (JBIG2/OpenJPEG/QCMS).

### 2. Load it into documentation.db

Run from *inside* the trimmed `dist/` directory (the loader walks `.`):

```
python3 /home/david/Documentation_Tools/pdfjs_loader.py
```

Before running, **delete the previous version's rows** — the loader only does
plain `INSERT`s against a `Content` table with `UNIQUE(path)`, so a stale row
from the old version aborts the whole transaction on the first collision:

```sql
DELETE FROM Content WHERE path LIKE 'p/%';
```

(`p/` is `PDFJS_PREFIX` in the loader — every pdf.js file is stored under that
prefix, and `WebServer.kt` matches request paths against it directly.)

### Gotchas hit going from 4.8.69 to 6.1.200

- **New file extensions need a `FILETYPE_MAP` entry.** v6 introduced
  `web/iccs/*.icc` (color profile) and `web/cmaps/*.bcmap` (CJK CMaps) that
  4.8.69 didn't have. Missing entries raise `KeyError` in `addFileToDatabase`.
  Check with a quick `os.walk` diff against `FILETYPE_MAP`'s keys before running
  for real — don't just fix extensions one crash at a time.
- **Every mime type in `FILETYPE_MAP` must already have a row in `ContentTypes`**
  (`getCompressionByMimeType` builds its lookup from that table, not from the
  loader script) — a mapping to a mime type with no row raises `KeyError` in
  `addFileToDatabase`, not in the mapping step. `application/vnd-iccprofile` and
  `application/octet-stream` were the two needed for v6's new extensions.
- **A run interrupted mid-compression leaves a stray `<file>.br`** next to its
  source file (`compressWithBrotli` only deletes it after a successful read).
  `os.walk` picks that up on the next run and crashes with `KeyError: '.br'`
  since `.br` isn't a real content type. `find dist -name "*.br"` and delete
  before retrying.
- **`addTestContentToDatabase()` starts with an unconditional `return`** — it's
  intentionally disabled (its hardcoded `TEST_PDF_LOCAL_PATH` doesn't exist
  outside a full pdf.js source checkout anyway). Leave it disabled.
- `DATABASE_PATHNAME` is hardcoded to `/home/david/documentation.db` — this
  script only ever writes to that one file, not whatever `documentation.db`
  ends up bundled with a release.

### Verifying on-device without a rebuild

`WebServer.kt` checks `/sdcard/Download/documentation.db` on every request and
hot-swaps to it if its mtime is newer than the currently-loaded db — no
reinstall needed:

```
adb push /home/david/documentation.db /sdcard/Download/documentation.db
```

Then load `http://localhost:6174/p/web/viewer.html?file=<path-to-a-pdf-in-Content>`
in the app (or via `chrome://inspect` / raw CDP against the WebView's
`webview_devtools_remote_<pid>` socket, forwarded with `adb forward`).

**Take a real `adb exec-out screencap`, not a CDP `Page.captureScreenshot`** —
the devtools screenshot doesn't reliably capture GPU-composited canvas content
in this WebView and will show a blank page even when the PDF rendered
correctly (confirmed by reading the canvas's actual pixel data via
`getImageData`, which did show the expected content). It's a devtools/screencap
limitation, not a pdf.js problem — don't waste time debugging a "blank" PDF
without checking a real screencap first.

### Publishing

This script only writes to a local file. Getting a new `documentation.db` into
an actual build/release is a separate step outside this repo — this file
doesn't cover it.
