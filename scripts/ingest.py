import argparse
import os
from DocumentationDatabase import DocumentationDatabase

# TODO: add multiprocessing support for ingesting large directories

def main():
    parser = argparse.ArgumentParser(description='Ingest files into a documentation database.')
    parser.add_argument('-f', '--file', help='Path to a file to add to the database.')
    parser.add_argument('-d', '--directory', help='Path to a directory to add all files to the database.')
    args = parser.parse_args()

    db_path = '/tmp/my-doc-db.sqlite'
    db = DocumentationDatabase(db_path)

    if args.file:
        with open(args.file, 'rb') as file:
            content = file.read()
        db.add_file(args.file, content, 'en-US')
        print(f"Added file {args.file} to the database.")

    if args.directory:
        for filename in os.listdir(args.directory):
            file_path = os.path.join(args.directory, filename)
            if os.path.isfile(file_path):
                with open(file_path, 'rb') as file:
                    content = file.read()
                db.add_file(file_path, content, 'en-US')
                print(f"Added file {file_path} to the database.")

if __name__ == '__main__':
    main() 