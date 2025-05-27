import argparse
import sqlite3

def main():
    parser = argparse.ArgumentParser(description='List documents in a SQLite database.')
    parser.add_argument('-p', '--path', help='Path to the SQLite database file.', required=True)
    args = parser.parse_args()
    db_path = args.path
    print(f"Database path: {db_path}")

    conn = sqlite3.connect(db_path)
    cursor = conn.cursor()
    cursor.execute("SELECT path, LENGTH(content) FROM content")
    for row in cursor.fetchall():
        print(f"Path: {row[0]}, Bytes: {row[1]}")
    conn.close()

if __name__ == '__main__':
    main() 