"""Regression tests for insert_optimized_media.py's destructive paths.

Every test here covers a way this script could delete a row it shouldn't -
the LIKE-wildcard over-match in delete_content, and the "no pages to check
against" case that would otherwise wipe the whole image corpus.
"""
import shutil
import sqlite3

import pytest

from insert_optimized_media import (
    IMAGES_URL_PREFIX,
    collect_referenced_media,
    delete_content,
    delete_unreferenced_media,
)
from optimize_media import Logger
from populate_db import DictionaryCompressor

SCHEMA = """
CREATE TABLE Languages (id INTEGER PRIMARY KEY AUTOINCREMENT, value TEXT NOT NULL UNIQUE);
CREATE TABLE ContentTypes (id INTEGER PRIMARY KEY AUTOINCREMENT, value TEXT NOT NULL UNIQUE,
                           compression TEXT NOT NULL);
CREATE TABLE Content (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    path TEXT NOT NULL,
    languageID INTEGER NOT NULL,
    content BLOB NOT NULL,
    contentTypeID INTEGER NOT NULL,
    templateId INTEGER NOT NULL DEFAULT 0,
    UNIQUE(path)
);
"""

PAGE_TYPE_ID = 12
# Content is dictionary-compressed from schema 2.0.0 on (ADFA-5153), and
# insert_optimized_media reads pages back through that dictionary, so these
# fixtures have to speak it too.
DICTIONARY = bytes(range(256)) * 64
needs_brotli_cli = pytest.mark.skipif(shutil.which("brotli") is None, reason="brotli CLI not installed")

pytestmark = needs_brotli_cli


@pytest.fixture
def compressor():
    instance = DictionaryCompressor(DICTIONARY)
    yield instance
    instance.close()


@pytest.fixture
def conn():
    connection = sqlite3.connect(":memory:")
    connection.executescript(SCHEMA)
    connection.execute("INSERT INTO Languages (id, value) VALUES (1, 'en-US')")
    connection.execute("INSERT INTO ContentTypes (id, value, compression) VALUES (?, 'text/html', 'brotli')",
                       (PAGE_TYPE_ID,))
    yield connection
    connection.close()


def add_row(conn, path, blob=b"x", template_id=0):
    conn.execute(
        "INSERT INTO Content (path, languageID, content, contentTypeID, templateId) VALUES (?, 1, ?, ?, ?)",
        (path, blob, PAGE_TYPE_ID, template_id),
    )


def paths(conn):
    return {row[0] for row in conn.execute("SELECT path FROM Content")}


def page_blob(compressor, *image_names):
    """A stored page blob referencing each image the way md_to_json bakes it
    in: an HTML src="..." attribute inside JSON, so the filename is followed
    by an escaped quote."""
    srcs = "".join(f'<img src=\\"{IMAGES_URL_PREFIX}{name}\\">' for name in image_names)
    return compressor.compress(f'{{"blocks":[{{"html":"{srcs}"}}]}}'.encode("utf-8"))


class TestDeleteContent:
    def test_removes_the_row_and_its_chunk_fragments(self, conn):
        add_row(conn, "k/html/images/big.png")
        add_row(conn, "k/html/images/big.png-1")
        add_row(conn, "k/html/images/big.png-2")
        add_row(conn, "k/html/images/other.png")

        delete_content(conn, "k/html/images/big.png")

        assert paths(conn) == {"k/html/images/other.png"}

    def test_underscore_is_not_treated_as_a_wildcard(self, conn):
        # "_" in a LIKE pattern matches any single character, so an unescaped
        # "k/html/_nav.html-%" would also match "k/html/Xnav.html-1" and take
        # an unrelated page's chunk fragment with it.
        add_row(conn, "k/html/_nav.html")
        add_row(conn, "k/html/_nav.html-1")
        add_row(conn, "k/html/Xnav.html")
        add_row(conn, "k/html/Xnav.html-1")

        delete_content(conn, "k/html/_nav.html")

        assert paths(conn) == {"k/html/Xnav.html", "k/html/Xnav.html-1"}

    def test_percent_is_not_treated_as_a_wildcard(self, conn):
        add_row(conn, "k/html/images/100%.png")
        add_row(conn, "k/html/images/100%.png-1")
        add_row(conn, "k/html/images/100-other.png-1")

        delete_content(conn, "k/html/images/100%.png")

        assert paths(conn) == {"k/html/images/100-other.png-1"}


class TestDeleteUnreferencedMedia:
    def test_removes_only_images_no_page_references(self, conn, compressor):
        add_row(conn, "k/html/page.html", page_blob(compressor, "kept.png"), template_id=2)
        add_row(conn, "k/html/images/kept.png")
        add_row(conn, "k/html/images/orphan.png")
        add_row(conn, "k/html/images/orphan.png-1")

        removed = delete_unreferenced_media(conn, PAGE_TYPE_ID, Logger(None), compressor)

        assert removed == 1
        assert paths(conn) == {"k/html/page.html", "k/html/images/kept.png"}

    def test_refuses_to_run_when_no_page_references_any_image(self, conn, compressor):
        # populate_db.py hasn't written its pages yet (or was skipped): the
        # reference scan comes back empty and every stored image looks like
        # garbage. Deleting the whole corpus is never what was meant.
        add_row(conn, "k/html/images/a.png")
        add_row(conn, "k/html/images/b.png")

        with pytest.raises(RuntimeError, match="no page references any image"):
            delete_unreferenced_media(conn, PAGE_TYPE_ID, Logger(None), compressor)

        assert paths(conn) == {"k/html/images/a.png", "k/html/images/b.png"}

    def test_empty_database_is_not_an_error(self, conn, compressor):
        assert delete_unreferenced_media(conn, PAGE_TYPE_ID, Logger(None), compressor) == 0

    def test_untemplated_rows_are_not_scanned_for_references(self, conn, compressor):
        # templateId 0 marks a raw asset, not a page; only page/nav rows carry
        # the JSON that image references live in.
        add_row(conn, "k/html/page.html", page_blob(compressor, "kept.png"), template_id=2)
        add_row(conn, "k/html/images/kept.png")

        assert collect_referenced_media(conn, PAGE_TYPE_ID, compressor) == {"kept.png"}
