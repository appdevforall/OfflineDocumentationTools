CREATE TABLE Languages (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        value TEXT NOT NULL UNIQUE
    );
CREATE TABLE sqlite_sequence(name,seq);
CREATE TABLE ContentTypes (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        value TEXT NOT NULL UNIQUE,
        compression TEXT NOT NULL
    );
CREATE TABLE TooltipCategories (
 id       INTEGER PRIMARY KEY,
 category TEXT NOT NULL
);
CREATE TABLE TooltipButtonNumbers (
  'id'   INTEGER UNIQUE -- Manually assigned so buttons show in the order we want.
);
CREATE TABLE IF NOT EXISTS "Content" (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        path TEXT NOT NULL,
        languageID INTEGER NOT NULL,
        content BLOB NOT NULL,
        contentTypeID INTEGER NOT NULL,
        FOREIGN KEY (languageID) REFERENCES Languages(id),
        FOREIGN KEY (contentTypeID) REFERENCES ContentTypes(id),
        UNIQUE('path')
    );
CREATE TABLE `ide_tooltip_table` (
        `tooltipCategory` TEXT NOT NULL,
        `tooltipTag` TEXT NOT NULL,
        `tooltipSummary` TEXT NOT NULL,
        `tooltipDetail` TEXT NOT NULL,
        `tooltipButtons` TEXT NOT NULL,
        PRIMARY KEY(`tooltipCategory`, `tooltipTag`));
CREATE TABLE IF NOT EXISTS "Tooltips" (
 'id' INTEGER PRIMARY KEY AUTOINCREMENT,
'categoryId' INTEGER NOT NULL,
 'tag' TEXT NOT NULL,
 'summary' TEXT NOT NULL,
 'detail' TEXT NOT NULL,
 UNIQUE ('categoryId', 'tag'),
 FOREIGN KEY(categoryId) REFERENCES TooltipCategories(id)
);
CREATE TABLE TooltipButtons (
 'tooltipId' INTEGER,
 'buttonNumberId' INTEGER,
 'description' TEXT,
 'uri' TEXT,
 FOREIGN KEY(tooltipId) REFERENCES Tooltips(id),
 FOREIGN KEY(buttonNumberId) REFERENCES TooltipButtonNumbers(id)
);
CREATE TABLE LastChange (
        documentationSet TEXT,
        changeTime TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
        who TEXT
    );
CREATE TABLE DocumentationDatabaseVersion (
  major      INT NOT NULL,
  minor      INT NOT NULL,
  patch      INT NOT NULL,
  who        TEXT NOT NULL,
  comment    TEXT NOT NULL,
  changeTime TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);
