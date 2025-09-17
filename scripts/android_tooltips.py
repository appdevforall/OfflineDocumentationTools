from bs4 import BeautifulSoup
import os
import json
import re
import time
import argparse
import pickle
from tqdm_loggable.auto import tqdm
import logging
from android_html_page import AndroidHtmlPage


def generate_tooltip_tag(class_name, class_url):
    """
    Generate the correct tooltipTag for a class based on its URL.

    Args:
        class_name (str): The class name from the HTML link
        class_url (str): The URL path for the class

    Returns:
        str: The tooltipTag with appropriate prefix
    """
    if class_url.startswith('/reference/android/'):
        # For android classes, add 'android.' prefix
        # Remove '/reference/android/' prefix and convert path to package notation
        package_path = class_url[18:]  # Remove '/reference/android/'
        if package_path.startswith('/'):
            package_path = package_path[1:]
        package_dots = package_path.replace('/', '.')
        return f"android.{package_dots}"
    elif class_url.startswith('/reference/androidx/'):
        # For android classes, add 'android.' prefix
        # Remove '/reference/android/' prefix and convert path to package notation
        package_path = class_url[19:]  # Remove '/reference/android/'
        if package_path.startswith('/'):
            package_path = package_path[1:]
        package_dots = package_path.replace('/', '.')
        return f"androidx.{package_dots}"
    else:
        # For androidx and other classes, just use the class name
        return class_name


def analyze_classes(directory_path):
    """
    Parse classes.html to create a mapping of class names to their descriptions and URLs.
    Handles the Android documentation format with tables containing class links and descriptions.

    Args:
        directory_path (str): Path to the directory containing classes.html

    Returns:
        dict: Mapping of class names to (description, url) tuples
    """
    class_map = {}
    classes_path = os.path.join(directory_path, 'classes.html')

    with open(classes_path, 'r', encoding='utf-8') as f:
        soup = BeautifulSoup(f.read(), 'html.parser')

        # Find all tables in the document
        tables = soup.find_all('table')

        for table in tables:
            # Find all rows in the table
            rows = table.find_all('tr')

            for row in rows:
                # Skip rows without the data-version-added attribute (likely headers)
                if not row.get('data-version-added'):
                    continue

                # Find the link column and description column
                link_cell = row.find('td', class_='jd-linkcol')
                desc_cell = row.find('td', class_='jd-descrcol')

                if link_cell and desc_cell:
                    # Extract the link
                    link = link_cell.find('a')
                    if link and 'href' in link.attrs:
                        class_url = link['href']
                        class_name = link.get_text(strip=True)

                        # Filter: only include classes from /reference/android or /reference/androidx
                        if not (class_url.startswith('/reference/android/') or class_url.startswith(
                                '/reference/androidx/')):
                            continue

                        # Extract the description text
                        description = desc_cell.get_text(strip=True)

                        # Clean up the description - remove extra whitespace and &nbsp;
                        description = re.sub(r'\s+', ' ', description)
                        description = description.replace('&nbsp;', ' ').strip()

                        class_map[class_name] = (description, class_url)

    return class_map


def analyze_androidx_classes(directory_path):
    """
    Parse androidx classes.html to create a mapping of class names to their descriptions and URLs.
    Handles the AndroidX documentation format with tables containing class links and descriptions.

    Args:
        directory_path (str): Path to the directory containing androidx/classes.html

    Returns:
        dict: Mapping of class names to (description, url) tuples
    """
    class_map = {}
    classes_path = directory_path

    if not os.path.exists(classes_path):
        logging.warning(f"androidx classes.html not found at: {classes_path}")
        return class_map

    with open(classes_path, 'r', encoding='utf-8') as f:
        soup = BeautifulSoup(f.read(), 'html.parser')

        # Find all table rows in the document
        rows = soup.find_all('tr')

        for row in rows:
            # Find the link column and description column
            cells = row.find_all('td')
            if len(cells) < 2:
                continue

            # First cell contains the link
            link_cell = cells[0]
            # Second cell contains the description
            desc_cell = cells[1]

            # Extract the link from the first cell
            link = link_cell.find('a')
            if link and 'href' in link.attrs:
                class_url = link['href']
                # class_name = link.get_text(strip=True)
                base_class_name = link.get_text()
                class_name = generate_tooltip_tag(base_class_name, class_url)

                # Filter: only include classes from /reference/androidx
                if not (class_url.startswith('/reference/android/') or class_url.startswith('/reference/androidx/')):
                    continue

                # Extract the description text from the second cell
                description = desc_cell.get_text()

                # Clean up the description - remove extra whitespace
                description = re.sub(r'\s+', ' ', description)
                description = description.replace('&nbsp;', ' ').strip()

                class_map[class_name] = (description, class_url)

    return class_map


