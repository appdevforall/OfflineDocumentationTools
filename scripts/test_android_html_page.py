import unittest
import tempfile
import os

class TestAndroidHtmlPage(unittest.TestCase):
    def test_initialize_with_file_path(self):
        """Test that android_html_page can be initialized with a file path"""
        with tempfile.TemporaryDirectory() as temp_dir:
            # Create a sample HTML file
            html_content = """
            <html>
            <head><title>Test Class</title></head>
            <body>
                <div class="jd-details-descr">
                    <p>This is a test description.</p>
                </div>
            </body>
            </html>
            """
            
            # Write the content to a temporary file
            file_path = os.path.join(temp_dir, 'test_class.html')
            with open(file_path, 'w') as f:
                f.write(html_content)
            
            # Import and initialize the class
            from android_html_page import AndroidHtmlPage
            
            # Test initialization with existing file
            page = AndroidHtmlPage(file_path)
            self.assertEqual(page.file_path, file_path)
            self.assertTrue(os.path.exists(page.file_path))
            
            # Test initialization with non-existent file
            non_existent_path = os.path.join(temp_dir, 'non_existent.html')
            page2 = AndroidHtmlPage(non_existent_path)
            self.assertEqual(page2.file_path, non_existent_path)
            self.assertFalse(os.path.exists(page2.file_path))

    def test_extract_article_text(self):
        """Test that android_html_page can extract text from content between sentinel comments"""
        with tempfile.TemporaryDirectory() as temp_dir:
            # Create a sample HTML file with sentinel comments
            html_content = """
            <html>
            <head><title>Test Class</title></head>
            <body>
                <header>Some header content</header>
                <article class="devsite-article">
                    <h1>Main Title</h1>
                    <p>This is content before the sentinels.</p>
                    
                    <!-- ======== START OF CLASS DATA ======== -->
                    <p>This is the main content inside the sentinels.</p>
                    <p>This is another paragraph with <strong>bold text</strong> and <em>italic text</em>.</p>
                    <div>
                        <h2>Subsection</h2>
                        <p>More content here.</p>
                    </div>
                    <!-- ========= END OF CLASS DATA ========= -->
                    
                    <p>This is content after the sentinels.</p>
                </article>
                <footer>Some footer content</footer>
            </body>
            </html>
            """
            
            # Write the content to a temporary file
            file_path = os.path.join(temp_dir, 'test_article.html')
            with open(file_path, 'w') as f:
                f.write(html_content)
            
            # Import and initialize the class
            from android_html_page import AndroidHtmlPage
            
            # Test article text extraction
            page = AndroidHtmlPage(file_path)
            article_text = page.get_article_text()
            
            # Verify the extracted text contains the expected content between sentinels
            self.assertIn("This is the main content inside the sentinels", article_text)
            self.assertIn("This is another paragraph with bold text and italic text", article_text)
            self.assertIn("Subsection", article_text)
            self.assertIn("More content here", article_text)
            
            # Verify content outside sentinels is NOT included
            self.assertNotIn("Main Title", article_text)
            self.assertNotIn("This is content before the sentinels", article_text)
            self.assertNotIn("This is content after the sentinels", article_text)
            self.assertNotIn("Some header content", article_text)
            self.assertNotIn("Some footer content", article_text)

    def test_extract_article_text_missing_article(self):
        """Test that android_html_page handles missing article tag gracefully"""
        with tempfile.TemporaryDirectory() as temp_dir:
            # Create a sample HTML file without article tag
            html_content = """
            <html>
            <head><title>Test Class</title></head>
            <body>
                <p>This is content without an article tag.</p>
            </body>
            </html>
            """
            
            # Write the content to a temporary file
            file_path = os.path.join(temp_dir, 'test_no_article.html')
            with open(file_path, 'w') as f:
                f.write(html_content)
            
            # Import and initialize the class
            from android_html_page import AndroidHtmlPage
            
            # Test article text extraction with missing article
            page = AndroidHtmlPage(file_path)
            article_text = page.get_article_text()
            
            # Should return empty string or None when no article found
            self.assertEqual(article_text, "")

    def test_extract_content_between_sentinels(self):
        """Test that android_html_page extracts content between the specific HTML comment sentinels"""
        with tempfile.TemporaryDirectory() as temp_dir:
            # Create a sample HTML file with the sentinel comments
            html_content = """
            <html>
            <head><title>Test Class</title></head>
            <body>
                <header>Some header content</header>
                <article class="devsite-article">
                    <h1>Main Title</h1>
                    <p>This is content before the sentinels.</p>
                    
                    <!-- ======== START OF CLASS DATA ======== -->
                    <h2>Class Information</h2>
                    <p>This is the main class content that should be extracted.</p>
                    <div>
                        <h3>Methods</h3>
                        <p>Important method information here.</p>
                    </div>
                    <p>More class data content.</p>
                    <!-- ========= END OF CLASS DATA ========= -->
                    
                    <p>This is content after the sentinels.</p>
                    <footer>Some footer content</footer>
                </article>
            </body>
            </html>
            """
            
            # Write the content to a temporary file
            file_path = os.path.join(temp_dir, 'test_sentinels.html')
            with open(file_path, 'w') as f:
                f.write(html_content)
            
            # Import and initialize the class
            from android_html_page import AndroidHtmlPage
            
            # Test content extraction between sentinels
            page = AndroidHtmlPage(file_path)
            article_text = page.get_article_text()
            
            # Verify the extracted text contains the expected content between sentinels
            self.assertIn("Class Information", article_text)
            self.assertIn("This is the main class content that should be extracted", article_text)
            self.assertIn("Methods", article_text)
            self.assertIn("Important method information here", article_text)
            self.assertIn("More class data content", article_text)
            
            # Verify content outside sentinels is NOT included
            self.assertNotIn("This is content before the sentinels", article_text)
            self.assertNotIn("This is content after the sentinels", article_text)
            self.assertNotIn("Some header content", article_text)
            self.assertNotIn("Some footer content", article_text)
            self.assertNotIn("Main Title", article_text)

    def test_extract_content_missing_sentinels(self):
        """Test that android_html_page handles missing sentinel comments gracefully"""
        with tempfile.TemporaryDirectory() as temp_dir:
            # Create a sample HTML file without sentinel comments
            html_content = """
            <html>
            <head><title>Test Class</title></head>
            <body>
                <article class="devsite-article">
                    <h1>Main Title</h1>
                    <p>This is content without sentinel comments.</p>
                </article>
            </body>
            </html>
            """
            
            # Write the content to a temporary file
            file_path = os.path.join(temp_dir, 'test_no_sentinels.html')
            with open(file_path, 'w') as f:
                f.write(html_content)
            
            # Import and initialize the class
            from android_html_page import AndroidHtmlPage
            
            # Test content extraction with missing sentinels
            page = AndroidHtmlPage(file_path)
            article_text = page.get_article_text()
            
            # Should return empty string when no sentinels found
            self.assertEqual(article_text, "")

    def test_get_html_page(self):
        """Test that AndroidHtmlPage returns a valid HTML page with proper structure"""
        with tempfile.TemporaryDirectory() as temp_dir:
            # Create a sample HTML file with sentinel comments
            html_content = """
            <html>
            <head><title>Original Title</title></head>
            <body>
                <header>Some header content</header>
                <article class="devsite-article">
                    <p>This is content before the sentinels.</p>
                    
                    <!-- ======== START OF CLASS DATA ======== -->
                    <h1>Main Title</h1>
                    <h2>Class Information</h2>
                    <p>This is the main class content that should be extracted.</p>
                    <div>
                        <h3>Methods</h3>
                        <p>Important method information here.</p>
                    </div>
                    <p>More class data content.</p>
                    <!-- ========= END OF CLASS DATA ========= -->
                    
                    <p>This is content after the sentinels.</p>
                </article>
                <footer>Some footer content</footer>
            </body>
            </html>
            """
            
            # Write the content to a temporary file
            file_path = os.path.join(temp_dir, 'test_class.html')
            with open(file_path, 'w') as f:
                f.write(html_content)
            
            # Import and initialize the class
            from android_html_page import AndroidHtmlPage
            
            # Test HTML page generation
            page = AndroidHtmlPage(file_path)
            html_page = page.get_html_page()
            
            # Verify the HTML structure is correct
            self.assertIn("<html>", html_page)
            self.assertIn("</html>", html_page)
            self.assertIn("<head>", html_page)
            self.assertIn("</head>", html_page)
            self.assertIn("<body>", html_page)
            self.assertIn("</body>", html_page)
            
            # Verify the title is extracted from the h1 tag
            self.assertIn("<title>Main Title</title>", html_page)
            
            # Verify the article content is included
            self.assertIn("Class Information", html_page)
            self.assertIn("This is the main class content that should be extracted", html_page)
            self.assertIn("Methods", html_page)
            self.assertIn("Important method information here", html_page)
            self.assertIn("More class data content", html_page)
            
            # Verify content outside sentinels is NOT included
            self.assertNotIn("This is content before the sentinels", html_page)
            self.assertNotIn("This is content after the sentinels", html_page)
            self.assertNotIn("Some header content", html_page)
            self.assertNotIn("Some footer content", html_page)
            self.assertNotIn("Original Title", html_page)
            
            # Verify the structure follows the expected pattern
            # The HTML should start with <html> and end with </html>
            self.assertTrue(html_page.strip().startswith("<html>"))
            self.assertTrue(html_page.strip().endswith("</html>"))
            
            # Verify head section contains title
            head_start = html_page.find("<head>")
            head_end = html_page.find("</head>")
            self.assertGreater(head_start, -1)
            self.assertGreater(head_end, -1)
            self.assertGreater(head_end, head_start)
            
            head_content = html_page[head_start:head_end + 7]
            self.assertIn("<title>Main Title</title>", head_content)
            
            # Verify body section contains article content
            body_start = html_page.find("<body>")
            body_end = html_page.find("</body>")
            self.assertGreater(body_start, -1)
            self.assertGreater(body_end, -1)
            self.assertGreater(body_end, body_start)
            
            body_content = html_page[body_start:body_end + 7]
            self.assertIn("Class Information", body_content)
            self.assertIn("Important method information here", body_content)

if __name__ == '__main__':
    unittest.main() 