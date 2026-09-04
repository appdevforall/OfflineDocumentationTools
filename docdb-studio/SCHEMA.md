-- docdb-studio: the documentation.db schema.
--
-- A verbatim dump of sqlite_master from a real database - not a hand-written
-- description, and not the sqlite3 CLI's `.schema` output either, which invents
-- an `IF NOT EXISTS` for any table whose stored name is quoted. What is below is
-- exactly what SQLite has stored, so it can be diffed against a live file.
--
-- Regenerate with:
--   sqlite3 documentation.db "SELECT sql || ';' FROM sqlite_master WHERE sql IS NOT NULL ORDER BY CASE type WHEN 'table' THEN 0 WHEN 'index' THEN 1 ELSE 2 END, name;"
-- and put this header back on top.
--
-- Generated from a September 2026 documentation.db, DocumentationDatabaseVersion
-- 2.0.0, with CodeOnTheGo's docs/docdb/ADFA-5469-unquote-unique-constraints.sql
-- applied. If your copy predates that script, Templates and BookCategories still
-- read UNIQUE('name') and UNIQUE('category') - same indexes either way, since
-- SQLite resolves a string literal in that position back to the column
-- (https://sqlite.org/quirks.html#dblquote); the quotes were only ever a
-- readability cost.
--
-- Content, Tooltips, TooltipButtons and TooltipButtonNumbers below still carry
-- that quoting, along with the `CREATE TABLE "Content"` / `"Tooltips"` rename
-- artifacts. That is accurate, not an oversight: ADFA-5470 tracks clearing them.
-- Rebuilding Content is a bigger job than it looks - AddBook is an AFTER INSERT
-- trigger on it, so a naive row copy fires once per PDF and corrupts Bookshelf.
--
-- The PUCC_* tables are unrelated to documentation tooling. They are included
-- here because this is a faithful dump; do not maintain them.
--
-- What the previous revision of this file was missing, for scale: Templates,
-- BookCategories, Bookshelf, CompressionDictionary, DocumentationDatabaseVersion,
-- the six PUCC_* tables, both triggers, and Content.templateId. It also still
-- listed ide_tooltip_table, which no longer exists.

CREATE TABLE BookCategories (
  id          INTEGER PRIMARY KEY AUTOINCREMENT,
  category    STRING,
  description STRING DEFAULT '',
  UNIQUE(category)
);
CREATE TABLE Bookshelf (
  contentID      INTEGER NOT NULL,
  title          STRING DEFAULT '',
  description    STRING DEFAULT '',
  bookCategoryID INTEGER,
  FOREIGN KEY (bookCategoryID) REFERENCES BookCategories(id),
  UNIQUE(title, bookCategoryId)
);
CREATE TABLE CompressionDictionary (
    id   INTEGER PRIMARY KEY CHECK (id = 1),
    data BLOB NOT NULL
);
CREATE TABLE "Content" (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        path TEXT NOT NULL,
        languageID INTEGER NOT NULL,
        content BLOB NOT NULL,
        contentTypeID INTEGER NOT NULL, templateId INTEGER NOT NULL DEFAULT 0,
        FOREIGN KEY (languageID) REFERENCES Languages(id),
        FOREIGN KEY (contentTypeID) REFERENCES ContentTypes(id),
        UNIQUE('path')
    );
CREATE TABLE ContentTypes (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        value TEXT NOT NULL UNIQUE,
        compression TEXT NOT NULL
    );
CREATE TABLE DocumentationDatabaseVersion (
  -- From https://semver.org/
  --
  -- Given a version number MAJOR.MINOR.PATCH, increment the:
  --   MAJOR version when you make incompatible API changes
  --   MINOR version when you add functionality in a backward compatible manner
  --   PATCH version when you make backward compatible bug fixes
  --
  -- Additional labels for pre-release and build metadata are available as extensions to the MAJOR.MINOR.PATCH format.
  major      INT NOT NULL,
  minor      INT NOT NULL,
  patch      INT NOT NULL,
  who        TEXT NOT NULL, -- Who made the change?
  comment    TEXT NOT NULL, -- What changed?
  changeTime TIMESTAMP DEFAULT CURRENT_TIMESTAMP -- Don't provide this. The default is fine.
);
CREATE TABLE Languages (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        value TEXT NOT NULL UNIQUE
    );
CREATE TABLE LastChange (
        documentationSet TEXT,
        changeTime TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
        who TEXT
    );
CREATE TABLE 'PUCC_Classes' (
  id        INTEGER PRIMARY KEY AUTOINCREMENT,
  classname STRING
);
CREATE TABLE 'PUCC_ProfessorAssignments' (
  id          INTEGER PRIMARY KEY AUTOINCREMENT,
  professorID INTEGER NOT NULL,
  sectionID   INTEGER NOT NULL,
  FOREIGN KEY (professorID) REFERENCES PUCC_Professors(id),
  FOREIGN KEY (sectionID)   REFERENCES PUCC_Sections(id)
);
CREATE TABLE 'PUCC_Professors' (
  id        INTEGER PRIMARY KEY AUTOINCREMENT,
  firstName STRING,
  lastName  STRING
);
CREATE TABLE 'PUCC_Sections' (
  id          INTEGER PRIMARY KEY AUTOINCREMENT,
  sectionName STRING,
  classID     INTEGER NOT NULL,
  FOREIGN KEY (classID) REFERENCES PUCC_Classes(id)
);
CREATE TABLE 'PUCC_StudentAssignments' (
  id        INTEGER PRIMARY KEY AUTOINCREMENT,
  studentID INTEGER NOT NULL,
  sectionID INTEGER NOT NULL,
  FOREIGN KEY (studentID) REFERENCES PUCC_Students(id),
  FOREIGN KEY (sectionID) REFERENCES PUCC_Sections(id)
);
CREATE TABLE 'PUCC_Students' (
  id        INTEGER PRIMARY KEY AUTOINCREMENT,
  firstName STRING,
  lastName  STRING
);
CREATE TABLE Templates (
  id      INTEGER PRIMARY KEY AUTOINCREMENT,
  name    TEXT NOT NULL,
  content BLOB NOT NULL,
  UNIQUE(name)
);
CREATE TABLE TooltipButtonNumbers (
  'id'   INTEGER UNIQUE -- Manually assigned so buttons show in the order we want.
);
CREATE TABLE TooltipButtons (
 'tooltipId' INTEGER,
 'buttonNumberId' INTEGER,
 'description' TEXT,
 'uri' TEXT,
 FOREIGN KEY(tooltipId) REFERENCES Tooltips(id),
 FOREIGN KEY(buttonNumberId) REFERENCES TooltipButtonNumbers(id)
);
CREATE TABLE TooltipCategories (
 id       INTEGER PRIMARY KEY,
 category TEXT NOT NULL
);
CREATE TABLE "Tooltips" (
 'id' INTEGER PRIMARY KEY AUTOINCREMENT,
'categoryId' INTEGER NOT NULL,
 'tag' TEXT NOT NULL,
 'summary' TEXT NOT NULL,
 'detail' TEXT NOT NULL,
 UNIQUE ('categoryId', 'tag'),
 FOREIGN KEY(categoryId) REFERENCES TooltipCategories(id)
);
CREATE TABLE sqlite_sequence(name,seq);
CREATE TRIGGER AddBook AFTER INSERT ON Content WHEN NEW.path LIKE '%.pdf'
BEGIN
    INSERT INTO Bookshelf (contentID, title) VALUES (NEW.id, CURRENT_TIMESTAMP || NEW.id);
END;
CREATE TRIGGER DeleteBook AFTER DELETE ON Content WHEN OLD.path LIKE '%.pdf'
BEGIN
DELETE FROM Bookshelf WHERE contentID = OLD.id;
END;
