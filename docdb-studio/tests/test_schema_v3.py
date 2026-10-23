"""docdb-studio on a schema 3.0.0 database (ADFA-6740): keyed tooltip tables and
templates Brotli-compressed against CompressionDictionary.

The fixture is a 2.x database converted by the real scripts/migrate_tooltip_schema.py,
so these tests track whatever that step produces. Needs the brotli CLI."""

import shutil
import sqlite3
import subprocess
import sys
import tempfile
from pathlib import Path

import pytest

import docdb_studio as ds

MIGRATE = Path(__file__).resolve().parents[2] / "scripts" / "migrate_tooltip_schema.py"
DICTIONARY = b"<html><head><title>{{ title }}</title></head><body>{% for s in b %}{{ s.r | raw }}{% endfor %}" * 40
TEMPLATE = b"<html><title>{{ title | raw }}</title>{% for s in b %}{{ s.r | raw }}{% endfor %}</html>\n"

# What Code On The Go runs on a 3.0.0 database (migrate_tooltip_schema.APP_TOOLTIP_QUERY / APP_BUTTONS_QUERY).
APP_TOOLTIP = ("SELECT T.id, X.summary, X.detail FROM Tooltips AS T JOIN TooltipTexts AS X ON X.id = T.id "
               "WHERE T.categoryId IN (SELECT id FROM TooltipCategories WHERE category = ?) AND T.tag = ?")
APP_BUTTONS = ("SELECT D.description, U.uri FROM TooltipButtons AS B "
               "JOIN TooltipButtonDescriptions AS D ON D.id = B.descriptionId "
               "JOIN TooltipButtonUris AS U ON U.id = B.uriId WHERE B.tooltipId = ? ORDER BY B.buttonNumberId")

SCHEMA = """
CREATE TABLE TooltipCategories (id INTEGER PRIMARY KEY, category TEXT NOT NULL);
CREATE TABLE TooltipButtonNumbers (id INTEGER UNIQUE);
CREATE TABLE Tooltips (id INTEGER PRIMARY KEY AUTOINCREMENT, categoryId INTEGER NOT NULL, tag TEXT NOT NULL,
  summary TEXT NOT NULL, detail TEXT NOT NULL, UNIQUE (categoryId, tag));
CREATE TABLE TooltipButtons (tooltipId INTEGER, buttonNumberId INTEGER, description TEXT, uri TEXT);
CREATE TABLE DocumentationDatabaseVersion (major INT NOT NULL, minor INT NOT NULL, patch INT NOT NULL,
  who TEXT NOT NULL, comment TEXT NOT NULL, changeTime TIMESTAMP DEFAULT CURRENT_TIMESTAMP);
CREATE TABLE Content (id INTEGER PRIMARY KEY AUTOINCREMENT, path TEXT NOT NULL, languageID INTEGER NOT NULL,
  content BLOB NOT NULL, contentTypeID INTEGER NOT NULL, templateId INTEGER NOT NULL DEFAULT 0, UNIQUE(path));
CREATE TABLE CompressionDictionary (id INTEGER PRIMARY KEY CHECK (id = 1), data BLOB NOT NULL);
CREATE TABLE Templates (id INTEGER PRIMARY KEY AUTOINCREMENT, name TEXT NOT NULL, content BLOB NOT NULL,
  UNIQUE(name));
CREATE TABLE LastChange (documentationSet TEXT, changeTime TIMESTAMP DEFAULT CURRENT_TIMESTAMP, who TEXT);
INSERT INTO TooltipCategories VALUES (1, 'ide'), (2, 'kotlin');
INSERT INTO TooltipButtonNumbers VALUES (1), (2), (3);
INSERT INTO DocumentationDatabaseVersion (major, minor, patch, who, comment) VALUES (2, 0, 0, 'test', 'test');
INSERT INTO Content (path, languageID, content, contentTypeID) VALUES ('a/x.html', 1, x'00', 1);
INSERT INTO Tooltips VALUES (10, 1, 'build', 'Build it', 'Builds <b>it</b>'),
                            (11, 1, 'run', 'Run it', ''),
                            (12, 2, 'fun', '', 'A function');
INSERT INTO TooltipButtons VALUES (10, 1, 'View full documentation', 'a/x.html'),
                                  (10, 2, 'Examples', 'a/build.html#ex'),
                                  (11, 1, 'View full documentation', 'a/x.html');
"""

