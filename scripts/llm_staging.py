import sqlite3
import csv
import sys
import os


def dump_tooltips_to_csv(db_file, table_name):
    """
    Connects to a SQLite database and dumps the contents of a specified table
    into a TSV file named after the table.

    Args:
        db_file (str): The path to the SQLite database file.
        table_name (str): The name of the table to dump.
    """
    conn = None
    try:
        # Create a connection to the database
        conn = sqlite3.connect(db_file)
        cursor = conn.cursor()

        # Check if the table exists to prevent SQL injection and errors
        cursor.execute(f"SELECT name FROM sqlite_master WHERE type='table' AND name='{table_name}'")
        if not cursor.fetchone():
            print(f"Error: Table '{table_name}' does not exist in the database.")
            return

        # Build the SELECT query
        select_query = f"SELECT * FROM {table_name}"
        cursor.execute(select_query)

       #  # Get column headers from the cursor description
        column_headers = [desc[0] for desc in cursor.description] + ["desc_1", "uri_1", "desc_2", "uri_2", "desc_3", "uri_3"]

        outtext = "\t".join(column_headers) + "\n"

        # Fetch all rows from the table
        rows = cursor.fetchall()

        # lim = 0

        tooltips = []


        c = 0
        print(len(rows))
        for row in rows:
            if c % 100 == 0:
                print(c)
            #if lim > 10:
            #    break
            row_lenth = len(row)
            button_row = [str(i) for i in list(row)] + [""]*6

            cursor.execute("SELECT tooltipId, buttonNumberId,description,uri FROM Tooltipbuttons where tooltipId = ?", (row[0],))

            tooltipbuttons = sorted(cursor.fetchall(), key=lambda x: x[1])

            for idx, button in enumerate(tooltipbuttons):
                desc = button[2]
                uri = button[3]

                button_row[row_lenth + (2 * idx)] = desc
                button_row[row_lenth + (2 * idx) + 1] = uri

            tooltips += [button_row]

                # print(",".join(button_row))
            # lim += 1
            line_out = ["\"" + i + "\"" for i in button_row if i not in ["id, categ"]]
            outtext += str("|".join(button_row)) + "\n"
            c += 1
        open("tooltips_sep8.tsv", "w").write(outtext)

    except sqlite3.Error as e:
        print(f"SQLite error: {e}")

    except IOError as e:
        print(f"File I/O error: {e}")

    finally:
        # Close the database connection if it was successfully opened
        if conn:
            conn.close()

def main():
    # Check for the correct number of command-line arguments
    if len(sys.argv) != 3:
        print("Usage: python dump_table_to_tsv.py <database_file.db> <table_name>")
        sys.exit(1)

    database_file = sys.argv[1]
    table = sys.argv[2]

    # Check if the database file exists
    if not os.path.exists(database_file):
        print(f"Error: Database file '{database_file}' not found.")
        sys.exit(1)

    dump_tooltips_to_csv(database_file, table)

if __name__ == "__main__":
    main()