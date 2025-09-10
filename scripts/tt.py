import sqlite3
import csv
import sys
import os
import io
import time

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
    cursor = conn.cursor()

    # Get all entries from the main Tooltips table
    cursor.execute("SELECT id, categoryId, tag, summary, detail FROM Tooltips")
    all_tooltips = cursor.fetchall()



    # Iterate over each tooltip and populate the new table
    idx = 0
    data_array = []

    # Insert or update the new table with the combined data
    headers = ['categoryId', 'tag', 'summary', 'detail', 'description1', 'uri1', 'description2', 'uri2', 'description3',
        'uri3']

    idx = 0
    for tooltip_id, category_id, tag, summary, detail in all_tooltips:
        if idx % 100 == 0:
            print(idx)
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

        data_array += [[category_id,
                        tag,summary,
                        detail,
                        button_data.get('description1'),
                        button_data.get('uri1'),
                        button_data.get('description2'),
                        button_data.get('uri2'),
                        button_data.get('description3'),
                        button_data.get('uri3')]]

    with open(csv_file_out, 'w', newline='', encoding='utf-8') as csvfile:
        # Create a writer object.
        writer = csv.writer(csvfile)

        # Write the header row.
        writer.writerow(headers)

        # Write the data rows.
        writer.writerows(data_array)


def csv_to_tooltips(conn, csv_file):
    try:
        cursor = conn.cursor()
        # Drop tables if they exist to ensure a clean slate.
        print("Dropping existing tables...")
        cursor.execute("DROP TABLE IF EXISTS TooltipButtons")
        cursor.execute("DROP TABLE IF EXISTS Tooltips")

        # Create the tables based on the schemas.
        print("Creating database tables...")
        cursor.execute(SCHEMA_TOOLTIPS)
        cursor.execute(SCHEMA_TOOLTIP_BUTTONS)

        # Read data from the CSV file and populate the tables.
        print(f"Reading data from {csv_file}...")
        with open(csv_file, 'r') as csvfile:
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

        # Commit the changes to the database.
        conn.commit()
        print("Database population complete!")
    except sqlite3.Error as e:
        print(f"SQLite error: {e}")
    except FileNotFoundError:
        print(f"Error: The file '{csv_file}' was not found.")
    finally:
        conn.close()


def main():
    os.system("./clean.sh")
    conn = sqlite3.connect("documentation.db")
    db_tooltips_to_csv(conn, "test.csv")
    #csv_to_tooltips(conn, "full.csv")
    #populate_databases("documentation.db", "test.csv")
    #db_tooltips_to_csv(cursor)

    exit(1)

if __name__ == "__main__":
    main()
