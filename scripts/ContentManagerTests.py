# ContentManagerTests.py

import unittest
import os
import shutil
import sqlite3
import brotli
import hashlib
import logging
import sys

# We will assume the ContentManager class is available from the script we have been working on.
# For this to work, save the content manager script as ContentManager.py in the same directory.
from ContentManager import ContentManager, setup_logging, content_manager_logger

# Test directories and files
LOG_DIR = "ContentManagerTesting"
RESULTS_DIR = LOG_DIR + "_Results"
LOG_FILE = os.path.join(RESULTS_DIR, "log.txt")

# Create the necessary directories and configure the logger
os.makedirs(RESULTS_DIR, exist_ok=True)

# Configure the logger to write to the single log file and stdout
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(levelname)s - %(message)s',
    handlers=[
        logging.FileHandler(LOG_FILE, mode='w'),
        logging.StreamHandler(sys.stdout)
    ]
)


class TestContentManager(unittest.TestCase):
    """
    Tests the functionality of the ContentManager class.
    """

    def setUp(self):
        """
        Sets up a temporary directory and a reference database for testing.
        """
        self.test_dir = LOG_DIR
        self.reference_db_path = os.path.join(self.test_dir, 'reference_db.sqlite')
        self.hashes_file_path = os.path.join(self.test_dir, 'hashes_dump.tsv')
        self.dump_output_dir = os.path.join(self.test_dir, 'dump_output')

        # Clear test_dir before each test to ensure a clean slate
        if os.path.exists(self.test_dir):
            shutil.rmtree(self.test_dir)
        os.makedirs(self.test_dir)

        # Create a reference database
        conn = sqlite3.connect(self.reference_db_path)
        cursor = conn.cursor()

        # Create necessary tables
        cursor.execute("""
            CREATE TABLE ContentTypes (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                value TEXT NOT NULL UNIQUE,
                compression TEXT NOT NULL
            )
        """)
        cursor.execute("INSERT INTO ContentTypes (value, compression) VALUES ('html', 'brotli')")
        cursor.execute("INSERT INTO ContentTypes (value, compression) VALUES ('md', 'none')")
        cursor.execute("INSERT INTO ContentTypes (value, compression) VALUES ('txt', 'brotli')")
        cursor.execute("""
            CREATE TABLE Content (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                path TEXT NOT NULL,
                languageID INTEGER NOT NULL,
                content BLOB NOT NULL,
                contentTypeID INTEGER NOT NULL,
                FOREIGN KEY (contentTypeID) REFERENCES ContentTypes(id)
            )
        """)

        # Define some sample content
        self.html_content = b'<html><body><h1>Hello World</h1></body></html>'
        self.md_content = b'# Markdown Test\n\nThis is a test file.'
        self.text_content = b'Just a simple text file.'

        # Define file paths and compression
        self.file1_path = 'docs/pages/intro.html'
        self.file2_path = 'assets/img/logo.md'
        self.file3_path = 'README.txt'

        # Compress content as needed
        compressed_html = brotli.compress(self.html_content)
        compressed_text = brotli.compress(self.text_content)

        # Insert content into the database
        cursor.execute("INSERT INTO Content (path, languageID, content, contentTypeID) VALUES (?, 1, ?, 1)",
                       (self.file1_path, compressed_html))
        cursor.execute("INSERT INTO Content (path, languageID, content, contentTypeID) VALUES (?, 1, ?, 2)",
                       (self.file2_path, self.md_content))
        cursor.execute("INSERT INTO Content (path, languageID, content, contentTypeID) VALUES (?, 1, ?, 3)",
                       (self.file3_path, compressed_text))

        conn.commit()
        conn.close()

    def tearDown(self):
        """
        Copies the contents of the test directory to a new directory named after the test.
        """
        test_name = self.id().split('.')[-1]
        test_results_dir = os.path.join(RESULTS_DIR, test_name)

        logging.info("Archiving test results for '%s' to '%s'...", test_name, test_results_dir)

        # Remove existing results directory to ensure a clean copy
        if os.path.exists(test_results_dir):
            shutil.rmtree(test_results_dir)

        # Copy the entire test directory contents to the results directory
        if os.path.exists(self.test_dir):
            shutil.copytree(self.test_dir, test_results_dir)

        # Log that the test directory has been cleared (which happens in setUp)
        logging.info("Test directory cleared for next run.")

    def test_dump_operations(self):
        """
        Tests dump_content() and dump_one() by verifying output against a reference.
        """
        logging.info("--- Starting dump operations tests ---")

        # Test dump_content()
        dump_manager = ContentManager(
            input_db_path=self.reference_db_path,
            output_dir=self.dump_output_dir,
            hashes_file_path=self.hashes_file_path
        )
        dump_manager.dump_content()

        self.assertTrue(os.path.exists(self.dump_output_dir))

        # Verify extracted files
        extracted_html_path = os.path.join(self.dump_output_dir, self.file1_path)
        extracted_md_path = os.path.join(self.dump_output_dir, self.file2_path)
        extracted_text_path = os.path.join(self.dump_output_dir, self.file3_path)

        self.assertTrue(os.path.exists(extracted_html_path))
        self.assertTrue(os.path.exists(extracted_md_path))
        self.assertTrue(os.path.exists(extracted_text_path))

        with open(extracted_html_path, 'rb') as f:
            self.assertEqual(f.read(), self.html_content)
        with open(extracted_md_path, 'rb') as f:
            self.assertEqual(f.read(), self.md_content)
        with open(extracted_text_path, 'rb') as f:
            self.assertEqual(f.read(), self.text_content)

        # Verify hashes file
        self.assertTrue(os.path.exists(self.hashes_file_path))
        with open(self.hashes_file_path, 'r', encoding='utf-8') as f:
            lines = f.readlines()
            self.assertEqual(len(lines), 3)

            # Recompute hashes to verify
            hash1 = hashlib.sha256(self.html_content).hexdigest()
            hash2 = hashlib.sha256(self.md_content).hexdigest()
            hash3 = hashlib.sha256(self.text_content).hexdigest()

            self.assertIn(f"{self.file1_path}\t{hash1}\n", lines)
            self.assertIn(f"{self.file2_path}\t{hash2}\n", lines)
            self.assertIn(f"{self.file3_path}\t{hash3}\n", lines)

        logging.info("Dump content test passed.")

        # Test dump_one()
        dump_one_output_dir = os.path.join(self.test_dir, 'dump_one_output')
        dump_one_manager = ContentManager(input_db_path=self.reference_db_path, output_dir=dump_one_output_dir)
        dump_one_manager.dump_one(self.file1_path)

        self.assertTrue(os.path.exists(dump_one_output_dir))
        extracted_file_path = os.path.join(dump_one_output_dir, self.file1_path)
        self.assertTrue(os.path.exists(extracted_file_path))

        with open(extracted_file_path, 'rb') as f:
            self.assertEqual(f.read(), self.html_content)

        logging.info("Dump one test passed.")
        logging.info("--- Finished dump operations tests ---")

    def test_build_single_changes(self):
        """
        Tests build_database() with a single update, deletion, and insertion.
        """
        logging.info("--- Starting build test with single changes ---")

        # Setup initial state by dumping the reference database
        build_dir = os.path.join(self.test_dir, 'build_source_single')
        os.makedirs(build_dir)

        initial_hashes_path = os.path.join(self.test_dir, 'hashes_build_single.tsv')
        initial_db_path = os.path.join(self.test_dir, 'initial_build_db_single.sqlite')
        shutil.copy2(self.reference_db_path, initial_db_path)

        initial_manager = ContentManager(
            input_db_path=initial_db_path,
            output_dir=build_dir,
            hashes_file_path=initial_hashes_path
        )
        initial_manager.dump_content()

        # Make single changes to the local files
        # 1. Modify an existing file (md_content)
        modified_md_content = b'# Updated Markdown\n\nThis file has been changed!'
        with open(os.path.join(build_dir, self.file2_path), 'wb') as f:
            f.write(modified_md_content)

        # 2. Delete a file
        os.remove(os.path.join(build_dir, self.file3_path))

        # 3. Add a new file
        new_file_path = 'new/file/added.html'
        new_file_content = b'<p>This is a brand new file.</p>'
        new_file_full_path = os.path.join(build_dir, new_file_path)
        os.makedirs(os.path.dirname(new_file_full_path), exist_ok=True)
        with open(new_file_full_path, 'wb') as f:
            f.write(new_file_content)

        # Run build_database()
        output_db_path = os.path.join(self.test_dir, 'output_db_single.sqlite')
        build_manager = ContentManager(
            input_db_path=initial_db_path,
            output_db_path=output_db_path,
            input_dir=build_dir,
            hashes_file_path=initial_hashes_path
        )
        build_manager.build_database()

        # Verify the new database
        self.assertTrue(os.path.exists(output_db_path))
        conn = sqlite3.connect(output_db_path)
        cursor = conn.cursor()

        # Verify deletion
        cursor.execute("SELECT path FROM Content WHERE path = ?", (self.file3_path,))
        self.assertIsNone(cursor.fetchone())

        # Verify modification
        cursor.execute("SELECT content FROM Content WHERE path = ?", (self.file2_path,))
        fetched_content = cursor.fetchone()[0]
        self.assertEqual(fetched_content, modified_md_content)

        # Verify insertion
        cursor.execute("SELECT content, contentTypeID FROM Content WHERE path = ?", (new_file_path,))
        fetched_content, fetched_type_id = cursor.fetchone()

        # We need to verify that it was compressed correctly
        expected_compressed_content = brotli.compress(new_file_content, quality=11)
        self.assertEqual(fetched_content, expected_compressed_content)

        # Also verify content type (html is ID 1 in our setup)
        self.assertEqual(fetched_type_id, 1)

        conn.close()
        logging.info("Build test with single changes passed.")
        logging.info("--- Finished build test with single changes ---")

    def test_build_multiple_changes(self):
        """
        Tests build_database() with multiple updates, deletions, and insertions.
        """
        logging.info("--- Starting build test with multiple changes ---")

        # Create a temporary database with enough files for the multiple test case
        temp_db_path = os.path.join(self.test_dir, 'temp_multi_test_db.sqlite')
        shutil.copy2(self.reference_db_path, temp_db_path)
        conn = sqlite3.connect(temp_db_path)
        cursor = conn.cursor()

        # Add more files to the temporary database
        cursor.execute("INSERT INTO ContentTypes (value, compression) VALUES ('js', 'brotli')")
        cursor.execute("INSERT INTO Content (path, languageID, content, contentTypeID) VALUES (?, 1, ?, 1)",
                       ('file4.html', brotli.compress(b'Other Content 1')))
        cursor.execute("INSERT INTO Content (path, languageID, content, contentTypeID) VALUES (?, 1, ?, 2)",
                       ('file5.md', b'Other Content 2'))
        cursor.execute("INSERT INTO Content (path, languageID, content, contentTypeID) VALUES (?, 1, ?, 3)",
                       ('file6.txt', brotli.compress(b'Other Content 3')))
        cursor.execute("INSERT INTO Content (path, languageID, content, contentTypeID) VALUES (?, 1, ?, 4)",
                       ('file7.js', brotli.compress(b'Other Content 4')))
        conn.commit()
        conn.close()

        # Define file paths for the new files
        file1_path = 'docs/pages/intro.html'
        file2_path = 'assets/img/logo.md'
        file3_path = 'README.txt'
        file4_path = 'file4.html'
        file5_path = 'file5.md'
        file6_path = 'file6.txt'
        file7_path = 'file7.js'
        new_file_1_path = 'new/file/added1.html'
        new_file_2_path = 'more/files/added2.js'

        build_dir = os.path.join(self.test_dir, 'build_source_multi')
        os.makedirs(build_dir)

        # Dump the new, larger database to the build directory
        initial_hashes_path_multi = os.path.join(self.test_dir, 'hashes_build_multi.tsv')
        initial_manager_multi = ContentManager(
            input_db_path=temp_db_path,
            output_dir=build_dir,
            hashes_file_path=initial_hashes_path_multi
        )
        initial_manager_multi.dump_content()

        # --- Test Logic: 2 updates, 2 deletions, 2 insertions ---

        # 1. Two Updates
        modified_html_content = b'<html><body><h1>Updated HTML</h1></body></html>'
        modified_md_content = b'# Multiple Markdown Change\nThis file has also been changed!'
        with open(os.path.join(build_dir, file1_path), 'wb') as f:
            f.write(modified_html_content)
        with open(os.path.join(build_dir, file2_path), 'wb') as f:
            f.write(modified_md_content)

        # 2. Two Deletions
        print("Removing " + os.path.join(build_dir, file3_path))
        os.remove(os.path.join(build_dir, file3_path))
        os.remove(os.path.join(build_dir, file4_path))

        # 3. Two Insertions
        new_file_1_content = b'<p>First new file.</p>'
        new_file_2_content = b'console.log("Second new file.");'

        new_file_1_full_path = os.path.join(build_dir, new_file_1_path)
        os.makedirs(os.path.dirname(new_file_1_full_path), exist_ok=True)
        with open(new_file_1_full_path, 'wb') as f:
            f.write(new_file_1_content)

        new_file_2_full_path = os.path.join(build_dir, new_file_2_path)
        os.makedirs(os.path.dirname(new_file_2_full_path), exist_ok=True)
        with open(new_file_2_full_path, 'wb') as f:
            f.write(new_file_2_content)

        # Run build_database()
        output_db_path = os.path.join(self.test_dir, 'output_db_multi.sqlite')
        build_manager = ContentManager(
            input_db_path=temp_db_path,
            output_db_path=output_db_path,
            input_dir=build_dir,
            hashes_file_path=initial_hashes_path_multi
        )
        build_manager.build_database()

        # Verify the new database
        self.assertTrue(os.path.exists(output_db_path))
        conn = sqlite3.connect(output_db_path)
        cursor = conn.cursor()

        # Verify deletions
        cursor.execute("SELECT path FROM Content WHERE path IN (?, ?)", (file3_path, file4_path))
        deleted_records = cursor.fetchall()
        self.assertEqual(len(deleted_records), 0, f"Expected 0 deleted records, found {len(deleted_records)}")

        # Verify modifications
        cursor.execute("SELECT content FROM Content WHERE path = ?", (file1_path,))
        fetched_content = cursor.fetchone()[0]
        self.assertEqual(fetched_content, brotli.compress(modified_html_content, quality=11))

        cursor.execute("SELECT content FROM Content WHERE path = ?", (file2_path,))
        fetched_content = cursor.fetchone()[0]
        self.assertEqual(fetched_content, modified_md_content)

        # Verify insertions
        cursor.execute("SELECT content, contentTypeID FROM Content WHERE path = ?", (new_file_1_path,))
        fetched_content_1, fetched_type_id_1 = cursor.fetchone()
        self.assertEqual(fetched_content_1, brotli.compress(new_file_1_content, quality=11))
        self.assertEqual(fetched_type_id_1, 1)

        cursor.execute("SELECT content, contentTypeID FROM Content WHERE path = ?", (new_file_2_path,))
        fetched_content_2, fetched_type_id_2 = cursor.fetchone()
        self.assertEqual(fetched_content_2, brotli.compress(new_file_2_content, quality=11))
        self.assertEqual(fetched_type_id_2, 1)

        conn.close()
        logging.info("Build test with multiple changes passed.")
        logging.info("--- Finished build test with multiple changes ---")
