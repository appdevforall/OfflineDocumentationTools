import csv
import sys
import os

def check_tooltip_tags_in_file(csv_file_path, text_file_path):
    """
    Checks for the presence of tooltip tags from a CSV file within a text file.

    Args:
        csv_file_path (str): The path to the CSV file containing the tooltip data.
        text_file_path (str): The path to the generic text file to search.
    """
    # Check if files exist
    if not os.path.exists(csv_file_path):
        print(f"Error: CSV file not found at '{csv_file_path}'")
        sys.exit(1)
    if not os.path.exists(text_file_path):
        print(f"Error: Text file not found at '{text_file_path}'")
        sys.exit(1)

    try:
        # Read the entire content of the text file for efficient searching
        with open(text_file_path, 'r', encoding='utf-8') as f:
            text_content = f.read()

        # Process the CSV file
        with open(csv_file_path, 'r', encoding='utf-8') as f:
            csv_reader = csv.reader(f)
            # Skip the header row
            next(csv_reader)

            for row in csv_reader:
                # The tag is the second column (index 1)
                if len(row) > 1:
                    tag = row[2]
                    if tag in text_content:
                        print(f"Tag: {tag} - Found")
                    else:
                        print(f"Tag: {tag} - Not Found")

    except Exception as e:
        print(f"An error occurred: {e}")
        sys.exit(1)

if __name__ == "__main__":
    # Check for the correct number of command-line arguments
    if len(sys.argv) != 3:
        print("Usage: python script.py <tooltip_csv_file> <text_file_to_check>")
        sys.exit(1)

    # Get file paths from command-line arguments
    csv_file = sys.argv[1]
    text_file = sys.argv[2]

    check_tooltip_tags_in_file(csv_file, text_file)