def get_detail(file_path):
    """
    Extract the detailed description from the Android HTML file.

    Args:
        file_path (str): Path to the HTML file to parse

    Returns:
        str: The detailed description or empty string if not found
    """
    if not os.path.exists(file_path):
        logging.warning(f"File does not exist for get_detail: {file_path}")
        return "No detail found - file missing"

    try:
        with open(file_path, 'r', encoding='utf-8') as f:
            soup = BeautifulSoup(f.read(), 'html.parser')

            # Look for the description section in Android docs
            desc_section = soup.find('div', class_='jd-details-descr')
            if desc_section:
                # Get all content within the description section
                content = []
                for element in desc_section.children:
                    if element.name:
                        content.append(str(element))
                return str(BeautifulSoup(''.join(content), 'html.parser'))

            # Fallback: look for any description-like content
            desc_divs = soup.find_all('div', class_=lambda x: x and 'descr' in x)
            if desc_divs:
                return str(desc_divs[0])
    except Exception as e:
        logging.error(f"Error reading file {file_path}: {str(e)}")
        return f"No detail found - error reading file: {str(e)}"

    return "No detail found"


def normalize_button_uri(file_path, button_uri):
    """
    Normalize a button URI by converting relative paths to absolute URLs for Android docs.

    Args:
        file_path (str): The full path to the current HTML file
        button_uri (str): The relative URI from the button

    Returns:
        str: The normalized absolute URL
    """
    # If it's already an absolute URL, return as is
    if button_uri.startswith('http'):
        return button_uri

    # For Android docs, convert relative paths to absolute localhost URLs
    if button_uri.startswith('/reference/'):
        # Remove the /reference/ prefix and construct the path
        clean_uri = button_uri[10:]  # Remove '/reference/' (10 characters)
        # Ensure no leading slash to avoid double slashes
        if clean_uri.startswith('/'):
            clean_uri = clean_uri[1:]  # Remove leading '/'
        return f"http://localhost:6174/AndroidDocs/{clean_uri}.html"

    # For other relative paths, try to construct based on the current file location
    # Extract the android path from the file_path
    # Example: /path/to/SourceDocs/AndroidDocs/API/Classes/developer.android.com/reference/android/widget/AbsListView.html
    # We want to extract: android/widget/AbsListView.html

    # Find the reference directory in the path
    ref_index = file_path.find('/reference/')
    if ref_index != -1:
        # Extract everything after /reference/
        android_path = file_path[ref_index + 10:]  # Skip '/reference/'
        # Remove .html extension if present
        if android_path.endswith('.html'):
            android_path = android_path[:-5]
        # Ensure no leading slash to avoid empty string in split
        if android_path.startswith('/'):
            android_path = android_path[1:]

        # Count the number of parent directory references
        parent_refs = button_uri.count('../')

        # Remove parent directory references from the button URI
        clean_uri = button_uri.replace('../', '')

        # Split the android path and remove the appropriate number of directories
        path_parts = android_path.split('/')
        if parent_refs > 0:
            path_parts = path_parts[:-parent_refs]

        # Construct the final path
        final_path = '/'.join(path_parts)

        # Ensure proper path construction without double slashes
        if final_path:
            # Ensure clean_uri doesn't start with a slash
            if clean_uri.startswith('/'):
                clean_uri = clean_uri[1:]
            return f"http://localhost:6174/AndroidDocs/{final_path}/{clean_uri}"
        else:
            # Ensure clean_uri doesn't start with a slash
            if clean_uri.startswith('/'):
                clean_uri = clean_uri[1:]
            return f"http://localhost:6174/AndroidDocs/{clean_uri}"

    return button_uri


def get_buttons(file_path):
    """
    Extract navigation buttons from the Android HTML file header.

    Args:
        file_path (str): Path to the HTML file to parse

    Returns:
        list: List of button tuples (label, url) for navigation links
    """
    buttons = []

    if not os.path.exists(file_path):
        logging.warning(f"File does not exist for get_buttons: {file_path}")
        return buttons

    try:
        with open(file_path, 'r', encoding='utf-8') as f:
            soup = BeautifulSoup(f.read(), 'html.parser')

            # Look for breadcrumb navigation or package links
            breadcrumbs = soup.find_all('a', href=True)
            for link in breadcrumbs:
                href = link.get('href')
                text = link.get_text(strip=True)

                # Look for package or class references
                if href and text and ('package' in href.lower() or 'class' in href.lower()):
                    normalized_url = normalize_button_uri(file_path, href)
                    buttons.append([f"View {text}", normalized_url])

        # Add self-referential button
        rel_path = os.path.relpath(file_path, os.path.dirname(file_path))
        self_url = normalize_button_uri(file_path, rel_path)
        buttons.append(["View full documentation", self_url])
    except Exception as e:
        logging.error(f"Error reading file {file_path}: {str(e)}")

    return buttons


