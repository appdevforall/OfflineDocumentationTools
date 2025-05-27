import unittest
from unittest.mock import patch, mock_open
import argparse
import tempfile
import os
import sqlite3
from ingest import main, DocumentationDatabase

class DummyPool:
    def __enter__(self): return self
    def __exit__(self, exc_type, exc_val, exc_tb): pass
    def starmap(self, func, iterable):
        return [func(*args) for args in iterable]

class TestIngest(unittest.TestCase):
    def setUp(self):
        # Create a temporary database file for each test
        self.temp_db = tempfile.NamedTemporaryFile(delete=False)
        self.temp_db.close()
        self.db_path = self.temp_db.name
        
        # Clean up any existing database
        if os.path.exists(self.db_path):
            os.unlink(self.db_path)
        
        # Initialize the database with the required schema
        self.db = DocumentationDatabase(self.db_path)

    def tearDown(self):
        # Clean up the temporary database file
        os.unlink(self.db_path)

    @patch('argparse.ArgumentParser.parse_args')
    @patch('builtins.open', new_callable=mock_open, read_data=b'Test content')
    @patch('os.path.isfile', return_value=True)
    @patch('ingest.DocumentationDatabase')
    def test_single_file_ingestion(self, mock_db, mock_isfile, mock_file, mock_args):
        mock_args.return_value = argparse.Namespace(file='test.txt', directory=None, path_to_db=self.db_path)
        mock_db.return_value = self.db
        self.db.emit_summary(label="test_single_file_ingestion before")
        main()
        self.db.emit_summary(label="test_single_file_ingestion after")

    @patch('multiprocessing.Pool', new=DummyPool)
    @patch('argparse.ArgumentParser.parse_args')
    @patch('os.listdir')
    @patch('builtins.open')
    @patch('os.path.isfile', return_value=True)
    @patch('ingest.DocumentationDatabase')
    def test_directory_ingestion(self, mock_db, mock_isfile, mock_file, mock_listdir, mock_args):
        mock_args.return_value = argparse.Namespace(file=None, directory='test_dir', path_to_db=self.db_path)
        mock_listdir.return_value = ['file1.txt', 'file2.txt', 'file3.md', 'image1.jpg', 'image2.png', 'image3.gif']
        mock_db.return_value = self.db

        # Create a mock file that returns different content based on the file being opened
        def mock_file_side_effect(*args, **kwargs):
            filename = args[0]
            if isinstance(filename, int):
                # Allow file descriptors to pass through to the real open
                return open(filename, *args[1:], **kwargs)
            if filename.endswith('.txt'):
                return mock_open(read_data=b'Text content')(*args, **kwargs)
            elif filename.endswith('.md'):
                return mock_open(read_data=b'# Markdown content')(*args, **kwargs)
            elif filename.endswith('.jpg'):
                return mock_open(read_data=b'\xFF\xD8\xFF\xE0\x00\x10JFIF\x00\x01\x01\x01\x00H\x00H\x00\x00\xFF\xDB\x00C\x00')(*args, **kwargs)
            elif filename.endswith('.png'):
                return mock_open(read_data=b'\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDR\x00\x00\x00\x01\x00\x00\x00\x01\x08\x06\x00\x00\x00\x1f\x15\xc4\x89\x00\x00\x00\nIDATx\x9cc\x00\x01\x00\x00\x05\x00\x01\r\n-\xb4\x00\x00\x00\x00IEND\xaeB`\x82')(*args, **kwargs)
            elif filename.endswith('.gif'):
                return mock_open(read_data=b'GIF87a\x01\x00\x01\x00\x80\x00\x00\xff\xff\xff\x00\x00\x00!\xf9\x04\x01\x00\x00\x00\x00,\x00\x00\x00\x00\x01\x00\x01\x00\x00\x02\x02D\x01\x00;')(*args, **kwargs)
            else:
                return mock_open(read_data=b'Unknown content')(*args, **kwargs)

        mock_file.side_effect = mock_file_side_effect

        self.db.emit_summary(label="test_directory_ingestion before")
        main()
        self.db.emit_summary(label="test_directory_ingestion after")

    @patch('builtins.open', new_callable=mock_open, read_data=b'Test content')
    @patch('os.path.isfile', return_value=True)
    def test_relative_path_handling(self, mock_isfile, mock_file):
        from ingest import process_file
        
        # Test various relative path patterns
        test_cases = [
            ('../../a/b/c.txt', 'a/b/c.txt'),
            ('../a/b/c.txt', 'a/b/c.txt'),
            ('./a/b/c.txt', 'a/b/c.txt'),
            ('a/b/c.txt', 'a/b/c.txt'),  # No change needed
            ('/a/b/c.txt', '/a/b/c.txt'),  # Absolute path, no change
        ]
        
        for input_path, expected_path in test_cases:
            with self.subTest(input_path=input_path):
                # Create a fresh database for each subtest
                temp_db = tempfile.NamedTemporaryFile(delete=False)
                temp_db.close()
                db_path = temp_db.name
                
                try:
                    # Process the file
                    process_file(input_path, db_path)
                    
                    # Verify the path was normalized correctly in the database
                    with sqlite3.connect(db_path) as conn:
                        cursor = conn.cursor()
                        cursor.execute("SELECT path FROM Content WHERE path = ?", (expected_path,))
                        result = cursor.fetchone()
                        self.assertIsNotNone(result, f"Path {expected_path} not found in database")
                        self.assertEqual(result[0], expected_path,
                                       f"Path not normalized correctly. Expected {expected_path}, got {result[0]}")
                finally:
                    # Clean up the temporary database
                    if os.path.exists(db_path):
                        os.unlink(db_path)

if __name__ == '__main__':
    unittest.main() 