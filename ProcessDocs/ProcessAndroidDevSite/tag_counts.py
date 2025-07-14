import os
from bs4 import BeautifulSoup


def count_html_tags_in_file(filepath):
    """
    Counts the occurrences of each HTML tag in a given file.

    Args:
        filepath (str): The path to the HTML file.

    Returns:
        dict: A dictionary where keys are tag names (str) and values are their counts (int).
              Returns an empty dictionary if the file cannot be read or parsed.
    """
    tag_counts = {}
    try:
        with open(filepath, 'r', encoding='utf-8') as f:
            content = f.read()

        soup = BeautifulSoup(content, 'html.parser')

        # Find all tags and count them
        for tag in soup.find_all(True):  # find_all(True) gets all tags
            tag_name = tag.name
            tag_counts[tag_name] = tag_counts.get(tag_name, 0) + 1

    except FileNotFoundError:
        print(f"Error: File not found at {filepath}")
    except Exception as e:
        print(f"Error processing {filepath}: {e}")

    return tag_counts


def process_html_directory(directory_path):
    """
    Processes all HTML files in a directory and counts their tags.

    Args:
        directory_path (str): The path to the directory containing HTML files.

    Returns:
        dict: A dictionary mapping filenames (str) to their respective tag count dictionaries.
    """
    doc_tags = {}

    if not os.path.isdir(directory_path):
        print(f"Error: Directory not found at {directory_path}")
        return doc_tags

    for filename in os.listdir(directory_path):
        if filename.endswith(('.html', '.htm')):
            filepath = os.path.join(directory_path, filename)
            print(f"Processing {filename}...")
            counts = count_html_tags_in_file(filepath)
            if counts:  # Only add if parsing was successful and counts exist
                doc_tags[filename] = counts

    return doc_tags


if __name__ == "__main__":
    # --- Example Usage ---
    # 1. Create a dummy directory and some HTML files for testing
    html_dir = "archdocs"
    if not os.path.exists(html_dir):
        os.makedirs(html_dir)

    tags_counts_txt = "tag_counts.txt"
    tags_files_txt = "tags_files.txt"

    print("\n--- Starting HTML Tag Counting ---")
    all_html_tag_counts = process_html_directory(html_dir)

    # 3. Print the results
    tags_out = ""
    print("\n--- Final Tag Counts ---")
    for filename, counts in all_html_tag_counts.items():
        tags_out += filename + "\n"
        for tag, count in counts.items():
            tags_out += "<" + tag + ">\t" + str(count) + "\n"
        tags_out += "---\n"

    open(tags_counts_txt, "w").write(tags_out)

    inverted_tag_map = {}
    # Iterate through each file and its tag counts
    for filename, tag_counts in all_html_tag_counts.items():
        # Iterate through each tag and its count within the current file
        for tag_name in tag_counts.keys():
            # If the tag is not yet a key in the inverted map, initialize it with an empty list
            if tag_name not in inverted_tag_map:
                inverted_tag_map[tag_name] = []
            # Add the current filename to the list for this tag
            inverted_tag_map[tag_name].append(filename)

    tag_files_out = ""
    for tag, filenames in inverted_tag_map.items():
        tag_files_out += "<" + tag + ">\n"
        tag_files_out += "\t".join(filenames) + "\n"
        tag_files_out += "---\n"

    open(tags_files_txt, "w").write(tag_files_out)

    # You can also print the full dictionary
    # print("\nFull Dictionary:")
    # import json
    # print(json.dumps(all_html_tag_counts, indent=2))

    # --- Clean up dummy files (optional) ---
    # import shutil
    # shutil.rmtree(test_dir)
    # print(f"\nCleaned up '{test_dir}' directory.")