def get_doc_type(file_path):
    """
    Extract the documentation type (class/module/package) and tag from an Android HTML file.

    Args:
        file_path (str): Path to the HTML file to parse

    Returns:
        tuple: (type, tag) where type is the documentation type (class/module/package) or 'unknown',
              and tag is the package/module path without the last component
    """
    if not os.path.exists(file_path):
        logging.warning(f"File does not exist for get_doc_type: {file_path}")
        return ('unknown', 'android')

    try:
        with open(file_path, 'r', encoding='utf-8') as f:
            soup = BeautifulSoup(f.read(), 'html.parser')

            # Look for title or heading that indicates the type
            title = soup.find('title')
            if title:
                title_text = title.get_text()
                if 'Class' in title_text:
                    return ('class', 'android')
                elif 'Package' in title_text:
                    return ('package', 'android')
                elif 'Module' in title_text:
                    return ('module', 'android')

            # Look for breadcrumb navigation to determine type
            breadcrumbs = soup.find_all('a', href=True)
            for link in breadcrumbs:
                href = link.get('href')
                if href and 'package' in href:
                    return ('package', 'android')
                elif href and 'class' in href:
                    return ('class', 'android')
    except Exception as e:
        logging.error(f"Error reading file {file_path}: {str(e)}")

    return ('unknown', 'android')


# ... (rest of the file remains the same)


# ... (rest of the file remains the same)

def get_all_file_data(file_path, truncate_len=500):
    """
    Extract all data from an Android HTML file in a single read operation.

    Args:
        file_path (str): Path to the HTML file to parse

    Returns:
        tuple: (detail, buttons, doc_type, tag) where:
            - detail is the detailed description
            - buttons is a list of button tuples (label, url)
            - doc_type is the documentation type (class/module/package)
            - tag is the package/module path
    """
    if not os.path.exists(file_path):
        logging.warning(f"File does not exist for get_all_file_data: {file_path}")
        return ("No detail found - file missing", [], 'unknown', 'android')

    try:
        with open(file_path, 'r', encoding='utf-8') as f:
            soup = BeautifulSoup(f.read(), 'html.parser')


            detail = ""

            hr_tag = soup.find('hr')
            hr_next = None
            if hr_tag:
                hr_next = hr_tag.find_next("hr")
            # Find the devsite-code block which contains the class signature
            if hr_next:
                p_tag = hr_next.find_next_sibling('p')
                if p_tag:
                    detail = p_tag.get_text()
            elif hr_tag and not hr_next:
                p_tag = hr_tag.find_next_sibling('p')
                if p_tag:
                    detail = p_tag.get_text()
            else:
                article_body = soup.find('div', class_='devsite-article-body')
                if article_body:
                    p_tag = article_body.find('p')
                    if p_tag:
                        detail = p_tag.get_text()

            detail = re.sub(r'\s+', ' ', detail)
            detail = detail.replace('&nbsp;', ' ').strip()
            detail = detail.replace(r"\'", "'")
            if len(detail) > truncate_len:
                detail = detail[:truncate_len] + "..."

            # Only include a button to the exact page for the specific class
            # Construct the button URL based on the class URL pattern
            ref_index = file_path.find('/reference/')
            if ref_index != -1:
                # Extract everything after /reference/
                android_path = file_path[ref_index + 10:]  # Skip '/reference/' (10 characters)
                # Remove .html extension if present
                if android_path.endswith('.html'):
                    android_path = android_path[:-5]
                # Ensure no leading slash to avoid double slashes
                if android_path.startswith('/'):
                    android_path = android_path[1:]
                # Construct the button URL
                self_url = f"http://localhost:6174/AndroidDocs/{android_path}.html"
            else:
                # Fallback: use the old method
                rel_path = os.path.relpath(file_path, os.path.dirname(file_path))
                self_url = normalize_button_uri(file_path, rel_path)

            buttons = [["View full documentation", self_url]]

            # Extract doc type - simplified
            doc_type = 'class'  # Default to class since we're processing classes

            return (detail, buttons, doc_type, 'android')

    except Exception as e:
        logging.error(f"Error reading file {file_path}: {str(e)}")
        return (f"No detail found - error reading file: {str(e)}", [], 'unknown', 'android')


