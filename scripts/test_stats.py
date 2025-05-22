import unittest
from DocumentationDatabase import DocumentationDatabase
import os
import sqlite3

class TestStats(unittest.TestCase):
    def setUp(self):
        self.db_path = '/tmp/stats-test.sqlite'
        # Ensure a clean database for each test
        if os.path.exists(self.db_path):
            os.remove(self.db_path)
        self.db = DocumentationDatabase(self.db_path)

    def tearDown(self):
        if os.path.exists(self.db_path):
            os.remove(self.db_path)

    def test_new_database_has_zero_files(self):
        """Test that a new database contains zero files."""
        count, input_bytes, stored_bytes = self.db.stats()
        self.assertEqual(count, 0)
        self.assertEqual(input_bytes, 0)
        self.assertEqual(stored_bytes, 0)

    def test_stats_after_inserting_text_file(self):
        """Test that stats() returns correct values after inserting a text file."""
        # Create a larger text file with repeated content to ensure compression works
        content = b'Hello, World! ' * 100  # Repeat the text 100 times
        with open('/tmp/tiny.txt', 'wb') as f:
            f.write(content)
        with open('/tmp/tiny.txt', 'rb') as f:
            content = f.read()
        self.db.add_file('/tmp/tiny.txt', content, 'en-US')
        count, input_bytes, stored_bytes = self.db.stats()
        self.assertEqual(count, 1)
        self.assertEqual(input_bytes, len(content))
        self.assertGreater(stored_bytes, 0)
        self.assertLess(stored_bytes, input_bytes)  # Verify compression worked
        os.remove('/tmp/tiny.txt')

    def test_stats_after_inserting_1000_files(self):
        """Test that stats() returns correct values after inserting 1000 files."""
        num_files = 1000
        content = b'Hello, World! ' * 10  # 140 bytes per file
        total_input_bytes = 0
        for i in range(num_files):
            file_path = f'/tmp/file_{i}.txt'
            with open(file_path, 'wb') as f:
                f.write(content)
            with open(file_path, 'rb') as f:
                file_content = f.read()
            self.db.add_file(file_path, file_content, 'en-US')
            total_input_bytes += len(file_content)
            os.remove(file_path)
        count, input_bytes, stored_bytes = self.db.stats()
        self.assertEqual(count, num_files)
        self.assertEqual(input_bytes, total_input_bytes)
        self.assertGreater(stored_bytes, 0)
        self.assertLess(stored_bytes, input_bytes)  # Verify compression worked

    def test_stored_bytes_matches_database(self):
        """Test that stored_bytes in the class matches the sum of bytes in the Content table."""
        num_files = 10
        content = b'Hello, World! ' * 10
        for i in range(num_files):
            file_path = f'/tmp/file_db_{i}.txt'
            with open(file_path, 'wb') as f:
                f.write(content)
            with open(file_path, 'rb') as f:
                file_content = f.read()
            self.db.add_file(file_path, file_content, 'en-US')
            os.remove(file_path)
        # Get stored_bytes from the class
        _, _, stored_bytes_class = self.db.stats()
        # Get sum of content lengths from the database
        with sqlite3.connect(self.db_path) as conn:
            cursor = conn.cursor()
            cursor.execute("SELECT SUM(LENGTH(content)) FROM Content")
            stored_bytes_db = cursor.fetchone()[0]
        self.assertEqual(stored_bytes_class, stored_bytes_db)

if __name__ == '__main__':
    unittest.main() 