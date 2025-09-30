import csv
import argparse
import os
import sys
from bs4 import BeautifulSoup, Comment, Tag

# Define the category ID for XML layout attributes
CATEGORY_ID = 2
# Hard-coded detail text
DETAIL_PLACEHOLDER = "Placeholder detail"
# Hard-coded URI for the single 'Learn more' button
DEFAULT_URI = "a/layout/placeholder.html"

# CSV Headers matching the format required by TooltipManager.py
CSV_HEADERS = ['categoryId', 'tag', 'summary', 'detail', 'description1', 'uri1', 'description2', 'uri2', 'description3',
               'uri3']


def extract_comments_and_attrs_bs(xml_file_path):
    """
    Parses the XML file using BeautifulSoup and extracts comments and attribute data.

    Returns a list of dictionaries, where each dictionary represents an <attr>
    tag and its preceding comments.
    """
    print(f"Parsing XML file with BeautifulSoup: {xml_file_path}...")
    try:
        with open(xml_file_path, 'r', encoding='utf-8') as f:
            soup = BeautifulSoup(f, 'xml')  # Use 'xml' parser
    except FileNotFoundError:
        print(f"Error: XML file not found at {xml_file_path}")
        sys.exit(1)

    extracted_data = []

    # Iterate through all <attr> tags
    for attr_tag in soup.find_all('attr'):
        attr_name = attr_tag.get('name')
        if not attr_name:
            continue

        # Find all preceding sibling comments
        comments = []
        raw_comments = []

        # Look backwards through preceding siblings
        for sibling in attr_tag.previous_siblings:
            if isinstance(sibling, Comment):
                # Clean up the comment text
                comment_text = sibling.string.strip()
                if comment_text:
                    comments.append(comment_text)
                    raw_comments.append(f"")
            elif isinstance(sibling, Tag):
                # Stop if we hit a non-comment tag (e.g., <declare-styleable>)
                break
            elif sibling.string and sibling.string.strip():
                # Stop if we hit significant text/whitespace that isn't a comment
                pass  # Continue searching for comments

        # Comments are gathered in reverse order, so reverse them to be in document order
        comments.reverse()
        raw_comments.reverse()

        # Combine comments into the summary string
        comment_summary = '\n\n'.join(comments)

        extracted_data.append({
            'name': attr_name,
            'summary': comment_summary,
            'element': attr_tag,  # Store the BS tag object
            'raw_comments': '\n'.join(raw_comments)  # Keep original comments for disambiguation HTML
        })

    return extracted_data


