import unittest
from DocumentationDatabase import DocumentationDatabase
from faker import Faker
import tempfile
import os
from PIL import Image
import io

class TestGetFile(unittest.TestCase):
    def test_insert_and_retrieve_plain_text_file(self):
        fake = Faker()
        # Generate a random plain text file
        text_content = fake.text().encode()
        text_path = 'random_' + fake.file_name(extension='txt')

        # Create a temporary database file
        temp_db_file = tempfile.NamedTemporaryFile(delete=False)
        temp_db_file.close()
        os.unlink(temp_db_file.name)

        # Initialize the database
        db = DocumentationDatabase(temp_db_file.name)

        # Insert the file into the database
        db.add_file(text_path, text_content, 'en-US')

        # Retrieve the file from the database
        with db.get_file(text_path, 'en-US') as retrieved_file:
            retrieved_content = retrieved_file.read()

        # Verify the retrieved content matches the original
        self.assertEqual(retrieved_content, text_content)

        # Clean up the temporary database file
        os.unlink(temp_db_file.name)

    def test_insert_and_retrieve_html_file(self):
        fake = Faker()
        # Generate a random HTML file
        html_content = b'<html><head><title>' + fake.sentence().encode() + b'</title></head><body>' + fake.paragraph().encode() + b'</body></html>'
        html_path = 'random_' + fake.file_name(extension='html')

        # Create a temporary database file
        temp_db_file = tempfile.NamedTemporaryFile(delete=False)
        temp_db_file.close()
        os.unlink(temp_db_file.name)

        # Initialize the database
        db = DocumentationDatabase(temp_db_file.name)

        # Insert the file into the database
        db.add_file(html_path, html_content, 'en-US')

        # Retrieve the file from the database
        with db.get_file(html_path, 'en-US') as retrieved_file:
            retrieved_content = retrieved_file.read()

        # Verify the retrieved content matches the original
        self.assertEqual(retrieved_content, html_content)

        # Clean up the temporary database file
        os.unlink(temp_db_file.name)

    def test_insert_and_retrieve_jpeg_file(self):
        fake = Faker()
        # Create a simple JPEG image using Pillow
        img = Image.new('RGB', (800, 600), color='red')
        img_byte_arr = io.BytesIO()
        img.save(img_byte_arr, format='JPEG')
        jpeg_content = img_byte_arr.getvalue()
        jpeg_path = 'random_' + fake.file_name(extension='jpg')

        # Create a temporary database file
        temp_db_file = tempfile.NamedTemporaryFile(delete=False)
        temp_db_file.close()
        os.unlink(temp_db_file.name)

        # Initialize the database
        db = DocumentationDatabase(temp_db_file.name)

        # Insert the file into the database
        db.add_file(jpeg_path, jpeg_content, 'en-US')

        # Retrieve the file from the database
        with db.get_file(jpeg_path, 'en-US') as retrieved_file:
            retrieved_content = retrieved_file.read()

        # Verify the retrieved content matches the original
        self.assertEqual(retrieved_content, jpeg_content)

        # Clean up the temporary database file
        os.unlink(temp_db_file.name)

    def test_insert_and_retrieve_png_file(self):
        fake = Faker()
        # Create a more interesting PNG image using Pillow
        img = Image.new('RGB', (800, 600), color='green')
        # Add a solid square in the middle
        for x in range(300, 500):
            for y in range(200, 400):
                img.putpixel((x, y), (255, 0, 0))  # red
        # Add some random colors
        for x in range(0, 800, 100):
            for y in range(0, 600, 100):
                img.putpixel((x, y), (0, 0, 255))  # blue
        for x in range(50, 800, 100):
            for y in range(50, 600, 100):
                img.putpixel((x, y), (255, 255, 0))  # yellow
        img_byte_arr = io.BytesIO()
        img.save(img_byte_arr, format='PNG')
        png_content = img_byte_arr.getvalue()
        png_path = 'random_' + fake.file_name(extension='png')

        # Create a temporary database file
        temp_db_file = tempfile.NamedTemporaryFile(delete=False)
        temp_db_file.close()
        os.unlink(temp_db_file.name)

        # Initialize the database
        db = DocumentationDatabase(temp_db_file.name)

        # Insert the file into the database
        db.add_file(png_path, png_content, 'en-US')

        # Retrieve the file from the database
        with db.get_file(png_path, 'en-US') as retrieved_file:
            retrieved_content = retrieved_file.read()

        # Validate that the retrieved content is a valid PNG file
        try:
            Image.open(io.BytesIO(retrieved_content))
        except Exception as e:
            self.fail(f"Retrieved content is not a valid PNG file: {e}")

        # Summarize the input size, output size, and percent saved
        input_size = len(png_content)
        output_size = len(retrieved_content)
        percent_saved = ((input_size - output_size) / input_size) * 100
        print(f"PNG Test: Input size: {input_size} bytes, Output size: {output_size} bytes, Percent saved: {percent_saved:.2f}%")

        # Clean up the temporary database file
        os.unlink(temp_db_file.name)

if __name__ == '__main__':
    unittest.main() 