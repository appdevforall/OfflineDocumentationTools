# Rebuilding every documentation set into a new database (ADFA-4065)

`.github/workflows/build-all-docs.yaml` regenerates all four JSON documentation sets the database
serves through Pebble templates, in one run, and saves the result as a **new** database:

| Set | Source | Generator → loader | Templates installed | Paths |
| --- | --- | --- | --- | --- |
| Kotlin website | JetBrains/kotlin-web-site + Writerside `webHelpImages.zip` | `populate_db.py`, `insert_optimized_media.py` | `page.peb`, `nav.peb` | `k/html/`, `assets/` |
| kotlin-stdlib/-reflect/-test | JetBrains/kotlin + published artifacts | `build-stdlib-json-docs.sh` → `sync_kdoc_json_to_db.py` | (keeps the existing `layout.pebble`) | `k/kotlin-*` |
| Java | the runner JDK's `lib/src.zip` | `build-jdk-json-docs.sh` → `flatten_templates.py` → `sync_javadoc_json_to_db.py` | `javadoc.peb` | `j/html/api/` |
| Android/AndroidX | the scrape committed under `ProcessDocs/AndroidDocs` | `android_docs_to_json.py` → `load_android_json_db.py` | `android-*.peb` + stylesheet | `a/`, `assets/` |

followed by `remint_dictionary.py` + `verify_remint_dictionary.py` (re-train the shared Brotli
dictionary over the finished corpus) and `verify_all_docs_db.py` (this directory). Each set's steps
are the single-set workflow's steps verbatim — `build-kotlin-docs.yaml`, `build-java-docs.yaml`,
`build-android-docs.yaml` — so their notes on inputs, memory and timing apply unchanged.

The source database is only ever read. The Drive workflow saves to `destination_folder_id` (a new
timestamped file) or `destination_file_id` (which may not be the source); the local workflow saves
to `output_db_path` (which may not be `db_path`). Either way the new database is also kept as a run
artifact / in `output_dir`, dry run or not.

## Running it locally

`build-all-docs-local.yaml` is the same workflow against paths on disk, and
`run-build-all-docs-with-act.sh` runs it under [act](https://github.com/nektos/act) with the input
database and image export mounted read-only:

```bash
# Everything, writing <output-dir>/documentation.db (needs a ~16 GB container VM for the JDK pass)
./run-build-all-docs-with-act.sh --db-path ~/docs/documentation.db \
    --images-zip-path ~/docs/webHelpImages.zip --dokka-worker-heap 8g --live

# Every step, quickly, on a small VM
./run-build-all-docs-with-act.sh --db-path ~/docs/documentation.db \
    --images-zip-path ~/docs/webHelpImages.zip \
    --java-modules java.sql,java.transaction.xa --no-verify-java-parity --android-only android/app/
```

`--skip-kotlin-website`, `--skip-kotlin-stdlib`, `--skip-java`, `--skip-android` leave a set as the
source had it; `--skip-remint` leaves the dictionary alone (structural checks only).

## Why the Java rows are not written with `--plain-brotli` here

`build-java-docs.yaml` writes plain-Brotli Java rows. The current CodeOnTheGo reader
(`DocumentationCompression.kt`, ADFA-5240) attaches the shared dictionary to *every* brotli row of a
2.x database and has no plain retry, and decoding a plain row with a dictionary attached fails for
nearly all real rows (measured: 594 of 600 sampled). In the combined run the Java sync therefore
compresses against the dictionary like every other loader, and the re-mint leaves every row
dictionary-compressed regardless.

## What `verify_all_docs_db.py` checks

Run last, on the database as it will ship:

1. every rebuilt set has rows served through a template;
2. every templated row in the database names an existing `Templates` row, reassembles, decodes the
   way the app decodes it, and parses as JSON (Dokka `package-list` rows are plain text and are
   reported, not failed);
3. a sample per set renders through its stored template (the Android renderer's `TemplateCheck`),
   within the app's 4 MiB cap;
4. against the original: no row appeared or vanished outside the sets' paths, and no tooltip button
   that resolved before now points at nothing (removed Tooltips are reported — the stdlib sync
   deletes tooltips whose page is gone, by design);
5. `PRAGMA quick_check`.

```bash
python3 scripts/build_all_docs/verify_all_docs_db.py new.db --original documentation.db \
    --sets kotlin-website,kotlin-stdlib,java,android --sample 20
```
