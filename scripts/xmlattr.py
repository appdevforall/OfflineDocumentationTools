import argparse
import csv
import os
import sys
import re
from bs4 import BeautifulSoup, Comment, NavigableString, Tag

# --- Configuration ---
TOOLTIP_CSV_HEADERS = [
    'categoryId', 'tag', 'summary', 'detail',
    'description1', 'uri1', 'description2', 'uri2', 'description3', 'uri3'
]
# The category ID for 'xml' is 2, based on your TooltipCategories table.
XML_CATEGORY_ID = 2


def clean_comment(comment_text):
    """
    Cleans up the raw XML comment text for use as a summary.
    """
    # 2. Remove the "eat-comment" marker
    cleaned = re.sub(r'<eat-comment\s*/>', '', comment_text).strip()

    # 3. Replace multiple spaces with a single space
    cleaned = re.sub(r'\s\s+', ' ', cleaned)

    # 4. Ensure it ends with a period if it has content
    if cleaned and not cleaned.endswith('.'):
        cleaned += '.'

    return cleaned if cleaned else "No description available."


def extract_nested_values(attr_tag):
    """
    Extracts nested <enum> and <flag> names from an <attr> tag.
    Returns a tuple: (enum_values_list, flag_values_list)
    """
    enum_values = []
    flag_values = []

    for child in attr_tag.find_all(True, recursive=False):  # find only direct children
        name = child.get('name')
        if name:
            if child.name == 'enum':
                enum_values.append(name)
            elif child.name == 'flag':
                flag_values.append(name)

    return enum_values, flag_values


def generate_tooltip_row(tag_name, summary, enum_values, flag_values):
    """
    Formats the extracted data into a single CSV row.
    """
    # Append enum/flag information to the summary
    final_summary = summary

    if enum_values:
        enum_str = ". Possible values are: " + ", ".join(enum_values) + "."
        final_summary = final_summary.rstrip('.') + enum_str

    if flag_values:
        flag_str = ". Optional values are: " + ", ".join(flag_values) + "."
        final_summary = final_summary.rstrip('.') + flag_str

    # Construct the row
    tooltip_row = [
        XML_CATEGORY_ID,  # categoryId (xml=2)
        f"xml.attr.{tag_name}",  # tag
        final_summary.strip(),  # summary
        "Placeholder detail",  # detail
        "Placeholder tier 3 button",  # description1
        "a/path/to/placeholder.html",  # uri1
        '', '', '', ''  # button2/button3 placeholders
    ]
    return tooltip_row


def bs4_tooltips_to_csv(xml_content, output_csv_path):
    """
    Parses the Android attrs.xml content using BeautifulSoup, extracts attributes,
    removes duplicates, and appends the new tooltip entries to the output CSV file.
    """
    # Use a dictionary for new tooltips, keyed by (categoryId, tag),
    # to automatically handle duplicates.
    unique_tooltips = {}

    # Use 'xml' parser to correctly handle self-closing tags and comments
    soup = BeautifulSoup(xml_content, 'xml')

    # The relevant content is inside the outermost <resources> tag.
    resources_tag = soup.find('resources')
    if not resources_tag:
        print("Error: Could not find the <resources> root tag in the XML.", file=sys.stderr)
        return

    # Use a generator to track the last significant comment found
    last_comment_text = "No description available."

    # Iterate over all direct children of <resources> and nested elements
    for element in resources_tag.descendants:
        if isinstance(element, Comment):
            # When a comment is encountered, clean it and store its text.
            cleaned = clean_comment(element.string)
            if cleaned != "No description available.":
                # Only update the comment if it's substantial (e.g., skips empty comments)
                last_comment_text = cleaned

        elif isinstance(element, Tag):
            # Process only <attr> tags that define an attribute (have a 'name' attribute)
            if element.name == 'attr' and 'name' in element.attrs:
                attr_name = element.get('name')

                # 1. Extract nested enum/flag values
                enum_values, flag_values = extract_nested_values(element)

                # 2. Generate the CSV row
                row = generate_tooltip_row(attr_name, last_comment_text, enum_values, flag_values)

                # 3. Use (categoryId, tag) as the unique key to prevent duplicates
                tag = row[1]
                key = (XML_CATEGORY_ID, tag)

                # Store or overwrite the entry (only the last one for a given tag is kept)
                unique_tooltips[key] = row

                # 4. Reset the comment after use so subsequent attrs without
                #    a preceding comment get a fresh default message.
                last_comment_text = "No description available."

            # Resetting for structural elements like <declare-styleable> is now unnecessary
            # because the reset happens only after an <attr> tag is processed.

    # Convert the dictionary values (the unique rows) back into a list.
    final_tooltips = list(unique_tooltips.values())

    # --- CSV Writing Logic ---
    try:
        # Check if the file exists and is not empty to determine if headers are needed
        file_exists = os.path.exists(output_csv_path)
        is_empty = not file_exists or os.path.getsize(output_csv_path) == 0

        with open(output_csv_path, 'a', newline='', encoding='utf-8-sig') as csvfile:
            writer = csv.writer(csvfile)

            if is_empty:
                print(f"Creating new CSV file with headers: {output_csv_path}")
                writer.writerow(TOOLTIP_CSV_HEADERS)
            else:
                print(f"Appending {len(final_tooltips)} unique tooltips to existing CSV file: {output_csv_path}")

            writer.writerows(final_tooltips)

        print(f"Successfully added {len(final_tooltips)} unique tooltips to {output_csv_path}.")

    except IOError as e:
        print(f"Error writing to CSV file {output_csv_path}: {e}", file=sys.stderr)
        sys.exit(1)


def main():
    """Main function to handle command-line arguments and file processing."""
    parser = argparse.ArgumentParser(
        description="Extracts Android layout attribute tooltips from attrs.xml using BeautifulSoup and appends them to a CSV file."
    )
    parser.add_argument('--xml-file', required=True,
                        help='The path to the Android attrs.xml file to parse.')
    parser.add_argument('--output-csv', required=True,
                        help='The path to the tooltip CSV file to append to.')

    args = parser.parse_args()

    # Read the entire XML content first
    try:
        with open(args.xml_file, 'r', encoding='utf-8') as f:
            xml_content = f.read()
    except FileNotFoundError:
        print(f"Error: XML file not found at '{args.xml_file}'", file=sys.stderr)
        sys.exit(1)
    except Exception as e:
        print(f"An error occurred while reading the XML file: {e}", file=sys.stderr)
        sys.exit(1)

    bs4_tooltips_to_csv(xml_content, args.output_csv)


if __name__ == "__main__":
    main()