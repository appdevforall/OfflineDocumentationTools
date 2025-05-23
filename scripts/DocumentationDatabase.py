import os
import os.path as path
import mimetypes
import sqlite3
import sys
import brotli
import io
from PIL import Image
import subprocess
import contextlib

class DocumentationDatabase:
    COMPRESSORS = {
        'text': 'brotli',
        'image': 'none',
        'application': 'brotli'
    }

    CONTENT_TYPES = {
        'text/plain',
        'text/html',
        'text/css',
        'text/markdown',
        'image/jpeg',
        'image/png',
        'image/gif',
        'application/json',
        'application/xml'
    }

    OVERRIDE_MIMETYPES = {
        'image/jpeg': 'none',
        'image/png': 'none',
        'image/gif': 'none',
        'image/svg+xml': 'brotli'
    }

    SCHEMA_SQL = """
    CREATE TABLE IF NOT EXISTS Content (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        path TEXT NOT NULL,
        languageID INTEGER NOT NULL,
        content BLOB NOT NULL,
        contentTypeID INTEGER NOT NULL,
        FOREIGN KEY (languageID) REFERENCES Languages(id),
        FOREIGN KEY (contentTypeID) REFERENCES ContentTypes(id)
    );

    CREATE TABLE IF NOT EXISTS Languages (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        value TEXT NOT NULL UNIQUE
    );

    CREATE TABLE IF NOT EXISTS ContentTypes (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        value TEXT NOT NULL UNIQUE,
        compression TEXT NOT NULL
    );

    CREATE TABLE IF NOT EXISTS ide_tooltip_table (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        path TEXT NOT NULL,
        languageID INTEGER NOT NULL,
        content TEXT NOT NULL,
        FOREIGN KEY (languageID) REFERENCES Languages(id)
    );
    """

    def __init__(self, database_path):
        self.database_path = database_path
        self.input_bytes = 0
        self.stored_bytes = 0
        # Create the database if it doesn't exist or is empty
        if not os.path.exists(database_path) or os.path.getsize(database_path) == 0:
            with self.get_connection() as connection:
                cursor = connection.cursor()
                self.create_tables(cursor)
                self.populate_content_types(cursor)
                self.populate_languages(cursor)
                connection.commit()
        else:
            # Check if the database conforms to the schema
            with self.get_connection() as connection:
                cursor = connection.cursor()
                cursor.execute("SELECT name FROM sqlite_master WHERE type='table';")
                tables = cursor.fetchall()
                expected_tables = {'ide_tooltip_table', 'Content', 'Languages', 'ContentTypes'}
                existing_tables = {table[0] for table in tables}
                # Ignore any tables that start with 'sqlite_'
                filtered_tables = {table for table in existing_tables if not table.startswith('sqlite_')}
                if filtered_tables != expected_tables:
                    raise ValueError("Database schema does not match the expected schema")

    @contextlib.contextmanager
    def get_connection(self):
        """Context manager for database connections."""
        connection = sqlite3.connect(self.database_path)
        connection.execute("PRAGMA foreign_keys = ON;")  # Enable foreign key constraints
        try:
            yield connection
        finally:
            connection.close()

    def get_exts(self, files):
        exts = sorted({path.splitext(i)[-1] for i in files if len(path.splitext(i)[-1]) != 0})
        noexts = sorted([i for i in files if len(path.splitext(i)[-1]) == 0])
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
                sys.exit(1)
            sql.add(f"""INSERT INTO ContentTypes (value, compression) VALUES ('{mime_type}', '{compressor}');""")
        # Add image/svg+xml to ContentTypes
        sql.add("""INSERT INTO ContentTypes (value, compression) VALUES ('image/svg+xml', 'brotli');""")
        cursor.executescript("BEGIN;\n" + "\n".join(sql) + "\nCOMMIT;\n")

    def populate_languages(self, cursor):
        cursor.execute("INSERT INTO Languages (value) VALUES ('en-US');")

    def add_file(self, path, content, language):
        with self.get_connection() as connection:
            cursor = connection.cursor()
            # Get languageID for the given language
            cursor.execute("SELECT id FROM Languages WHERE value = ?", (language,))
            language_id = cursor.fetchone()[0]
            # Detect content type from file extension
            ext = os.path.splitext(path)[1]
            if ext not in mimetypes.types_map:
                print(f"Skipping file {path}: Unsupported file extension: {ext}")
                return
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
                elif content_type in self.OVERRIDE_MIMETYPES:
                    # Use the compression method specified in OVERRIDE_MIMETYPES
                    compressor = self.OVERRIDE_MIMETYPES[content_type]
                    if compressor == 'brotli':
                        compressed_content = brotli.compress(content)
                    else:
                        compressed_content = content
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
            # Update byte counters
            self.input_bytes += len(content)
            self.stored_bytes += len(compressed_content)
            connection.commit()

    def get_file(self, path, language):
        with self.get_connection() as connection:
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

    def emit_summary(self, label=None):
        """
        Prints a summary with the total count of files stored and the number of files grouped by each content type.
        If a label is provided, it will be printed at the start of the method.
        """
        if label:
            print(label)
        with self.get_connection() as connection:
            cursor = connection.cursor()
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

    def stats(self):
        with self.get_connection() as connection:
            cursor = connection.cursor()
            # Get count of files
            cursor.execute("SELECT COUNT(*) FROM Content")
            count = cursor.fetchone()[0]
            return count, self.input_bytes, self.stored_bytes

    # Removed write_languages() method 

mimetypes.types_map[".svg"] = "image/svg+xml" 