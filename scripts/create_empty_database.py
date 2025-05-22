#! /usr/bin/python

import os
import os.path as path
import mimetypes
import sqlite3
import sys

class DocumentationDatabase:
    COMPRESSORS = {
        "application": "brotli",  # e.g. application/json
        "text": "brotli",
        "image": None,
    }
    SKIP_EXTS = {"", ".version"}
    OVERRIDE_MIMETYPES = {"image/svg+xml": "brotli"}

    SCHEMA_SQL = """
BEGIN;

CREATE TABLE ide_tooltip_table (
  tooltipTag      TEXT NOT NULL,
  tooltipCategory TEXT NOT NULL,
  tooltipSummary  TEXT NOT NULL,
  tooltipDetail   TEXT NOT NULL,
  tooltipButtons  TEXT NOT NULL,

  PRIMARY KEY(tooltipTag, tooltipCategory)
);

CREATE TABLE Content (
  path          TEXT    NOT NULL,
  languageID    INTEGER NOT NULL,
  content       BLOB    NOT NULL,
  contentTypeID INTEGER NOT NULL, 
  
  PRIMARY KEY (path, languageID),
  FOREIGN KEY(languageID)    REFERENCES Languages(id),
  FOREIGN KEY(contentTypeID) REFERENCES ContentTypes(id)
);

CREATE TABLE Languages (
  id    INTEGER PRIMARY KEY NOT NULL,
  value TEXT NOT NULL,

  UNIQUE(value)
);

CREATE TABLE ContentTypes (
  id          INTEGER PRIMARY KEY NOT NULL,
  value       TEXT NOT NULL,
  compression TEXT NOT NULL,

  UNIQUE(value)
);

COMMIT;
"""

    def __init__(self, database_path):
        self.database_path = database_path
        # Create the database if it doesn't exist
        if not os.path.exists(database_path):
            with sqlite3.connect(database_path) as connection:
                cursor = connection.cursor()
                self.write_create_tables(cursor)
        else:
            # Check if the database conforms to the schema
            with sqlite3.connect(database_path) as connection:
                cursor = connection.cursor()
                cursor.execute("SELECT name FROM sqlite_master WHERE type='table';")
                tables = cursor.fetchall()
                expected_tables = {'ide_tooltip_table', 'Content', 'Languages', 'ContentTypes'}
                existing_tables = {table[0] for table in tables}
                if existing_tables != expected_tables:
                    raise ValueError("Database schema does not match the expected schema.")

    def get_exts(self, files):
        exts = sorted({path.splitext(i)[-1] for i in files if len(path.splitext(i)[-1]) != 0})
        noexts = sorted([i for i in files if len(path.splitext(i)[-1]) == 0])
        print(f"There are {len(files)} files and {len(exts)} unique extensions. They are\n{"\n".join(exts)}")
        print(f"There are {len(noexts)} files with no extension. They are\n{"\n".join(noexts)}")
        return exts

    def write_create_tables(self, cursor):
        cursor.executescript(self.SCHEMA_SQL)

    def get_type_content(self, ext):
        if ext in self.SKIP_EXTS:
            print(f"Skipping file extension \"{ext}\".\n")
            return

        if ext not in mimetypes.types_map:
            print(f"WARNING: MIME type \"{ext}\" not in MIME type map. Skipping.\n")
            return

        mt = mimetypes.types_map[ext]
        major_type, minor_type = mt.split("/")

        if mt in self.OVERRIDE_MIMETYPES:
            compressor = self.OVERRIDE_MIMETYPES[mt]
        elif major_type in self.COMPRESSORS:
            compressor = self.COMPRESSORS[major_type]
        else:
            print(f"ERROR: Invalid major_type, \"{major_type}\" for MIME type \"{mt}\". Quitting.")
            sys.exit(1)

        return f"""INSERT INTO ContentTypes (value, compression) VALUES ('{mt}', '{compressor}');"""

    def write_filetypes_content(self, cursor, exts):
        sql = set()  # used to deduplicate the insert statements
        for ext in exts:
            type_content = self.get_type_content(ext)
            if type_content:
                sql.add(type_content)
        cursor.executescript("BEGIN;\n" + "\n".join(sql) + "\nCOMMIT;\n")

    def write_languages(self, cursor):
        cursor.execute("INSERT INTO Languages (value) VALUES ('en-US');")

    def create_empty_database(self, list_of_filenames):
        with open(list_of_filenames) as fd:
            files = [i.strip() for i in fd]
        exts = self.get_exts(files)
        with sqlite3.connect(self.database_path) as connection:
            cursor = connection.cursor()
            self.write_filetypes_content(cursor, exts)
            self.write_languages(cursor)


if __name__ == "__main__":
    # Example usage
    db = DocumentationDatabase("Documentation.db")
    db.create_empty_database("/tmp/files")
