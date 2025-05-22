import unittest
import os
import tempfile
import sqlite3
from DocumentationDatabase import DocumentationDatabase

class TestDocumentationDatabase(unittest.TestCase):
    def setUp(self):
        self.temp_db_file = tempfile.NamedTemporaryFile(delete=False)
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

    def test_content_types_populated(self):
        # Check if the ContentTypes table contains all expected types
        with sqlite3.connect(self.temp_db_file.name) as connection:
            cursor = connection.cursor()
            cursor.execute("SELECT value FROM ContentTypes;")
            content_types = {row[0] for row in cursor.fetchall()}
            for mime_type in DocumentationDatabase.CONTENT_TYPES:
                self.assertIn(mime_type, content_types)

    def test_languages_populated(self):
        # Check if the Languages table contains 'en-US'
        with sqlite3.connect(self.temp_db_file.name) as connection:
            cursor = connection.cursor()
            cursor.execute("SELECT value FROM Languages;")
            languages = {row[0] for row in cursor.fetchall()}
            self.assertIn('en-US', languages)

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
        # Create a valid DB with the correct schema and content
        with tempfile.NamedTemporaryFile(delete=False) as valid_db_file:
            valid_db_file.close()
            # Use the class's methods to create and populate the schema
            db = DocumentationDatabase(valid_db_file.name)
            # Should NOT raise ValueError
            try:
                with sqlite3.connect(valid_db_file.name) as connection:
                    cursor = connection.cursor()
                    cursor.execute("SELECT name FROM sqlite_master WHERE type='table';")
                    tables = cursor.fetchall()
                    expected_tables = {'ide_tooltip_table', 'Content', 'Languages', 'ContentTypes'}
                    existing_tables = {table[0] for table in tables if not table[0].startswith('sqlite_')}
                    self.assertEqual(existing_tables, expected_tables)
                # Instantiate DocumentationDatabase a second time to verify it accepts a preexisting valid database
                db2 = DocumentationDatabase(valid_db_file.name)
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