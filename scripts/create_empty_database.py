#! /usr/bin/python

import sys
from DocumentationDatabase import DocumentationDatabase

if __name__ == "__main__":
    if len(sys.argv) != 2:
        print("Usage: python create_empty_database.py <database_path>")
        sys.exit(1)
    database_path = sys.argv[1]
    db = DocumentationDatabase(database_path)
    db.create_empty_database("/tmp/files")
