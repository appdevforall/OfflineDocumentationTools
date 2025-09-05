import sqlite3
import csv
import sys
import os
import argparse

def dump_table(table_name, output_name, cursor):
    select_query = f"SELECT * FROM {table_name}"
    cursor.execute(select_query)

    column_headers = [desc[0] for desc in cursor.description]

    rows = cursor.fetchall()

    # Write the data to the TSV file
    with open(output_name, 'w', newline='', encoding='utf-8') as tsv_file:
        tsv_writer = csv.writer(tsv_file, delimiter='\t')

        # Write the header row
        tsv_writer.writerow(column_headers)

        # Write the data rows
        tsv_writer.writerows(rows)

def main():
    print("Hello world")
    # /home/elissa/ADFA/1419scratch/androidxtooltips/documentation.db
    # Set up argument parser
    parser = argparse.ArgumentParser(description="Dump a SQLite table to a TSV file.")
    parser.add_argument(
        "--db-file",
        required=True,
        help="The path to the SQLite database file."
    )
    parser.add_argument(
        "--table-name",
        required=True,
        help="The name of the table to dump."
    )
    parser.add_argument(
        "--output-file",
        required=True,
        help="The path for the output TSV file."
    )

    args = parser.parse_args()

    db_file, table_name, output_file = args.db_file, args.table_name, args.output_file

    conn = sqlite3.connect(db_file)
    cursor = conn.cursor()

    dump_table(table_name, output_file, cursor)

    # Check if the database file exists
    if not os.path.exists(args.db_file):
        print(f"Error: Database file '{args.db_file}' not found.")
        sys.exit(1)


if __name__ == "__main__":
    main()