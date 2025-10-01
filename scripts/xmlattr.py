import argparse
import csv
import os
from bs4 import BeautifulSoup, Comment
from collections import defaultdict
import shutil

# --- Configuration Constants ---
CATEGORY_ID = 2
DETAIL_PLACEHOLDER = "Placeholder detail"
URI_PLACEHOLDER = "a/layout/placeholder.html"
DISAMBIGUATION_URI_BASE = "a/layout/disambiguation/"
TOOLTIP_HEADERS = [
    'categoryId', 'tag', 'summary', 'detail',
    'description1', 'uri1',
    'description2', 'uri2',
    'description3', 'uri3'
]


# --- Core Functions ---

def extract_comments_before_attr(attr_tag):
    """
    Extracts and combines all preceding XML comments for an <attr> tag.
    Returns the cleaned, combined comment text.
    """
    comments = []
    # Loop backward through the tag's previous siblings
    for sibling in attr_tag.previous_siblings:
        # Check if the sibling is a BeautifulSoup Comment object
        if isinstance(sibling, Comment):
            # Clean up the comment text: strip XML comment delimiters and leading/trailing whitespace
            clean_comment = sibling.strip()
            if clean_comment:
                # Insert at the beginning to maintain original document order
                comments.insert(0, clean_comment)
        # Stop at the first non-comment, non-whitespace sibling
        elif sibling.name or str(sibling).strip():
            break

    # Combine all comments with a newline separator, then remove all internal newlines
    # and collapse excessive whitespace for a clean string.
    combined_summary = '\n'.join(comments)
    # Remove newlines and excess whitespace, then strip leading/trailing space
    combined_summary = ' '.join(combined_summary.split()).strip()

    return combined_summary


def get_enum_or_flag_summary(attr_tag):
    """
    Generates the 'Possible values' or 'Optional values' string for <enum> or <flag> children.
    """
    # Check for <enum> children
    enums = attr_tag.find_all('enum', recursive=False)
    if enums:
        names = [e.get('name') for e in enums if e.get('name')]
        if names:
            names_quoted = [f"'{n}'" for n in names]
            return f" Possible values are: {', '.join(names_quoted)}"

    # Check for <flag> children
    flags = attr_tag.find_all('flag', recursive=False)
    if flags:
        names = [f.get('name') for f in flags if f.get('name')]
        if names:
            names_quoted = [f"'{n}'" for n in names]
            return f" Optional values are: {', '.join(names_quoted)}"

    return ""


def generate_disambiguation_page(attr_name, summaries, disambiguation_dir):
    """
    Generates a simple HTML file for an attribute with multiple conflicting summaries.
    """
    filename = f"{attr_name}.html"
    filepath = os.path.join(disambiguation_dir, filename)

    html_content = f"""
<!DOCTYPE html>
<html>
<head>
    <title>Disambiguation for '{attr_name}'</title>
</head>
<body>
    <h1>Possible Meanings for Android Attribute: <code>{attr_name}</code></h1>
    <p>This attribute has different meanings depending on the specific Android view or context it's applied to. Below are the possible definitions found in the documentation:</p>
    <ul>
"""
    for summary in summaries:
        html_content += f"        <li>{summary}</li>\n"

    html_content += """
    </ul>
    <p>Please refer to the full Android documentation for context-specific details.</p>
</body>
</html>
"""
    os.makedirs(disambiguation_dir, exist_ok=True)
    with open(filepath, 'w', encoding='utf-8') as f:
        f.write(html_content.strip())

    return f"{DISAMBIGUATION_URI_BASE}{filename}"


