# Java API docs → documentation.db

Replaces the scraped Java API HTML in `documentation.db` with the JSON that the kdoc-to-json
plugin's Javadoc mode produces, and installs the Pebble templates that render it.

```bash
# 1. Generate the JSON (see Dokka-plugin-kdoc2json/scripts/java)
Dokka-plugin-kdoc2json/scripts/java/build-jdk-json-docs.sh -j /path/to/jdk-17

# 2. Flatten the renderer's templates into standalone ones for the database
python3 scripts/sync_java_docs/flatten_templates.py

# 3. Look at what would change, then do it
python3 scripts/sync_java_docs/sync_javadoc_json_to_db.py <json-root> --db documentation.db --dry-run
python3 scripts/sync_java_docs/sync_javadoc_json_to_db.py <json-root> --db documentation.db
```

## How a page is stored

The same arrangement the Kotlin website docs already use, and it is worth being explicit about
because the pieces disagree with each other at first glance:

| Column | Value | Why |
| --- | --- | --- |
| `path` | `j/html/api/…/ArrayList.html` | unchanged — it is the URL a browser asks for |
| `content` | **JSON**, shared-dictionary Brotli | the data; the template turns it into a page |
| `contentTypeID` | `text/html` | the type of the *served* page, not of the blob |
| `templateId` | one of the nine `javadoc-*.peb` rows | chosen from the JSON's `page` field |

## Why the templates are flattened

`pebble-renderer/` and the database's reader run Pebble in different environments, and the
database's is narrower: templates are stored one per row with no loader that resolves
`{% extends %}` / `{% import %}` by name, and only Pebble's built-in filters exist. Every template
already in the database is self-contained, so that is the contract.

`flatten_templates.py` therefore generates the database copies from the renderer's rather than
having a second set maintained by hand: it inlines the parent template and the imported macros,
drops the `href` filter and turns `doc` into the built-in `raw`.

Three things are rewritten into the JSON on the way in, all so the templates need nothing beyond
what the reader already passes:

- **`.json` links become `.html`**, since the row's path is what the browser requests. This is done
  on the *parsed* JSON, because in the raw text a link inside documentation HTML is
  `href=\"List.json\"` with escaped quotes, and a regex over the unparsed form misses it.
- **`pathToRoot` is injected**, since the templates need it for the stylesheet and the top nav and
  the reader passes nothing but the JSON.
- The `page` field selects the template, so that mapping lives here and not in the reader.

## What it leaves alone

About half the rows under `j/html/api/` are page kinds this pipeline does not generate —
`class-use/` (4,672), `package-use` (224), the tree pages (225), `serialized-form`, `help-doc`.
They are working documentation, nothing in the new pages links to them, and deleting them would
take information out of the database. They are left as HTML unless you pass `--delete-missing`.

`element-list` is not a page and is copied through unchanged with no template.