def traverse(directory_path, output_path, output_root=None, debug_mode=False):
    """
    Traverse the Android documentation directory and create tooltip records.

    Args:
        directory_path (str): Path to the Android documentation directory
        output_path (str): Path to the output pickle file
        output_root (str): Root directory for output HTML files (optional)
        debug_mode (bool): If True, only process the first 10 items
    """
    # First, analyze the android classes.html to get class mappings
    android_class_map =  analyze_androidx_classes(os.path.join(directory_path, 'android', 'classes.html'))

    # Then, analyze the androidx classes.html to get class mappings
    androidx_class_map = analyze_androidx_classes(os.path.join(directory_path, 'androidx', 'classes.html'))

    # Combine both class maps
    class_map = {**android_class_map, **androidx_class_map}

    # In debug mode, limit to first 10 items
    if debug_mode:
        class_map = dict(list(class_map.items())[:10])
        print(
            f"DEBUG MODE: Processing only first 10 items out of {len(android_class_map) + len(androidx_class_map)} total")
        print(f"  Android classes: {len(android_class_map)}")
        print(f"  AndroidX classes: {len(androidx_class_map)}")

    # Initialize output directory
    os.makedirs(output_root, exist_ok=True)
    print(f"Output directory initialized: {output_root}")

    # Initialize tooltip records dictionary
    tooltip_records = {}
    processed_count = 0
    error_count = 0

    print(f"Found {len(android_class_map)} Android classes and {len(androidx_class_map)} AndroidX classes")
    print(f"Total classes to process: {len(class_map)}")
    """
    # Process Android classes
    for class_name, (description, class_url) in tqdm(android_class_map.items(), desc="Processing Android classes"):
        try:
            # Construct the full file path
            if class_url.startswith('/reference/'):
                # Remove the /reference/ prefix and any leading slash, then add .html extension
                relative_path = class_url[10:]  # Remove '/reference/' (10 characters)
                if relative_path.startswith('/'):
                    relative_path = relative_path[1:]
                relative_path += '.html'
                full_path = os.path.join(directory_path, relative_path)
            else:
                full_path = os.path.join(directory_path, class_url + '.html')

            # Check if the file exists at the expected path
            if not os.path.exists(full_path):
                print(f"Skipping missing file for class '{class_name}': {full_path}")
                logging.warning(f"File not found for class '{class_name}': {full_path}")
                error_count += 1
                continue

            # Use the single get_all_file_data function for both Android and AndroidX
            detail, buttons, doc_type, tag = get_all_file_data(full_path)

            if description == detail:
                detail = ""

            button_data = []
            for button in buttons[:3]:  # Limit to 3 buttons
                if len(button) >= 2:
                    button_data.append({
                        "first": button[0],
                        "second": button[1]
                    })
            button_json = json.dumps(button_data)

            # Generate tooltipTag
            tooltip_tag = generate_tooltip_tag(class_name, class_url)

            # Add to tooltip records dictionary
            tooltip_records[tooltip_tag] = {
                'tooltipSummary': description,
                'tooltipDetail': detail,
                'tooltipButtons': button_json
            }

            # Create AndroidHtmlPage and write to filesystem
            try:
                android_page = AndroidHtmlPage(full_path)
                html_content = android_page.get_html_page()
                if html_content and not html_content.strip().startswith(
                        "<html><head><title>Class Documentation</title></head><body><p>File not found"):
                    # Calculate the relative path from the input directory
                    rel_path = os.path.relpath(full_path, directory_path)
                    # Construct the HTML output path
                    html_output_path = os.path.join(output_root, rel_path)
                    # Create the output directory if it doesn't exist
                    output_dir = os.path.dirname(html_output_path)
                    os.makedirs(output_dir, exist_ok=True)
                    # Write the cleaned HTML content to the filesystem
                    with open(html_output_path, 'w', encoding='utf-8') as f:
                        f.write(html_content)
                    logging.debug(f"Wrote cleaned HTML to: {html_output_path}")
            except Exception as e:
                logging.warning(f"Failed to write HTML content for {class_name}: {str(e)}")

            processed_count += 1

        except Exception as e:
            logging.error(f"Error processing {class_name}: {str(e)}")
            error_count += 1
            continue
        """


    # Process AndroidX classes
    for class_name, (description, class_url) in tqdm(androidx_class_map.items(), desc="Processing AndroidX classes"):
        try:
            # Construct the full file path
            if class_url.startswith('/reference/'):
                # Remove the /reference/ prefix and any leading slash, then add .html extension
                relative_path = class_url[10:]  # Remove '/reference/' (10 characters)
                if relative_path.startswith('/'):
                    relative_path = relative_path[1:]
                relative_path += '.html'
                full_path = os.path.join(directory_path, relative_path)
            else:
                full_path = os.path.join(directory_path, class_url + '.html')

            # Check if the file exists at the expected path
            if not os.path.exists(full_path):
                print(f"Skipping missing file for class '{class_name}': {full_path}")
                logging.warning(f"File not found for class '{class_name}': {full_path}")
                error_count += 1
                continue

            # Use the single get_all_file_data function for both Android and AndroidX
            detail, buttons, doc_type, tag = get_all_file_data(full_path)

            if description == detail:
                detail = ""

            button_data = []
            for button in buttons[:3]:  # Limit to 3 buttons
                if len(button) >= 2:
                    button_data.append({
                        "first": button[0],
                        "second": button[1]
                    })
            button_json = json.dumps(button_data)

            # Generate tooltipTag
            tooltip_tag = generate_tooltip_tag(class_name, class_url)

            # Add to tooltip records dictionary
            tooltip_records[tooltip_tag] = {
                'tooltipSummary': description,
                'tooltipDetail': detail,
                'tooltipButtons': button_json
            }

            # Create AndroidHtmlPage and write to filesystem
            try:
                android_page = AndroidHtmlPage(full_path)
                html_content = android_page.get_html_page()
                if html_content and not html_content.strip().startswith(
                        "<html><head><title>Class Documentation</title></head><body><p>File not found"):
                    # Calculate the relative path from the input directory
                    rel_path = os.path.relpath(full_path, directory_path)
                    # Construct the HTML output path
                    html_output_path = os.path.join(output_root, rel_path)
                    # Create the output directory if it doesn't exist
                    output_dir = os.path.dirname(html_output_path)
                    os.makedirs(output_dir, exist_ok=True)
                    # Write the cleaned HTML content to the filesystem
                    with open(html_output_path, 'w', encoding='utf-8') as f:
                        f.write(html_content)
                    logging.debug(f"Wrote cleaned HTML to: {html_output_path}")
            except Exception as e:
                logging.warning(f"Failed to write HTML content for {class_name}: {str(e)}")

            processed_count += 1

        except Exception as e:
            logging.error(f"Error processing {class_name}: {str(e)}")
            error_count += 1
            continue

    # Save the tooltip records to pickle file
    try:
        with open(output_path, 'wb') as f:
            pickle.dump(tooltip_records, f)
        open("androidx_tooltips.json", "w").write(json.dumps(tooltip_records, indent=4))
        print(f"Tooltip records saved to: {output_path}")
        print(f"Total records: {len(tooltip_records)}")
    except Exception as e:
        print(f"Error saving tooltip records: {str(e)}")
        raise

    print(f"Processing completed. Processed: {processed_count}, Errors: {error_count}")