def process_xml_to_tooltip_data(xml_file_path, disambiguation_dir):
    """
    The main processing logic. Groups attributes by name and resolves conflicts.
    Returns a list of final tooltip rows and a list of disambiguation files to create.
    """
    print(f"Reading XML file: {xml_file_path}")
    with open(xml_file_path, 'r', encoding='utf-8') as f:
        # The XML is likely not well-formed for a standard parser, so we use 'html.parser'
        # which is more forgiving, especially with leading comments and fragments.
        soup = BeautifulSoup(f, 'html.parser')

    # Group all <attr> tags by their 'name' attribute
    # { 'attr_name': [ (tag, summary_text), ... ], ... }
    attr_groups = defaultdict(list)
    for attr_tag in soup.find_all('attr'):
        attr_name = attr_tag.get('name')
        if not attr_name:
            continue

        # 1. Extract and clean the preceding comments
        raw_summary = extract_comments_before_attr(attr_tag)

        # 2. Append enum/flag details to the summary
        enum_flag_summary = get_enum_or_flag_summary(attr_tag)
        final_summary = raw_summary + enum_flag_summary

        # Only consider entries with *some* documentation for grouping
        if final_summary:
            attr_groups[attr_name].append((attr_tag, final_summary))
        else:
            # If no comments but tag exists, we still process it to create a barebones entry
            # only if no other entries exist for this name. This case is handled in the
            # final generation loop below.
            pass

    final_tooltip_data = []

    # Process each attribute group to resolve conflicts (disambiguation)
    for attr_name, entries in attr_groups.items():
        # Tags for the same attribute name, grouped by unique summary text
        unique_summaries = {}
        for _, summary in entries:
            unique_summaries[summary] = unique_summaries.get(summary, 0) + 1

        num_unique_summaries = len(unique_summaries)

        # --- Default Tooltip Values ---
        tag = f"xml.attr.{attr_name}"
        detail = DETAIL_PLACEHOLDER
        # Buttons default to standard 'Learn more' link
        desc1 = f"Learn more about {attr_name}"
        uri1 = URI_PLACEHOLDER
        desc2, uri2, desc3, uri3 = "", "", "", ""

        # --- CONFLICT RESOLUTION LOGIC ---
        if num_unique_summaries == 1:
            # Case 1: Multiple tags, but only one unique summary string.
            # Use that single, unique summary.
            summary = list(unique_summaries.keys())[0]

        elif num_unique_summaries > 1:
            # Case 2: Multiple tags with multiple unique summaries (CONFLICT!)
            # 1. Set the main summary to the conflict placeholder
            summary = "This attribute may have different meanings in different contexts"

            # 2. Generate the disambiguation HTML page
            unique_summary_list = list(unique_summaries.keys())
            disambiguation_uri = generate_disambiguation_page(
                attr_name, unique_summary_list, disambiguation_dir
            )

            # 3. Overwrite the first button with the disambiguation link
            desc1 = f"See possible meanings for {attr_name}"
            uri1 = disambiguation_uri

        else:
            # Case 3: Tags exist, but none of them had comments (highly unlikely for the first entry)
            # Fallback to a barebones entry if no other entries exist.
            if not entries:
                summary = f"Documentation for '{attr_name}' is not currently available."
            else:
                # Should not be reached if attr_groups is built correctly, but safety first
                continue

        # Append the final row data
        final_tooltip_data.append([
            CATEGORY_ID, tag, summary, detail,
            desc1, uri1, desc2, uri2, desc3, uri3
        ])

    print(f"Processed {len(final_tooltip_data)} unique attribute tooltips.")
    return final_tooltip_data


def write_csv(data, output_csv_file):
    """
    Writes the list of tooltip data to the output CSV file.
    """
    print(f"Writing data to CSV file: {output_csv_file}")
    # Use 'utf-8-sig' to ensure compatibility with Excel/TooltipManager.py's reading
    with open(output_csv_file, 'w', newline='', encoding='utf-8-sig') as csvfile:
        writer = csv.writer(csvfile)
        writer.writerow(TOOLTIP_HEADERS)
        writer.writerows(data)
    print("CSV file generation complete!")


# --- Main Execution ---

def main():
    parser = argparse.ArgumentParser(
        description='Parses Android layout attribute XML to generate a CSV file for tooltips.',
        formatter_class=argparse.RawTextHelpFormatter
    )

    parser.add_argument('--input-xml-file', required=True,
                        help='The path to the input layout attributes XML file to process.')
    parser.add_argument('--output-csv-file', required=True,
                        help='The path to the output CSV file for this script.')
    parser.add_argument('--disambiguation-dir', required=True,
                        help='The path to the directory where this script will be placing the generated disambiguation pages.')

    args = parser.parse_args()

    # Clear and recreate the disambiguation directory to ensure a clean build
    if os.path.exists(args.disambiguation_dir):
        shutil.rmtree(args.disambiguation_dir)
    os.makedirs(args.disambiguation_dir, exist_ok=True)
    print(f"Disambiguation directory set up at: {args.disambiguation_dir}")

    # 1. Process the XML and get the data
    tooltip_data = process_xml_to_tooltip_data(args.input_xml_file, args.disambiguation_dir)

    # 2. Write the CSV
    write_csv(tooltip_data, args.output_csv_file)


if __name__ == "__main__":
    main()