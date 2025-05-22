import unittest
import os
import tempfile
import sqlite3
from DocumentationDatabase import DocumentationDatabase
from faker import Faker
from PIL import Image
import io
from faker_file.providers.jpeg_file import JpegFileProvider
from faker_file.providers.image.pil_generator import PilImageGenerator
from faker_file.storages.filesystem import FileSystemStorage

class TestAddFile(unittest.TestCase):
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

    def test_add_html_file_to_database(self):
        # Insert an HTML file into the Content table using a missing method
        html_path = 'testfile.html'
        html_content = b'<html><body>Hello</body></html>'
        with sqlite3.connect(self.temp_db_file.name) as connection:
            cursor = connection.cursor()
            # Count the number of files before adding the new file
            cursor.execute("SELECT COUNT(*) FROM Content")
            count_before = cursor.fetchone()[0]
            # Call the missing method to add the file
            self.db.add_file(html_path, html_content, 'en-US')
            # Verify the file is present and the number of files increased by 1
            cursor.execute("SELECT COUNT(*) FROM Content")
            count_after = cursor.fetchone()[0]
            self.assertEqual(count_after, count_before + 1)

    def test_add_random_html_file_to_database(self):
        fake = Faker()
        # Generate a random HTML file of at least 5 KB
        html_content = b'<html><head><title>' + fake.sentence().encode() + b'</title></head><body>'
        while len(html_content) < 5 * 1024:  # Ensure at least 5 KB
            html_content += b'<p>' + fake.paragraph().encode() + b'</p>'
        html_content += b'</body></html>'
        html_path = 'random_' + fake.file_name(extension='html')
        with sqlite3.connect(self.temp_db_file.name) as connection:
            cursor = connection.cursor()
            # Count the number of files before adding the new file
            cursor.execute("SELECT COUNT(*) FROM Content")
            count_before = cursor.fetchone()[0]
            # Call the missing method to add the file
            self.db.add_file(html_path, html_content, 'en-US')
            # Verify the file is present and the number of files increased by 1
            cursor.execute("SELECT COUNT(*) FROM Content")
            count_after = cursor.fetchone()[0]
            self.assertEqual(count_after, count_before + 1)

    def test_compressed_content_is_smaller(self):
        fake = Faker()
        # Generate a large HTML file to ensure compression is noticeable
        html_content = b'<html><head><title>' + fake.sentence().encode() + b'</title></head><body>'
        while len(html_content) < 10 * 1024:  # Ensure at least 10 KB
            html_content += b'<p>' + fake.paragraph().encode() + b'</p>'
        html_content += b'</body></html>'
        html_path = 'compressed_' + fake.file_name(extension='html')
        with sqlite3.connect(self.temp_db_file.name) as connection:
            cursor = connection.cursor()
            # Call the missing method to add the file
            self.db.add_file(html_path, html_content, 'en-US')
            # Verify the stored content is smaller than the original
            cursor.execute("SELECT content FROM Content WHERE path = ?", (html_path,))
            stored_content = cursor.fetchone()[0]
            self.assertLess(len(stored_content), len(html_content))

    def test_add_plain_text_file_to_database(self):
        fake = Faker()
        # Generate a large plain text file to ensure compression is noticeable
        text_content = b''
        while len(text_content) < 10 * 1024:  # Ensure at least 10 KB
            text_content += fake.paragraph().encode() + b'\n'
        text_path = 'random_' + fake.file_name(extension='txt')
        with sqlite3.connect(self.temp_db_file.name) as connection:
            cursor = connection.cursor()
            # Count the number of files before adding the new file
            cursor.execute("SELECT COUNT(*) FROM Content")
            count_before = cursor.fetchone()[0]
            # Call the missing method to add the file
            self.db.add_file(text_path, text_content, 'en-US')
            # Verify the file is present and the number of files increased by 1
            cursor.execute("SELECT COUNT(*) FROM Content")
            count_after = cursor.fetchone()[0]
            self.assertEqual(count_after, count_before + 1)
            # Verify the stored content is smaller than the original
            cursor.execute("SELECT content FROM Content WHERE path = ?", (text_path,))
            stored_content = cursor.fetchone()[0]
            self.assertLess(len(stored_content), len(text_content))

    def test_add_css_file_to_database(self):
        fake = Faker()
        # Generate a large CSS file to ensure compression is noticeable
        css_content = b''
        while len(css_content) < 10 * 1024:  # Ensure at least 10 KB
            css_content += f"""
            .{fake.word()} {{
                color: {fake.hex_color()};
                font-size: {fake.random_int(min=12, max=24)}px;
                margin: {fake.random_int(min=0, max=20)}px;
                padding: {fake.random_int(min=0, max=20)}px;
                background-color: {fake.hex_color()};
            }}
            """.encode()
        css_path = 'random_' + fake.file_name(extension='css')
        with sqlite3.connect(self.temp_db_file.name) as connection:
            cursor = connection.cursor()
            # Count the number of files before adding the new file
            cursor.execute("SELECT COUNT(*) FROM Content")
            count_before = cursor.fetchone()[0]
            # Call the missing method to add the file
            self.db.add_file(css_path, css_content, 'en-US')
            # Verify the file is present and the number of files increased by 1
            cursor.execute("SELECT COUNT(*) FROM Content")
            count_after = cursor.fetchone()[0]
            self.assertEqual(count_after, count_before + 1)
            # Verify the stored content is smaller than the original
            cursor.execute("SELECT content FROM Content WHERE path = ?", (css_path,))
            stored_content = cursor.fetchone()[0]
            self.assertLess(len(stored_content), len(css_content))

    def test_add_markdown_file_to_database(self):
        fake = Faker()
        # Generate a large Markdown file to ensure compression is noticeable
        markdown_content = b''
        while len(markdown_content) < 10 * 1024:  # Ensure at least 10 KB
            markdown_content += f"""
            # {fake.sentence()}
            {fake.paragraph()}
            ## {fake.sentence()}
            {fake.paragraph()}
            """.encode()
        markdown_path = 'random_' + fake.file_name(extension='md')
        with sqlite3.connect(self.temp_db_file.name) as connection:
            cursor = connection.cursor()
            # Count the number of files before adding the new file
            cursor.execute("SELECT COUNT(*) FROM Content")
            count_before = cursor.fetchone()[0]
            # Call the missing method to add the file
            self.db.add_file(markdown_path, markdown_content, 'en-US')
            # Verify the file is present and the number of files increased by 1
            cursor.execute("SELECT COUNT(*) FROM Content")
            count_after = cursor.fetchone()[0]
            self.assertEqual(count_after, count_before + 1)
            # Verify the stored content is smaller than the original
            cursor.execute("SELECT content FROM Content WHERE path = ?", (markdown_path,))
            stored_content = cursor.fetchone()[0]
            self.assertLess(len(stored_content), len(markdown_content))

    def test_add_jpeg_image_to_database(self):
        fake = Faker()
        # Create a simple JPEG image using Pillow
        img = Image.new('RGB', (800, 600), color='red')
        img_byte_arr = io.BytesIO()
        img.save(img_byte_arr, format='JPEG')
        jpeg_content = img_byte_arr.getvalue()
        jpeg_path = 'random_' + fake.file_name(extension='jpg')
        with sqlite3.connect(self.temp_db_file.name) as connection:
            cursor = connection.cursor()
            # Count the number of files before adding the new file
            cursor.execute("SELECT COUNT(*) FROM Content")
            count_before = cursor.fetchone()[0]
            # Call the missing method to add the file
            self.db.add_file(jpeg_path, jpeg_content, 'en-US')
            # Verify the file is present and the number of files increased by 1
            cursor.execute("SELECT COUNT(*) FROM Content")
            count_after = cursor.fetchone()[0]
            self.assertEqual(count_after, count_before + 1)
            # Verify the stored content is not compressed
            cursor.execute("SELECT content FROM Content WHERE path = ?", (jpeg_path,))
            stored_content = cursor.fetchone()[0]
            self.assertEqual(len(stored_content), len(jpeg_content))

    def test_add_gif_image_to_database(self):
        fake = Faker()
        # Create a simple GIF image using Pillow
        img = Image.new('RGB', (800, 600), color='blue')
        img_byte_arr = io.BytesIO()
        img.save(img_byte_arr, format='GIF')
        gif_content = img_byte_arr.getvalue()
        gif_path = 'random_' + fake.file_name(extension='gif')
        with sqlite3.connect(self.temp_db_file.name) as connection:
            cursor = connection.cursor()
            # Count the number of files before adding the new file
            cursor.execute("SELECT COUNT(*) FROM Content")
            count_before = cursor.fetchone()[0]
            # Call the missing method to add the file
            self.db.add_file(gif_path, gif_content, 'en-US')
            # Verify the file is present and the number of files increased by 1
            cursor.execute("SELECT COUNT(*) FROM Content")
            count_after = cursor.fetchone()[0]
            self.assertEqual(count_after, count_before + 1)
            # Verify the stored content is not compressed
            cursor.execute("SELECT content FROM Content WHERE path = ?", (gif_path,))
            stored_content = cursor.fetchone()[0]
            self.assertEqual(len(stored_content), len(gif_content))

    def test_add_png_image_to_database(self):
        fake = Faker()
        # Create a PNG image: green on the left half, yellow on the right half
        width, height = 800, 600
        img = Image.new('RGB', (width, height))
        for x in range(width):
            for y in range(height):
                if x < width // 2:
                    img.putpixel((x, y), (0, 128, 0))  # green
                else:
                    img.putpixel((x, y), (255, 255, 0))  # yellow
        img_byte_arr = io.BytesIO()
        img.save(img_byte_arr, format='PNG')
        png_content = img_byte_arr.getvalue()
        png_path = 'random_' + fake.file_name(extension='png')
        with sqlite3.connect(self.temp_db_file.name) as connection:
            cursor = connection.cursor()
            # Count the number of files before adding the new file
            cursor.execute("SELECT COUNT(*) FROM Content")
            count_before = cursor.fetchone()[0]
            # Call the missing method to add the file
            self.db.add_file(png_path, png_content, 'en-US')
            # Verify the file is present and the number of files increased by 1
            cursor.execute("SELECT COUNT(*) FROM Content")
            count_after = cursor.fetchone()[0]
            self.assertEqual(count_after, count_before + 1)
            # Verify the stored content is less than or equal to the original
            cursor.execute("SELECT content FROM Content WHERE path = ?", (png_path,))
            stored_content = cursor.fetchone()[0]
            self.assertLessEqual(len(stored_content), len(png_content))

if __name__ == '__main__':
    unittest.main() 