pytestmark = pytest.mark.skipif(shutil.which("brotli") is None, reason="needs the brotli CLI")


def _cli(data: bytes, *args: str) -> bytes:
    with tempfile.NamedTemporaryFile() as f:
        f.write(DICTIONARY)
        f.flush()
        return subprocess.run(["brotli", "-D", f.name, *args, "-c"], input=data, capture_output=True,
                              check=True).stdout


def _v2(tmp_path: Path) -> Path:
    db = tmp_path / "documentation.db"
    with sqlite3.connect(db) as conn:
        conn.executescript(SCHEMA)
        conn.execute("INSERT INTO CompressionDictionary VALUES (1, ?)", (DICTIONARY,))
        conn.execute("INSERT INTO Templates (name, content) VALUES ('page.peb', ?)", (TEMPLATE,))
    return db


@pytest.fixture
def v3(tmp_path: Path) -> Path:
    db = _v2(tmp_path)
    subprocess.run([sys.executable, str(MIGRATE), str(db)], check=True, capture_output=True)
    assert ds.database_major_version(db) == 3
    return db


def _q(db: Path, sql: str, params: tuple = ()) -> list:
    with sqlite3.connect(db) as conn:
        return conn.execute(sql, params).fetchall()


def _orphans(db: Path) -> list:
    return [
        _q(db, "SELECT COUNT(*) FROM TooltipTexts WHERE id NOT IN (SELECT id FROM Tooltips)")[0][0],
        _q(db, "SELECT COUNT(*) FROM TooltipButtonDescriptions WHERE id NOT IN "
               "(SELECT descriptionId FROM TooltipButtons)")[0][0],
        _q(db, "SELECT COUNT(*) FROM TooltipButtonUris WHERE id NOT IN (SELECT uriId FROM TooltipButtons)")[0][0],
    ]


def test_version_3_is_supported() -> None:
    assert ds.SUPPORTED_DATABASE_MAJOR_VERSION >= 3


def test_reads_tooltips_and_buttons(v3: Path) -> None:
    assert ds.get_total_count(v3) == 3
    assert ds.get_page(v3, 50, 0) == [
        (10, "ide", "build", "Build it", "Builds <b>it</b>"),
        (11, "ide", "run", "Run it", ""),
        (12, "kotlin", "fun", "", "A function"),
    ]
    assert ds.get_tooltip_by_id(v3, 10) == ("ide", "build", "Build it", "Builds <b>it</b>")
    assert ds.get_tooltip_for_edit(v3, 12) == (2, "kotlin", "fun", "", "A function")
    assert ds.get_tooltip_buttons(v3, 10) == [(1, "View full documentation", "a/x.html"),
                                              (2, "Examples", "a/build.html#ex")]
    assert ds.get_total_count(v3, "Run") == 1  # summary
    assert ds.get_total_count(v3, "build.html") == 1  # button uri
    assert ds.get_button_reference_counts(v3) == {"a/x.html": 2, "a/build.html": 1}
    assert [r[0] for r in ds.get_all_button_rows(v3)] == [10, 10, 11]
    assert ds.find_empty_summary_tooltips(v3) == [(12, "fun")]
    assert ds.get_tooltip_export_rows(v3, "build")[0][:6] == [
        "ide", "build", "Build it", "Builds <b>it</b>", "View full documentation", "a/x.html"]


def test_insert_takes_the_text_id_and_the_app_finds_it(v3: Path) -> None:
    new_id = ds.insert_tooltip(v3, 2, "val", "A value", "Immutable")
    ds.replace_tooltip_buttons(v3, new_id, [(1, "View full documentation", "a/x.html"), (2, "New", "k/val.html")])
    assert _q(v3, "SELECT id FROM TooltipTexts WHERE summary = 'A value'") == [(new_id,)]
    (row,) = _q(v3, APP_TOOLTIP, ("kotlin", "val"))
    assert row == (new_id, "A value", "Immutable")
    assert _q(v3, APP_BUTTONS, (new_id,)) == [("View full documentation", "a/x.html"), ("New", "k/val.html")]
    # The shared strings are reused, not stored twice.
    assert _q(v3, "SELECT COUNT(*) FROM TooltipButtonUris WHERE uri = 'a/x.html'") == [(1,)]
    assert _orphans(v3) == [0, 0, 0]


