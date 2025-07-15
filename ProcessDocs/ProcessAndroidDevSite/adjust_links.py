import os
from bs4 import BeautifulSoup
from urllib.parse import urlparse, unquote

def process_html_files(directory_path):
    """
    Processes HTML files in a given directory to adjust <a> tag classes.

    Args:
        directory_path (str): The path to the directory containing HTML files.
    """
    if not os.path.isdir(directory_path):
        print(f"Error: Directory not found at '{directory_path}'")
        return

    print(f"Processing HTML files in: {directory_path}")

    # Walk through the directory
    for root, _, files in os.walk(directory_path):
        for file_name in files:
            if file_name.endswith(".html") or file_name.endswith(".htm"):
                file_path = os.path.join(root, file_name)
                print(f"\n--- Processing file: {file_path} ---")

                try:
                    with open(file_path, 'r', encoding='utf-8') as f:
                        soup = BeautifulSoup(f, 'html.parser')

                    modified = False
                    # Find all <a> tags
                    for a_tag in soup.find_all('a'):
                        current_class = a_tag.get('class', [])
                        href = a_tag.get('href')

                        # Skip if class is 'external' or 'existing'
                        if 'external' in current_class or 'existing' in current_class:
                            # print(f"  Skipping <a> tag with class: {current_class} and href: {href}")
                            continue

                        # Process if class is 'non-existing' and href exists
                        if 'non-existing' in current_class and href:
                            # Parse the URL to get the path component, ignoring fragments
                            parsed_url = urlparse(href)
                            # Unquote the path to handle URL-encoded characters (e.g., spaces)
                            relative_path = unquote(parsed_url.path)

                            # Construct the full local path
                            # os.path.join handles joining paths correctly across OS
                            # os.path.dirname(file_path) gives the directory of the current HTML file
                            target_file_path = os.path.join(os.path.dirname(file_path), relative_path)

                            # Normalize the path to handle '..' etc.
                            target_file_path = os.path.normpath(target_file_path)

                            print(f"  Checking <a> tag: href='{href}', current_class='{current_class}'")
                            print(f"    Resolved target path: '{target_file_path}'")

                            if os.path.exists(target_file_path):
                                if 'non-existing' in current_class: # Ensure it's still 'non-existing'
                                    # Remove 'non-existing' and add 'existing'
                                    a_tag['class'] = [cls for cls in current_class if cls != 'non-existing']
                                    a_tag['class'].append('existing')
                                    print(f"    -> Changed class to 'existing' for: {href}")
                                    modified = True
                            else:
                                print(f"    -> Target file DOES NOT exist: '{target_file_path}'")
                        elif href and not current_class:
                            # If no class is present, and it's a local link, check existence
                            parsed_url = urlparse(href)
                            relative_path = unquote(parsed_url.path)
                            target_file_path = os.path.join(os.path.dirname(file_path), relative_path)
                            target_file_path = os.path.normpath(target_file_path)

                            if not parsed_url.netloc and not parsed_url.scheme: # It's a local link
                                if os.path.exists(target_file_path):
                                    a_tag['class'] = ['existing']
                                    print(f"    -> Added 'existing' class to local link with no class: {href}")
                                    modified = True
                                else:
                                    a_tag['class'] = ['non-existing']
                                    print(f"    -> Added 'non-existing' class to local link with no class: {href}")
                                    modified = True


                    if modified:
                        with open(file_path, 'w', encoding='utf-8') as f:
                            f.write(str(soup))
                        print(f"--- File saved: {file_path} (modifications applied) ---")
                    else:
                        print(f"--- No modifications needed for: {file_path} ---")

                except Exception as e:
                    print(f"Error processing file '{file_path}': {e}")

def main():
    html_directory = "combdocs_backup"

    process_html_files(html_directory)
    # Create a dummy directory and files for testing
    if not os.path.exists(html_directory):
        os.makedirs(html_directory)


# --- How to use the script ---
if __name__ == "__main__":
    main()
