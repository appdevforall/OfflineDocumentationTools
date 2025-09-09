import sqlite3
import csv
import sys
import os
import io

def db_tooltips_to_csv(cursor, csv_file_out):
    print("Populating 'NormalizedTooltips' table...")

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

def populate_databases(db_file, csv_file):
    """
    Populates the Tooltips and TooltipButtons tables from a CSV file.

    Args:
        db_file (str): The path to the SQLite database file.
        csv_file (str): The path to the CSV file.
    """
    conn = None
    try:
        conn = sqlite3.connect(db_file)
        cursor = conn.cursor()

        # Create Tooltips table
        cursor.execute('''
            CREATE TABLE IF NOT EXISTS "Tooltips" (
                'id'         INTEGER PRIMARY KEY AUTOINCREMENT,
                'categoryId' INTEGER NOT NULL,  
                'tag'        TEXT NOT NULL,  
                'summary'    TEXT NOT NULL,  
                'detail'     TEXT NOT NULL,  
                UNIQUE ('categoryId', 'tag')
            );
        ''')

        # Create TooltipButtons table
        cursor.execute('''
            CREATE TABLE IF NOT EXISTS TooltipButtons (
                'tooltipId'      INTEGER,
                'buttonNumberId' INTEGER,
                'description'    TEXT,
                'uri'            TEXT
            );
        ''')

        # Read and insert data from the CSV file
        with open(csv_file, 'r', encoding='utf-8') as file:
            reader = csv.DictReader(file)
            for row in reader:
                # Check if a row with the same unique key exists
                cursor.execute(
                    'SELECT id FROM Tooltips WHERE categoryId = ? AND tag = ?',
                    (row['categoryId'], row['tag'])
                )
                existing_row = cursor.fetchone()

                tooltip_id = None
                if existing_row:
                    # If it exists, get the original id and update the row
                    tooltip_id = existing_row[0]
                    cursor.execute(
                        '''UPDATE Tooltips 
                           SET summary = ?, detail = ?
                           WHERE id = ?''',
                        (row['summary'], row['detail'], tooltip_id)
                    )
                else:
                    # If it doesn't exist, insert a new row
                    cursor.execute(
                        'INSERT INTO Tooltips (categoryId, tag, summary, detail) VALUES (?, ?, ?, ?)',
                        (row['categoryId'], row['tag'], row['summary'], row['detail'])
                    )
                    tooltip_id = cursor.lastrowid

                # Insert or replace into TooltipButtons table for each button

                # Update or insert into TooltipButtons for each button
                for i in range(1, 4):
                    description_key = f'description{i}'
                    uri_key = f'uri{i}'

                    description = row.get(description_key)
                    uri = row.get(uri_key)

                    if description and uri:
                        # Check if the button record already exists
                        cursor.execute(
                            'SELECT 1 FROM TooltipButtons WHERE tooltipId = ? AND buttonNumberId = ?',
                            (tooltip_id, i)
                        )
                        button_exists = cursor.fetchone()

                        if button_exists:
                            # If it exists, update the description and uri
                            cursor.execute(
                                '''UPDATE TooltipButtons 
                                   SET description = ?, uri = ?
                                   WHERE tooltipId = ? AND buttonNumberId = ?''',
                                (description, uri, tooltip_id, i)
                            )
                        else:
                            # If it doesn't exist, insert a new record
                            cursor.execute(
                                '''INSERT INTO TooltipButtons (tooltipId, buttonNumberId, description, uri)
                                   VALUES (?, ?, ?, ?)''',
                                (tooltip_id, i, description, uri)
                            )

        conn.commit()
        print("Database populated successfully!")

    except sqlite3.Error as e:
        print(f"An error occurred: {e}")
    finally:
        if conn:
            conn.close()

def main():
    os.system("./clean.sh")
    #conn = sqlite3.connect("documentation.db")
    #cursor = conn.cursor()
    #db_tooltips_to_csv(cursor, "newtest.csv")
    populate_databases("documentation.db", "test.csv")
    #db_tooltips_to_csv(cursor)

    exit(1)

if __name__ == "__main__":
    main()
