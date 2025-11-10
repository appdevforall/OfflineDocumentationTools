import csv
import io
import os
import sqlite3
import brotli

def generate_placeholder_html_bytes(attr_name):
    html = f"""<html><head><title>Placeholder for {attr_name}</title></head>
    <body>Placeholder for XML layout attribute {attr_name}</body></html>"""

    return html.encode("utf-8")

def process_csv(csv_data: str, tag_column: str) -> dict:
    """
    Reads CSV data and returns a dictionary where each key is the value
    of a specified 'tag' column and the value is a dictionary of the
    entire row's data.

    This function is designed to handle newlines within quoted CSV fields
    by using Python's built-in csv module, which correctly parses them.

    Args:
        csv_data: A string containing the CSV content.
        tag_column: The name of the column to use as the key in the
                    output dictionary.

    Returns:
        A dictionary mapping tag values to row dictionaries.
        Returns an empty dictionary if the tag column is not found.
    """
    output_dict = {}

    try:
        # Use io.StringIO to treat the string as a file, which csv.DictReader can read.
        csv_file = io.StringIO(csv_data)
        
        # Use DictReader to automatically map headers to values for each row.
        reader = csv.DictReader(csv_file)
        
        # Check if the tag column exists in the headers.
        if tag_column not in reader.fieldnames:
            print(f"Error: The tag column '{tag_column}' was not found in the CSV headers.")
            return {}

        # Iterate through each row, which is a dictionary.
        for row in reader:
            tag_value = row.get(tag_column)
            if tag_value is not None:
                # Use the tag value as the key and the full row as the value.
                # The csv module automatically handles quoted values with newlines.
                output_dict[tag_value] = row
            else:
                # Handle cases where the tag value might be missing for a row.
                print(f"Warning: Skipping a row with no value in the '{tag_column}' column.")

    except Exception as e:
        # General error handling for file processing.
        print(f"An error occurred while processing the CSV data: {e}")
        return {}
    
    return output_dict

# The name of the column to use as the key.
tag_column_name = "tag"

# Process the CSV data and get the result.
processed_data = process_csv(open("attrs.csv", "r").read(), tag_column_name)
os.system("./clean.sh")

database_name = "documentation.db"
placeholder_html_out_path = "x"

if not os.path.exists(placeholder_html_out_path):
    os.makedirs(placeholder_html_out_path)

conn = sqlite3.connect(database_name)
cursor = conn.cursor()
# cursor.execute("INSERT INTO TooltipCategories (category) VALUES ('layoutXML') RETURNING id;")
# conn.commit()

tooltip_id = 60000
layout_cat_id = 2

# Print the resulting dictionary in a readable format.
if processed_data:
    print("Successfully processed CSV data:")
    for key, row in processed_data.items():
        tooltip_tag = row["attr_name"]
        tooltip_summary = row["summary"]
        tooltip_detail = ''

        # Insert tooltip
        cursor.execute("""
            INSERT OR REPLACE INTO Tooltips
            (id, categoryId, tag, summary, detail)
            VALUES (?, ?, ?, ?, ?)
            """, (tooltip_id, layout_cat_id, tooltip_tag, tooltip_summary, tooltip_detail))
        conn.commit()

        placeholder_content_path = placeholder_html_out_path + "/" + tooltip_tag + ".html"
        placeholder_html_compressed = brotli.compress(generate_placeholder_html_bytes(tooltip_tag))

        cursor.execute(
            "INSERT INTO Content (path, languageID, content, contentTypeID) VALUES (?, ?, ?, ?)",
            (placeholder_content_path, 1, placeholder_html_compressed, 12))
        conn.commit()

        button_str, button_uri = ("See documentation for XML layout attribute " + tooltip_tag, placeholder_content_path)

        cursor.execute("""
            INSERT OR REPLACE INTO TooltipButtons
            (tooltipId, buttonNumberId, description, uri)
            VALUES (?, ?, ?, ?)
        """, (tooltip_id, 1, button_str, button_uri))
        conn.commit()

        tooltip_id += 1

conn.close()