def main():
    """
    Main function to process Android documentation and create tooltips.
    """

    parser = argparse.ArgumentParser(description='Process Android documentation for tooltips')
    parser.add_argument('--input', required=True, help='Path to the directory containing Android classes.html (e.g., SourceDocs/AndroidDocs/API/Classes/developer.android.com/reference)')
    parser.add_argument('--output', required=True, help='Path to the output pickle file (e.g., tooltips_android.pkl)')
    parser.add_argument('--html-output', required=True, help='Root directory for output HTML files')
    parser.add_argument('--debug', action='store_true', help='Debug mode: only process first 10 items')

    args = parser.parse_args()

    if not os.path.exists(args.input):
        print(f"Error: Input directory {args.input} does not exist")
        return 1

    print(f"Processing Android documentation from {args.input}")
    print(f"Output pickle file: {args.output}")
    print(f"HTML output directory: {args.html_output}")
    traverse(args.input, args.output, output_root=args.html_output, debug_mode=args.debug)


    """
    d = get_all_file_data(
        "/home/elissa/ADFA/OfflineDocumentationTools/ProcessDocs/AndroidDocs/androidx/media3/muxer/AacMuxer.html")
    print(d)
    print("Android tooltip processing completed!")
    return 0
    """

if __name__ == '__main__':
    exit(main())