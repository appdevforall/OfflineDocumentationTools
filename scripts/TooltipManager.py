import sqlite3
import csv
import sys
import os
import io
import time
import argparse
import shutil

SCHEMA_TOOLTIPS = """
CREATE TABLE IF NOT EXISTS "Tooltips" (
  'id' INTEGER PRIMARY KEY AUTOINCREMENT, 
  'categoryId' INTEGER NOT NULL, 
  'tag' TEXT NOT NULL, 
  'summary' TEXT NOT NULL, 
  'detail' TEXT NOT NULL, 
  UNIQUE ('categoryId', 'tag'),
  FOREIGN KEY(categoryId) REFERENCES TooltipCategories(id)
);
"""

SCHEMA_TOOLTIP_BUTTONS = """
CREATE TABLE IF NOT EXISTS TooltipButtons (
  'tooltipId' INTEGER,
  'buttonNumberId' INTEGER,
  'description' TEXT,
  'uri' TEXT,
  FOREIGN KEY(tooltipId) REFERENCES Tooltips(id),
  FOREIGN KEY(buttonNumberId) REFERENCES TooltipButtonNumbers(id)
);
"""


def db_tooltips_to_csv(conn, csv_file_out):
    """
    Dumps the contents of the 'Tooltips' and 'TooltipButtons' tables to a CSV file.

    Args:
        conn: A SQLite database connection object.
        csv_file_out: The path to the output CSV file.
    """
    cursor = conn.cursor()

    print(f"Dumping database to CSV file: {csv_file_out}")

    # Get all entries from the main Tooltips table
    cursor.execute("SELECT id, categoryId, tag, summary, detail FROM Tooltips")
    all_tooltips = cursor.fetchall()

    # Iterate over each tooltip and populate the new table
    data_array = []

    # Insert or update the new table with the combined data
    headers = ['categoryId', 'tag', 'summary', 'detail', 'description1', 'uri1', 'description2', 'uri2', 'description3',
               'uri3']

    idx = 0
    for tooltip_id, category_id, tag, summary, detail in all_tooltips:
        if idx % 100 == 0:
            print(f"Processing row {idx}")
        idx += 1

        # Fetch all buttons for the current tooltip
        cursor.execute('''
                   SELECT buttonNumberId, description, uri
                   FROM TooltipButtons
                   WHERE tooltipId = ?
                   ORDER BY buttonNumberId ASC
               ''', (tooltip_id,))
        buttons = cursor.fetchall()

        # Prepare the button data for the new table
        button_data = {}
        for button in buttons:
            button_num = button[0]
            desc = button[1]
            uri = button[2]
            button_data[f'description{button_num}'] = desc
            button_data[f'uri{button_num}'] = uri

        data_array.append([
            category_id,
            tag, summary,
            detail,
            button_data.get('description1'),
            button_data.get('uri1'),
            button_data.get('description2'),
            button_data.get('uri2'),
            button_data.get('description3'),
            button_data.get('uri3')
        ])

    with open(csv_file_out, 'w', newline='', encoding='utf-8') as csvfile:
        writer = csv.writer(csvfile)
        writer.writerow(headers)
        writer.writerows(data_array)

    print("Database dump to CSV complete!")


