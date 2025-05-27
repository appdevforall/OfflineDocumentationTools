import argparse
import sqlite3
import os

def main():
    parser = argparse.ArgumentParser(description='List documents in a SQLite database.')
    parser.add_argument('-p', '--path', help='Path to the SQLite database file.', required=True)
    args = parser.parse_args()
    db_path = args.path
    print(f"Database path: {db_path}")

    if not os.path.exists(db_path):
        print(f"Error: Database file not found at {db_path}")
        return

    try:
        conn = sqlite3.connect(db_path)
        cursor = conn.cursor()
        cursor.execute("SELECT path, LENGTH(content) FROM content")
        for row in cursor.fetchall():
            print(f"Path: {row[0]}, Bytes: {row[1]}")
    except sqlite3.Error as e:
        print(f"Database error: {e}")
    finally:
        if 'conn' in locals():
            conn.close()

if __name__ == '__main__':
    main() 