import unittest
import os
import tempfile
import sqlite3
from create_empty_database import DocumentationDatabase


class TestDocumentationDatabase(unittest.TestCase):
    def setUp(self):
        # Create a temporary database file name for testing, but remove the file so DocumentationDatabase can create it
        self.temp_db_file = tempfile.NamedTemporaryFile(delete=False)
        self.temp_db_file.close()
        os.unlink(self.temp_db_file.name)
        self.db = DocumentationDatabase(self.temp_db_file.name)

    def tearDown(self):
        # Clean up the temporary database file if it exists
        if os.path.exists(self.temp_db_file.name):
            os.unlink(self.temp_db_file.name)

    def test_init_creates_database(self):
        # Check if the database file exists
        self.assertTrue(os.path.exists(self.temp_db_file.name))
        # Check if the tables are created
        with sqlite3.connect(self.temp_db_file.name) as connection:
            cursor = connection.cursor()
            cursor.execute("SELECT name FROM sqlite_master WHERE type='table';")
            tables = cursor.fetchall()
            self.assertIn(('ide_tooltip_table',), tables)
            self.assertIn(('Content',), tables)
            self.assertIn(('Languages',), tables)
            self.assertIn(('ContentTypes',), tables)

    def test_get_exts(self):
        files = ['file1.txt', 'file2.py', 'file3', 'file4.md']
        exts = self.db.get_exts(files)
        self.assertEqual(exts, ['.md', '.py', '.txt'])

    def test_get_type_content(self):
        # Test with a known extension
        ext = '.txt'
        expected = "INSERT INTO ContentTypes (value, compression) VALUES ('text/plain', 'brotli');"
        self.assertEqual(self.db.get_type_content(ext), expected)
        # Test with a skipped extension
        ext = '.version'
        self.assertIsNone(self.db.get_type_content(ext))
        # Test with an unknown extension
        ext = '.unknown'
        self.assertIsNone(self.db.get_type_content(ext))

    def test_create_empty_database(self):
        # Create a temporary file list
        with tempfile.NamedTemporaryFile(mode='w', delete=False) as temp_file:
            temp_file.write('file1.txt\nfile2.py\nfile3.md\n')
        try:
            self.db.create_empty_database(temp_file.name)
            # Check if the database has the expected content
            with sqlite3.connect(self.temp_db_file.name) as connection:
                cursor = connection.cursor()
                cursor.execute("SELECT value FROM ContentTypes;")
                content_types = cursor.fetchall()
                self.assertIn(('text/plain',), content_types)
                self.assertIn(('text/x-python',), content_types)
                self.assertIn(('text/markdown',), content_types)
        finally:
            os.unlink(temp_file.name)

    def test_error_on_bad_schema(self):
        # Create a DB with the wrong schema
        with tempfile.NamedTemporaryFile(delete=False) as bad_db_file:
            bad_db_file.close()
            with sqlite3.connect(bad_db_file.name) as connection:
                cursor = connection.cursor()
                cursor.execute("CREATE TABLE WrongTable (id INTEGER PRIMARY KEY);")
            # Should raise ValueError
            with self.assertRaises(ValueError):
                DocumentationDatabase(bad_db_file.name)
            os.unlink(bad_db_file.name)

    def test_accept_valid_preexisting_database(self):
        # Create a valid DB with the correct schema
        with tempfile.NamedTemporaryFile(delete=False) as valid_db_file:
            valid_db_file.close()
            with sqlite3.connect(valid_db_file.name) as connection:
                cursor = connection.cursor()
                cursor.executescript(DocumentationDatabase.SCHEMA_SQL)
            # Should NOT raise ValueError
            try:
                DocumentationDatabase(valid_db_file.name)
            finally:
                os.unlink(valid_db_file.name)

    def test_database_file_persists_after_object_goes_out_of_scope(self):
        # Create a temporary database file name
        with tempfile.NamedTemporaryFile(delete=False) as temp_file:
            temp_file.close()
            os.unlink(temp_file.name)
            # Create the database and let the object go out of scope
            DocumentationDatabase(temp_file.name)
            # Verify the file still exists
            self.assertTrue(os.path.exists(temp_file.name))
            os.unlink(temp_file.name)


if __name__ == '__main__':
    unittest.main() 