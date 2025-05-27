import argparse
import os
import multiprocessing
from DocumentationDatabase import DocumentationDatabase
from validator import HTMLValidator

def process_file(file_path, db_path):
    print(f"Processing file: {file_path}")
    
    # For HTML files, validate before adding
    if file_path.endswith('.html') or file_path.endswith('.htm'):
        validator = HTMLValidator()
        if not validator.check_html_file(file_path):
            print(f"Skipping invalid HTML file: {file_path}")
            return

    # Add file to database
    db = DocumentationDatabase(db_path)
    with open(file_path, 'rb') as file:
        content = file.read()
    if db.add_file(file_path, content, 'en-US'):
        print(f"Added file {file_path} to the database.")

def main():
    parser = argparse.ArgumentParser(description='Ingest files into a documentation database.')
    parser.add_argument('-f', '--file', help='Path to a file to add to the database.')
    parser.add_argument('-d', '--directory', help='Path to a directory to add all files to the database.')
    parser.add_argument('-p', '--path-to-db', help='Path to the SQLite database file.', default='/tmp/my-doc-db.sqlite')
    args = parser.parse_args()

    db_path = args.path_to_db
    db = DocumentationDatabase(db_path)

    if args.file:
        with open(args.file, 'rb') as file:
            content = file.read()
        if db.add_file(args.file, content, 'en-US'):
            print(f"Added file {args.file} to the database.")

    if args.directory:
        file_paths = [os.path.join(args.directory, filename) for filename in os.listdir(args.directory) if os.path.isfile(os.path.join(args.directory, filename))]
        with multiprocessing.Pool() as pool:
            pool.starmap(process_file, [(file_path, db_path) for file_path in file_paths])

if __name__ == '__main__':
    main() 