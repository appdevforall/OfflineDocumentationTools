import os
import unittest
import tempfile
import shutil
from scripts.validator import HTMLValidator

class TestHTMLValidator(unittest.TestCase):
    def setUp(self):
        """Set up test environment before each test."""
        self.validator = HTMLValidator()
        # Create a temporary directory
        self.test_dir = tempfile.mkdtemp(prefix='html_validator_test_')

    def tearDown(self):
        """Clean up test environment after each test."""
        # Remove the temporary directory and all its contents
        shutil.rmtree(self.test_dir)

    def test_valid_doc1(self):
        """Test a simple valid HTML document with basic structure."""
        # Create test file
        file_path = os.path.join(self.test_dir, 'valid_doc1.html')
        content = "<html><head><title>Test</title></head><body><h1>Hello</h1><p>This is a paragraph.</p></body></html>"
        with open(file_path, 'w', encoding='utf-8') as f:
            f.write(content)

        # Test both direct content validation and file validation
        self.assertTrue(self.validator.validate_html_content(content))
        self.assertTrue(self.validator.check_html_file(file_path))

    def test_valid_doc2(self):
        """Test a valid HTML document with DOCTYPE declaration."""
        # Create test file
        file_path = os.path.join(self.test_dir, 'valid_doc2.html')
        content = "<!DOCTYPE html><html><body><div><span>Text</span></div></body></html>"
        with open(file_path, 'w', encoding='utf-8') as f:
            f.write(content)

        # Test both direct content validation and file validation
        self.assertTrue(self.validator.validate_html_content(content))
        self.assertTrue(self.validator.check_html_file(file_path))

    def test_broken_doc1(self):
        """Test HTML with intermingled tags."""
        # Create test file
        file_path = os.path.join(self.test_dir, 'broken_doc1.html')
        content = "<html><body><tag1><tag2></tag1></tag2></body></html>"
        with open(file_path, 'w', encoding='utf-8') as f:
            f.write(content)

        # Test both direct content validation and file validation
        self.assertFalse(self.validator.validate_html_content(content))
        self.assertFalse(self.validator.check_html_file(file_path))

    def test_broken_doc2(self):
        """Test HTML with complex intermingled tags."""
        # Create test file
        file_path = os.path.join(self.test_dir, 'broken_doc2.html')
        content = "<html><body><div><p><span>Hello</div></p></span></body></html>"
        with open(file_path, 'w', encoding='utf-8') as f:
            f.write(content)

        # Test both direct content validation and file validation
        self.assertFalse(self.validator.validate_html_content(content))
        self.assertFalse(self.validator.check_html_file(file_path))

    def test_malformed_doc1(self):
        """Test HTML with unclosed tags."""
        # Create test file
        file_path = os.path.join(self.test_dir, 'malformed_doc1.html')
        content = "<html><body><div><span>Hello"  # Unclosed tags
        with open(file_path, 'w', encoding='utf-8') as f:
            f.write(content)

        # Test both direct content validation and file validation
        self.assertFalse(self.validator.validate_html_content(content))
        self.assertFalse(self.validator.check_html_file(file_path))

    def test_malformed_doc2(self):
        """Test HTML with missing closing tag."""
        # Create test file
        file_path = os.path.join(self.test_dir, 'malformed_doc2.html')
        content = "<html><body><p>This is a paragraph.</body></html>"  # Missing closing p tag
        with open(file_path, 'w', encoding='utf-8') as f:
            f.write(content)

        # Test both direct content validation and file validation
        self.assertFalse(self.validator.validate_html_content(content))
        self.assertFalse(self.validator.check_html_file(file_path))

    def test_nonexistent_file(self):
        """Test handling of non-existent file."""
        self.assertFalse(self.validator.check_html_file('nonexistent.html'))

    def test_scan_html_directory(self):
        """Test directory scanning functionality."""
        # Create all test files
        test_files = {
            'valid_doc1.html': "<html><head><title>Test</title></head><body><h1>Hello</h1><p>This is a paragraph.</p></body></html>",
            'valid_doc2.html': "<!DOCTYPE html><html><body><div><span>Text</span></div></body></html>",
            'broken_doc1.html': "<html><body><tag1><tag2></tag1></tag2></body></html>",
            'broken_doc2.html': "<html><body><div><p><span>Hello</div></p></span></body></html>",
            'malformed_doc1.html': "<html><body><div><span>Hello",
            'malformed_doc2.html': "<html><body><p>This is a paragraph.</body></html>"
        }

        for filename, content in test_files.items():
            with open(os.path.join(self.test_dir, filename), 'w', encoding='utf-8') as f:
                f.write(content)

        valid_files, broken_files = self.validator.scan_html_directory(self.test_dir)
        
        # Check that we found the expected number of files
        self.assertEqual(len(valid_files), 2)  # valid_doc1.html and valid_doc2.html
        self.assertEqual(len(broken_files), 4)  # broken_doc1.html, broken_doc2.html, malformed_doc1.html, malformed_doc2.html

        # Verify specific files are in the correct lists
        self.assertIn(os.path.join(self.test_dir, 'valid_doc1.html'), valid_files)
        self.assertIn(os.path.join(self.test_dir, 'broken_doc1.html'), broken_files)

if __name__ == '__main__':
    unittest.main() 