import os
from bs4 import BeautifulSoup
from bs4.builder import ParserRejectedMarkup  # This exception is useful for lxml
import logging

# Configure logging to see messages
logging.basicConfig(level=logging.INFO, format='%(levelname)s: %(message)s')

def check_html_validity(file_path):
    """
    Checks if an HTML file is valid in terms of tag nesting using BeautifulSoup.
    Returns True if valid, False if broken.
    """
    try:
        with open(file_path, 'r', encoding='utf-8') as f:
            content = f.read()

        # Try parsing with lxml (generally more strict and faster)
        # If lxml rejects the markup, it often means it's severely malformed.
        try:
            BeautifulSoup(content, 'lxml')
            return True
        except ParserRejectedMarkup:
            logging.warning(f"File '{file_path}' rejected by lxml parser. Likely severe nesting issues.")
            return False
        except Exception as e:
            # Fallback to html.parser for less strict checks or other errors
            logging.warning(f"Error with lxml parsing '{file_path}': {e}. Attempting html.parser.")
            try:
                BeautifulSoup(content, 'html.parser')
                # html.parser is very forgiving. If it parses, it might still have issues
                # but might be considered "valid enough" for some purposes.
                # For strict nesting, lxml's ParserRejectedMarkup is key.
                return True
            except Exception as e_html:
                logging.error(f"File '{file_path}' is severely malformed and could not be parsed by html.parser either: {e_html}")
                return False

    except FileNotFoundError:
        logging.error(f"File not found: {file_path}")
        return False
    except UnicodeDecodeError:
        logging.error(f"UnicodeDecodeError: Could not read file '{file_path}'. Check encoding.")
        return False
    except Exception as e:
        logging.error(f"An unexpected error occurred while processing '{file_path}': {e}")
        return False

def scan_html_directory(directory_path):
    """
    Scans a directory for HTML files and categorizes them as valid or broken.
    """
    valid_files = []
    broken_files = []

    if not os.path.isdir(directory_path):
        logging.error(f"Error: Directory not found at '{directory_path}'")
        return valid_files, broken_files

    logging.info(f"Scanning directory: {directory_path}")
    for filename in os.listdir(directory_path):
        if filename.endswith(".html") or filename.endswith(".htm"):
            file_path = os.path.join(directory_path, filename)
            logging.info(f"Checking: {file_path}")
            if check_html_validity(file_path):
                valid_files.append(file_path)
            else:
                broken_files.append(file_path)

    return valid_files, broken_files

if __name__ == "__main__":
    # --- IMPORTANT: Configure your directory path here ---
    # Replace 'your_html_documents_directory' with the actual path to your HTML files
    html_directory = './your_html_documents_directory'
    # ----------------------------------------------------

    # Create a dummy directory and some files for testing
    if not os.path.exists(html_directory):
        os.makedirs(html_directory)

    # Valid HTML
    with open(os.path.join(html_directory, 'valid_doc1.html'), 'w', encoding='utf-8') as f:
        f.write("<html><head><title>Test</title></head><body><h1>Hello</h1><p>This is a paragraph.</p></body></html>")

    with open(os.path.join(html_directory, 'valid_doc2.html'), 'w', encoding='utf-8') as f:
        f.write("<!DOCTYPE html><html><body><div><span>Text</span></div></body></html>")

    # Broken HTML (intermingled tags)
    with open(os.path.join(html_directory, 'broken_doc1.html'), 'w', encoding='utf-8') as f:
        f.write("<html><body><tag1><tag2></tag1></tag2></body></html>") # Intermingled

    with open(os.path.join(html_directory, 'broken_doc2.html'), 'w', encoding='utf-8') as f:
        f.write("<html><body><div><p><span>Hello</div></p></span></body></html>") # More complex intermingling

    # Malformed (unclosed tags, etc.)
    with open(os.path.join(html_directory, 'malformed_doc1.html'), 'w', encoding='utf-8') as f:
        f.write("<html><body><div><span>Hello") # Unclosed tags

    with open(os.path.join(html_directory, 'malformed_doc2.html'), 'w', encoding='utf-8') as f:
        f.write("<html><body><p>This is a paragraph.</body></html>") # Missing closing p tag

    valid, broken = scan_html_directory(html_directory)

    print("\n--- Scan Results ---")
    print("\nValid HTML Files:")
    if valid:
        for f in valid:
            print(f"- {f}")
    else:
        print("  No valid HTML files found.")

    print("\nBroken HTML Files:")
    if broken:
        for f in broken:
            print(f"- {f}")
    else:
        print("  No broken HTML files found.")

    print(f"\nSummary: {len(valid)} valid, {len(broken)} broken.")

    # Clean up dummy files/directory
    # import shutil
    # shutil.rmtree(html_directory)
    # print(f"\nCleaned up dummy directory: {html_directory}")