def test_duplicate_insert_leaves_no_text_behind(v3: Path) -> None:
    with pytest.raises(sqlite3.IntegrityError):
        ds.insert_tooltip(v3, 1, "build", "dup", "dup")
    assert _orphans(v3) == [0, 0, 0]


def test_update_renames_and_rewrites_the_text(v3: Path) -> None:
    ds.update_tooltip(v3, 10, 2, "compile", "Compile it", "Compiles")
    assert _q(v3, APP_TOOLTIP, ("ide", "build")) == []
    assert _q(v3, APP_TOOLTIP, ("kotlin", "compile")) == [(10, "Compile it", "Compiles")]
    assert len(_q(v3, APP_BUTTONS, (10,))) == 2
    with pytest.raises(sqlite3.IntegrityError):
        ds.update_tooltip(v3, 11, 2, "compile", "x", "y")


def test_replacing_buttons_drops_strings_nothing_uses(v3: Path) -> None:
    ds.replace_tooltip_buttons(v3, 10, [(1, "View full documentation", "a/x.html")])
    assert _q(v3, "SELECT COUNT(*) FROM TooltipButtonDescriptions WHERE description = 'Examples'") == [(0,)]
    assert _orphans(v3) == [0, 0, 0]


def test_delete_removes_text_and_unused_strings(v3: Path) -> None:
    assert ds.delete_tooltips_bulk(v3, [10]) == 1
    assert _q(v3, "SELECT id FROM TooltipTexts ORDER BY id") == [(11,), (12,)]
    assert _orphans(v3) == [0, 0, 0]
    assert _q(v3, APP_BUTTONS, (11,)) == [("View full documentation", "a/x.html")]


def test_csv_import_updates_and_inserts(v3: Path) -> None:
    rows = [
        ["ide", "run", "Run now", "Runs", "Run docs", "a/run.html", "", "", "", ""],
        ["ide", "stop", "Stop it", "", "View full documentation", "a/x.html", "", "", "", ""],
    ]
    assert ds.import_csv_rows(v3, rows, {"ide": 1, "kotlin": 2}, mode="update") == (1, 1)
    assert _q(v3, APP_TOOLTIP, ("ide", "run")) == [(11, "Run now", "Runs")]
    assert _q(v3, APP_BUTTONS, (11,)) == [("Run docs", "a/run.html")]
    (stop_id, _, _), = _q(v3, APP_TOOLTIP, ("ide", "stop"))
    assert stop_id > 12
    assert _orphans(v3) == [0, 0, 0]


def test_template_round_trips_compressed(v3: Path) -> None:
    ((tid, name),) = ds.get_templates(v3)
    assert name == "page.peb"
    assert ds.get_template_text(v3, tid) == TEMPLATE.decode()
    ds.save_template(v3, tid, "<p>{{ edited }} été</p>")
    ((stored,),) = _q(v3, "SELECT content FROM Templates WHERE id = ?", (tid,))
    assert stored[0] & 0x7F != 0x11  # not large-window
    assert _cli(stored, "-d").decode() == "<p>{{ edited }} été</p>"
    assert ds.get_template_text(v3, tid) == "<p>{{ edited }} été</p>"
    assert ds.database_major_version(v3) == 3


def test_template_stays_plain_below_3(tmp_path: Path) -> None:
    db = _v2(tmp_path)
    ((tid, _),) = ds.get_templates(db)
    assert ds.get_template_text(db, tid) == TEMPLATE.decode()
    ds.save_template(db, tid, "<p>plain</p>")
    assert _q(db, "SELECT content FROM Templates") == [(b"<p>plain</p>",)]


def test_version_is_read_newest_changetime_first(tmp_path: Path) -> None:
    db = _v2(tmp_path)
    with sqlite3.connect(db) as conn:
        conn.execute("UPDATE DocumentationDatabaseVersion SET changeTime = '2026-10-01 00:00:00'")
        conn.execute("INSERT INTO DocumentationDatabaseVersion (major, minor, patch, who, comment, changeTime) "
                     "VALUES (3, 0, 0, 't', 't', '2026-10-02 00:00:00')")
        conn.execute("INSERT INTO DocumentationDatabaseVersion (major, minor, patch, who, comment, changeTime) "
                     "VALUES (1, 0, 0, 't', 't', '2026-09-01 00:00:00')")
    assert ds.database_major_version(db) == 3