def csv_to_tooltips(conn, csv_file, name):
    """
    Reconstructs the database from a CSV file.

    Args:
        conn: A SQLite database connection object.
        csv_file: The path to the input CSV file.
        name: The name of the person performing the build.
    """
    try:
        cursor = conn.cursor()
        # Drop tables if they exist to ensure a clean slate.
        print("Dropping existing tables...")
        cursor.execute("DROP TABLE IF EXISTS TooltipButtons")
        cursor.execute("DROP TABLE IF EXISTS Tooltips")
        cursor.execute("DROP TABLE IF EXISTS LastChange")

        # Create the tables based on the schemas.
        print("Creating database tables...")
        cursor.execute(SCHEMA_TOOLTIPS)
        cursor.execute(SCHEMA_TOOLTIP_BUTTONS)

        # Read data from the CSV file and populate the tables.
        print(f"Reading data from {csv_file}...")
        with open(csv_file, 'r', encoding='utf-8') as csvfile:
            reader = csv.DictReader(csvfile)
            for row in reader:
                # Insert data into the 'Tooltips' table first.
                cursor.execute(
                    "INSERT INTO Tooltips (categoryId, tag, summary, detail) VALUES (?, ?, ?, ?)",
                    (row['categoryId'], row['tag'], row['summary'], row['detail'])
                )

                # Get the ID of the newly created tooltip using lastrowid.
                tooltip_id = cursor.lastrowid

                # Insert data into the 'TooltipButtons' table for each of the three buttons.
                for i in range(1, 4):
                    description_key = f'description{i}'
                    uri_key = f'uri{i}'

                    # Check if the description and URI values exist before inserting.
                    if row.get(description_key) and row.get(uri_key):
                        cursor.execute(
                            "INSERT INTO TooltipButtons (tooltipId, buttonNumberId, description, uri) VALUES (?, ?, ?, ?)",
                            (tooltip_id, i, row[description_key], row[uri_key])
                        )

        cursor.execute("""
        CREATE TABLE LastChange (
            now TIMESTAMP,
            who TEXT
        )""")

        cursor.execute("""
        INSERT INTO LastChange VALUES (CURRENT_TIMESTAMP, ?)""", (name,))

        # Commit the changes to the database.
        conn.commit()
        print("Database population complete!")
    except sqlite3.Error as e:
        print(f"SQLite error: {e}")
        sys.exit(1)
    except FileNotFoundError:
        print(f"Error: The file '{csv_file}' was not found.")
        sys.exit(1)
    finally:
        conn.close()


def main():
    parser = argparse.ArgumentParser(description='Dump or build a SQLite database for tooltips.')

    # Define required arguments
    parser.add_argument('--operation', choices=['dump', 'build'], required=True,
                        help='Determines whether to dump the database to a CSV or build the database from a CSV.')
    parser.add_argument('--name', required=True,
                        help='The name of the person updating the database.')
    parser.add_argument('--input-db', required=True,
                        help='The path to the input database for both operations.')

    # Define conditional arguments
    parser.add_argument('--output-csv',
                        help='The path to the output CSV file for the dump operation.')
    parser.add_argument('--input-csv',
                        help='The path to the input CSV file for the build operation.')
    parser.add_argument('--output-db',
                        help='The path to the built database output file for the build operation.')

    args = parser.parse_args()

    conn = None
    try:
        # Check if the input database exists for both operations
        if not os.path.exists(args.input_db):
            print(f"Error: Input database file '{args.input_db}' does not exist.")
            sys.exit(1)

        if args.operation == 'dump':
            if not args.output_csv:
                parser.error("--output-csv is required for the 'dump' operation.")

            conn = sqlite3.connect(args.input_db)
            db_tooltips_to_csv(conn, args.output_csv)

        elif args.operation == 'build':
            if not args.input_csv or not args.output_db:
                parser.error("--input-csv and --output-db are required for the 'build' operation.")

            if not os.path.exists(args.input_csv):
                print(f"Error: Input CSV file '{args.input_csv}' does not exist.")
                sys.exit(1)

            # Copy the input database to the output path
            print(f"Copying '{args.input_db}' to '{args.output_db}'...")
            shutil.copyfile(args.input_db, args.output_db)

            # Connect to the newly created database file
            conn = sqlite3.connect(args.output_db)
            csv_to_tooltips(conn, args.input_csv, args.name)

    except sqlite3.Error as e:
        print(f"A database error occurred: {e}")
        sys.exit(1)
    except shutil.SameFileError:
        print("Error: Input and output database files are the same. Please specify different paths.")
        sys.exit(1)
    except IOError as e:
        print(f"File I/O error: {e}")
        sys.exit(1)
    finally:
        if conn:
            conn.close()


if __name__ == "__main__":
    main()
