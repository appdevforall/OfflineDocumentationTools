import os
import json
import sqlite3
import tempfile
import unittest
from http.client import HTTPConnection
from myServer import MyServer, HTTPServer, start_server
import threading
import time
import shutil

class TestDocumentationSystem(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        # Create temporary files for testing
        cls.temp_dir = tempfile.mkdtemp()
        cls.db_path = os.path.join(cls.temp_dir, "Documentation.db")
        
        # Create test files
        cls.files_list = os.path.join(cls.temp_dir, "files")
        cls.dirs_list = os.path.join(cls.temp_dir, "directories")
        
        with open(cls.files_list, "w") as f:
            f.write("./test.txt\n")
            f.write("./test.md\n")
            f.write("./test.html\n")
        
        with open(cls.dirs_list, "w") as f:
            f.write(".\n")
            f.write("./docs\n")
        
        # Create test content
        cls.content_dir = os.path.join(cls.temp_dir, "content")
        if os.path.exists(cls.content_dir):
            shutil.rmtree(cls.content_dir)
        os.makedirs(cls.content_dir)
        os.makedirs(os.path.join(cls.content_dir, "docs"))
        
        with open(os.path.join(cls.content_dir, "test.txt"), "w") as f:
            f.write("Hello, World!")
        
        with open(os.path.join(cls.content_dir, "test.md"), "w") as f:
            f.write("# Test Markdown\n\nThis is a test.")
        
        with open(os.path.join(cls.content_dir, "test.html"), "w") as f:
            f.write("<html><body>Test HTML</body></html>")
        
        # Create test tooltips
        cls.tooltips_file = os.path.join(cls.temp_dir, "CoGoTooltips.json")
        with open(cls.tooltips_file, "w") as f:
            json.dump([{
                "tag": "test-tag",
                "category": "test-category",
                "summary": "Test Summary",
                "detail": "Test Detail",
                "buttons": [["Learn More", "http://example.com"]]
            }], f)

    def test_1_database_creation(self):
        """Test that the database is created with correct schema"""
        # Modify the database filename in create_empty_database.py
        import create_empty_database
        create_empty_database.DATABASE_FILENAME = self.db_path
        create_empty_database.LIST_OF_FILENAMES = self.files_list
        
        # Run database creation
        with open(self.files_list) as fd:
            files = [i.strip() for i in fd]
        exts = create_empty_database.get_exts(files)
        
        with sqlite3.connect(self.db_path) as connection:
            cursor = connection.cursor()
            create_empty_database.write_create_tables(cursor)
            create_empty_database.write_filetypes_content(cursor, exts)
            create_empty_database.write_languages(cursor)
        
        # Verify tables exist
        with sqlite3.connect(self.db_path) as conn:
            cursor = conn.cursor()
            tables = cursor.execute("SELECT name FROM sqlite_master WHERE type='table';").fetchall()
            table_names = [t[0] for t in tables]
            self.assertIn('ide_tooltip_table', table_names)
            self.assertIn('Content', table_names)
            self.assertIn('Languages', table_names)
            self.assertIn('ContentTypes', table_names)

    def test_2_content_insertion(self):
        """Test that content can be inserted and retrieved"""
        # Modify paths in put_content_in_database.py
        import put_content_in_database
        put_content_in_database.DATABASE_FILENAME = self.db_path
        put_content_in_database.LIST_OF_FILES = self.files_list
        put_content_in_database.LIST_OF_DIRECTORIES = self.dirs_list
        
        # Set up test files in current directory
        os.chdir(self.temp_dir)
        for f in ["test.txt", "test.md", "test.html"]:
            src = os.path.join(self.content_dir, f)
            dst = os.path.join(self.temp_dir, f)
            shutil.copy2(src, dst)
        
        # Create a fresh clone directory path (but don't create it)
        clone_dir = os.path.join(self.temp_dir, "clone_content")
        put_content_in_database.CLONE_INTO_DIR = clone_dir
        
        # Run content insertion
        with sqlite3.connect(self.db_path) as connection:
            put_content_in_database.main(connection.cursor())
        
        # Verify content was inserted
        with sqlite3.connect(self.db_path) as conn:
            cursor = conn.cursor()
            content = cursor.execute("SELECT path, content FROM Content WHERE path='test.txt'").fetchone()
            self.assertIsNotNone(content)
            self.assertEqual(content[0], 'test.txt')
            # Content should be compressed, so we can't directly compare it

    def test_3_server_functionality(self):
        """Test that the server can serve content"""
        # Start server in a separate thread
        import myServer
        myServer.DATABASE_FILENAME = self.db_path
        myServer.HOST_NAME = "localhost"
        myServer.SERVER_PORT = 8080
        
        # Create a connection in the main thread that will be used by the server
        connection = sqlite3.connect(self.db_path, check_same_thread=False)
        cursor = connection.cursor()
        
        server = HTTPServer((myServer.HOST_NAME, myServer.SERVER_PORT), MyServer)
        MyServer.cursor = cursor
        
        server_thread = threading.Thread(target=server.serve_forever)
        server_thread.daemon = True
        server_thread.start()
        
        # Give server time to start
        time.sleep(1)
        
        try:
            # Test server response
            conn = HTTPConnection("localhost", 8080)
            conn.request("GET", "/test.txt")
            response = conn.getresponse()
            response.read()  # Read the response to clear the connection
            
            self.assertEqual(response.status, 200)
            self.assertIn("text/plain", response.getheader("Content-type"))
            
            # Test language handling
            conn = HTTPConnection("localhost", 8080)  # Create new connection
            conn.request("GET", "/test.txt", headers={"Accept-Language": "en-US"})
            response = conn.getresponse()
            response.read()  # Read the response to clear the connection
            
            self.assertEqual(response.status, 200)
            
        finally:
            server.shutdown()
            server.server_close()
            connection.close()

    @classmethod
    def tearDownClass(cls):
        # Clean up temporary files
        shutil.rmtree(cls.temp_dir)

if __name__ == '__main__':
    unittest.main() 