def generate_attr_tooltips(data, disambiguation_dir):
    """
    Processes the extracted attribute data, handles conflicts, generates tooltips,
    and writes disambiguation files.

    Returns a list of rows for the output CSV file.
    """
    tooltip_data = []

    # 1. Group by attribute name to find conflicts
    attrs_by_name = {}
    for item in data:
        name = item['name']
        if name not in attrs_by_name:
            attrs_by_name[name] = []
        attrs_by_name[name].append(item)

    # 2. Process each attribute name group
    for attr_name, items in attrs_by_name.items():
        tag = f"xml.attr.{attr_name}"
        num_items = len(items)

        # Initialize default button data
        button_desc = f"Learn more about {tag}"
        button_uri = DEFAULT_URI
        final_summary = ""

        if num_items == 1:
            # Case 1: Single <attr> element
            item = items[0]
            final_summary = item['summary']

        else:
            # Case 2: Multiple <attr> elements with the same name
            # A summary is considered non-empty if it contains text after stripping whitespace
            comments_count = sum(1 for item in items if item['summary'].strip())

            if comments_count <= 1:
                # Subcase 2a: Only one or zero instances have comments
                non_empty_summaries = [item['summary'] for item in items if item['summary'].strip()]
                final_summary = non_empty_summaries[0] if non_empty_summaries else ""
                # The button remains the default one
            else:
                # Subcase 2b: Multiple instances have comments (Disambiguation required)
                final_summary = "This attribute may have different meanings in different contexts"

                # Generate Disambiguation HTML file
                html_filename = f"{attr_name}.html"
                html_path = os.path.join(disambiguation_dir, html_filename)

                html_content = f"<html><head><title>{attr_name} Meanings</title></head><body>"
                html_content += f"<h1>Possible Meanings for `android:{attr_name}`</h1>"

                for i, item in enumerate(items, 1):
                    # Clean up the raw comments for display in HTML (remove )
                    clean_comments = item['raw_comments'].replace('', '').strip()
                    html_content += f"<h2>Context {i}</h2>"
                    html_content += f"<p>{clean_comments.replace('\n', '<br>')}</p>"

                html_content += "</body></html>"

                # Ensure disambiguation directory exists
                os.makedirs(disambiguation_dir, exist_ok=True)
                with open(html_path, 'w', encoding='utf-8') as f:
                    f.write(html_content)

                # Update button for the disambiguation link
                button_desc = f"See possible meanings for {attr_name}"
                button_uri = f"a/layout/disambiguation/{html_filename}"

        # 3. Handle <enum> and <flag> children for the final summary
        # Use the first element's children (as in the original script)
        attr_element = items[0]['element']

        enum_names = [e.get('name') for e in attr_element.find_all('enum')]
        flag_names = [f.get('name') for f in attr_element.find_all('flag')]

        if enum_names:
            names_str = "', '".join(enum_names)
            enum_part = f" Possible values are: '{names_str}'"
            # Add to the summary, ensuring a space before it
            final_summary = final_summary.strip() + enum_part

        if flag_names:
            names_str = "', '".join(flag_names)
            flag_part = f" Optional values are: '{names_str}'"
            # Add to the summary, ensuring a space before it
            final_summary = final_summary.strip() + flag_part

        # 4. Create the CSV row
        csv_row = [
            CATEGORY_ID,
            tag,
            final_summary.strip(),
            DETAIL_PLACEHOLDER,
            button_desc,
            button_uri,
            None,  # description2
            None,  # uri2
            None,  # description3
            None  # uri3
        ]

        tooltip_data.append(csv_row)

    return tooltip_data


def write_csv(csv_file_path, data):
    """Writes the list of data rows to the specified CSV file."""
    print(f"Writing data to CSV file: {csv_file_path}")
    with open(csv_file_path, 'w', newline='', encoding='utf-8-sig') as csvfile:
        writer = csv.writer(csvfile)
        writer.writerow(CSV_HEADERS)
        writer.writerows(data)
    print("CSV generation complete!")


def main():
    # Check for BeautifulSoup dependency
    try:
        from bs4 import BeautifulSoup
    except ImportError:
        print("Error: BeautifulSoup is not installed. Please install it with 'pip install beautifulsoup4'.")
        sys.exit(1)

    parser = argparse.ArgumentParser(
        description='Generate tooltip CSV and disambiguation HTML for Android layout attributes using BeautifulSoup.')
    parser.add_argument('--input-xml-file', required=True,
                        help='The path to the input layout attributes XML file.')
    parser.add_argument('--output-csv-file', required=True,
                        help='The path to the output CSV file for this script.')
    parser.add_argument('--disambiguation-dir', required=True,
                        help='The path to the directory where generated disambiguation pages will be placed.')

    args = parser.parse_args()

    try:
        # 1. Extract comments and attributes using BeautifulSoup
        extracted_data = extract_comments_and_attrs_bs(args.input_xml_file)
        print(f"Extracted {len(extracted_data)} attribute definitions.")

        # 2. Generate tooltips, handle conflicts, and create disambiguation files
        tooltip_csv_data = generate_attr_tooltips(extracted_data, args.disambiguation_dir)

        # 3. Write final CSV
        write_csv(args.output_csv_file, tooltip_csv_data)

    except Exception as e:
        print(f"An unexpected error occurred: {e}")
        sys.exit(1)


if __name__ == "__main__":
    main()