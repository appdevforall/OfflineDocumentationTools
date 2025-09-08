import sqlite3
import csv
import sys
import os

def main():
    os.system("./clean.sh")
    conn = sqlite3.connect("documentation.db")
    cursor = conn.cursor()

    cursor.execute('''
               CREATE TABLE IF NOT EXISTS NormalizedTooltips (
                   id INTEGER PRIMARY KEY,
                   categoryId INTEGER,
                   tag TEXT,
                   summary TEXT,
                   detail TEXT,
                   description1 TEXT,
                   uri1 TEXT,
                   description2 TEXT,
                   uri2 TEXT,
                   description3 TEXT,
                   uri3 TEXT
               );
           ''')

    print("Populating 'NormalizedTooltips' table...")

    # Get all entries from the main Tooltips table
    cursor.execute("SELECT id, categoryId, tag, summary, detail FROM Tooltips")
    all_tooltips = cursor.fetchall()


    # Iterate over each tooltip and populate the new table
    for tooltip_id, category_id, tag, summary, detail in all_tooltips:

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

        # Insert or update the new table with the combined data
        cursor.execute('''
                   INSERT INTO NormalizedTooltips (
                       id, categoryId, tag, summary, detail,
                       description1, uri1,
                       description2, uri2,
                       description3, uri3
                   ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
               ''', (
            tooltip_id,
            category_id,
            tag,
            summary,
            detail,
            button_data.get('description1'),
            button_data.get('uri1'),
            button_data.get('description2'),
            button_data.get('uri2'),
            button_data.get('description3'),
            button_data.get('uri3'),
        ))
    cursor.execute(f"SELECT * FROM NormalizedTooltips")
    rows = cursor.fetchall()

    headers = [description[0] for description in cursor.description]
    with open("t.csv", 'w', newline='\n') as csvfile:
        csv_writer = csv.writer(csvfile)
        if headers:
            csv_writer.writerow(headers)
        csv_writer.writerows(rows)
    conn.commit()
    conn.close()

if __name__ == "__main__":
    main()