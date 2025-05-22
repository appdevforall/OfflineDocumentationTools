import os
import os.path as path
import mimetypes
import sqlite3
import sys
import brotli
import io
from PIL import Image
import subprocess

class DocumentationDatabase:
    COMPRESSORS = {
        "application": "brotli",  # e.g. application/json
        "text": "brotli",
        "image": None,
    }
    SKIP_EXTS = {"", ".version"}
    OVERRIDE_MIMETYPES = {"image/svg+xml": "brotli"}
    CONTENT_TYPES = ["application/json", "text/plain", "text/html", "image/jpeg", "image/png", "image/gif", "text/x-python", "text/markdown", "text/css"]

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
                self.create_tables(cursor)
                self.populate_content_types(cursor)
                self.populate_languages(cursor)
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

    def create_tables(self, cursor):
        cursor.executescript(self.SCHEMA_SQL)

    def populate_content_types(self, cursor):
        sql = set()
        for mime_type in self.CONTENT_TYPES:
            major_type, minor_type = mime_type.split("/")
            if mime_type in self.OVERRIDE_MIMETYPES:
                compressor = self.OVERRIDE_MIMETYPES[mime_type]
            elif major_type in self.COMPRESSORS:
                compressor = self.COMPRESSORS[major_type]
            else:
                print(f"ERROR: Invalid major_type, \"{major_type}\" for MIME type \"{mime_type}\". Quitting.")
                sys.exit(1)
            sql.add(f"""INSERT INTO ContentTypes (value, compression) VALUES ('{mime_type}', '{compressor}');""")
        cursor.executescript("BEGIN;\n" + "\n".join(sql) + "\nCOMMIT;\n")

    def populate_languages(self, cursor):
        cursor.execute("INSERT INTO Languages (value) VALUES ('en-US');")

    def add_file(self, path, content, language):
        with sqlite3.connect(self.database_path) as connection:
            cursor = connection.cursor()
            # Get languageID for the given language
            cursor.execute("SELECT id FROM Languages WHERE value = ?", (language,))
            language_id = cursor.fetchone()[0]
            # Detect content type from file extension
            ext = os.path.splitext(path)[1]
            if ext not in mimetypes.types_map:
                raise ValueError(f"Unsupported file extension: {ext}")
            content_type = mimetypes.types_map[ext]
            # Special handling for image files
            if content_type.startswith('image/'):
                if content_type == 'image/png':
                    # Call pngquant in a subshell
                    process = subprocess.Popen(['pngquant', '--force', '--output', '-', '-'], stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
                    stdout, stderr = process.communicate(input=content)
                    if process.returncode != 0:
                        raise RuntimeError(f"pngquant failed: {stderr.decode()}")
                    compressed_content = stdout
                else:
                    compressed_content = content
            else:
                # Compress non-image files
                compressed_content = brotli.compress(content)
            # Get contentTypeID for the detected content type
            cursor.execute("SELECT id FROM ContentTypes WHERE value = ?", (content_type,))
            content_type_id = cursor.fetchone()[0]
            # Insert the file into the Content table
            cursor.execute(
                "INSERT INTO Content (path, languageID, content, contentTypeID) VALUES (?, ?, ?, ?)",
                (path, language_id, compressed_content, content_type_id)
            )

    def get_file(self, path, language):
        with sqlite3.connect(self.database_path) as connection:
            cursor = connection.cursor()
            # Get languageID for the given language
            cursor.execute("SELECT id FROM Languages WHERE value = ?", (language,))
            language_id = cursor.fetchone()[0]
            # Retrieve the file content and content type from the Content table
            cursor.execute("SELECT content, contentTypeID FROM Content WHERE path = ? AND languageID = ?", (path, language_id))
            result = cursor.fetchone()
            if result is None:
                raise FileNotFoundError(f"File not found: {path} for language: {language}")
            content, content_type_id = result
            # Get the content type
            cursor.execute("SELECT value FROM ContentTypes WHERE id = ?", (content_type_id,))
            content_type = cursor.fetchone()[0]
            # Decompress the content if necessary
            if content_type.startswith('image/'):
                # Image files are not compressed
                return io.BytesIO(content)
            else:
                # Decompress non-image files
                return io.BytesIO(brotli.decompress(content))

    def emit_summary(self):
        """
        Prints a summary with the total count of files stored and the number of files grouped by each content type.
        """
        with sqlite3.connect(self.database_path) as conn:
            cursor = conn.cursor()
            # Total count of files
            cursor.execute("SELECT COUNT(*) FROM Content")
            total_files = cursor.fetchone()[0]
            print(f"Total files stored: {total_files}")

            # Number of files grouped by content type
            cursor.execute('''
                SELECT ContentTypes.value, COUNT(*)
                FROM Content
                JOIN ContentTypes ON Content.contentTypeID = ContentTypes.id
                GROUP BY ContentTypes.value
            ''')
            print("Files by content type:")
            for mime_type, count in cursor.fetchall():
                print(f"  {mime_type}: {count}")

    # Removed write_languages() method 