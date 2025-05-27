import os
from bs4 import BeautifulSoup
from bs4.builder import ParserRejectedMarkup
import logging
from lxml import etree, html

class HTMLValidator:
    """
    A class for validating HTML files and scanning directories for HTML validity.
    """
    def __init__(self):
        # Configure logging to see messages
        logging.basicConfig(level=logging.INFO, format='%(levelname)s: %(message)s')
        self.logger = logging.getLogger(__name__)

    def validate_html_content(self, content: str) -> bool:
        """
        Validates HTML content string for proper tag nesting using lxml.etree.XMLParser (strict XHTML mode).
        Returns True if valid, False if broken.
        Note: Input must be well-formed XHTML.
        """
        try:
            parser = etree.XMLParser(recover=False)
            etree.fromstring(content.encode('utf-8'), parser=parser)
            return True
        except etree.XMLSyntaxError as e:
            self.logger.warning(f"lxml.etree strict XHTML validation failed: {e}")
            return False
        except Exception as e:
            self.logger.error(f"Unexpected error during lxml XHTML validation: {e}")
            return False

    def check_html_file(self, file_path: str) -> bool:
        """
        Checks if an HTML file is valid by reading its contents and validating them.
        Returns True if valid, False if broken or if file cannot be read.
        """
        try:
            with open(file_path, 'r', encoding='utf-8') as f:
                content = f.read()
            return self.validate_html_content(content)
        except FileNotFoundError:
            self.logger.error(f"File not found: {file_path}")
            return False
        except UnicodeDecodeError:
            self.logger.error(f"UnicodeDecodeError: Could not read file '{file_path}'. Check encoding.")
            return False
        except Exception as e:
            self.logger.error(f"An unexpected error occurred while processing '{file_path}': {e}")
            return False

    def scan_html_directory(self, directory_path):
        """
        Scans a directory for HTML files and categorizes them as valid or broken.
        """
        valid_files = []
        broken_files = []

        if not os.path.isdir(directory_path):
            self.logger.error(f"Error: Directory not found at '{directory_path}'")
            return valid_files, broken_files

        self.logger.info(f"Scanning directory: {directory_path}")
        for filename in os.listdir(directory_path):
            if filename.endswith(".html") or filename.endswith(".htm"):
                file_path = os.path.join(directory_path, filename)
                self.logger.info(f"Checking: {file_path}")
                if self.check_html_file(file_path):
                    valid_files.append(file_path)
                else:
                    broken_files.append(file_path)

        return valid_files, broken_files

if __name__ == "__main__":
    # Example usage
    validator = HTMLValidator()
    
    # Example: Validate a directory
    directory_path = './your_html_documents_directory'
    valid_files, broken_files = validator.scan_html_directory(directory_path)
    
    print("\n--- Scan Results ---")
    print("\nValid HTML Files:")
    if valid_files:
        for f in valid_files:
            print(f"- {f}")
    else:
        print("  No valid HTML files found.")

    print("\nBroken HTML Files:")
    if broken_files:
        for f in broken_files:
            print(f"- {f}")
    else:
        print("  No broken HTML files found.")

    print(f"\nSummary: {len(valid_files)} valid, {len(broken_files)} broken.")

    # Clean up dummy files/directory
    # import shutil
    # shutil.rmtree(html_directory)
    # print(f"\nCleaned up dummy directory: {html_directory}")
