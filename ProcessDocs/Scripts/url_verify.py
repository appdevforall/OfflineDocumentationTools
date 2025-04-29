import os
import sys
from bs4 import BeautifulSoup
from urllib.parse import urlparse


def is_external(url):
    # Check if URL starts with an external scheme.
    parsed = urlparse(url)
    return parsed.scheme in ('http', 'https', 'mailto')


def check_local_file(base_dir, url):
    """
    Take in a URL pointing to local resource and determine whether the contents are stored locally.
    Valid = we have it
    Broken = we don't
    """
    # Remove query and fragment parts using urlparse.
    parsed = urlparse(url)
    local_path = parsed.path

    # If the local_path is empty (for example, a hash link), we can treat it as valid.
    if not local_path:
        return "valid"

    # Join with base directory of HTML file.
    abs_path = os.path.normpath(os.path.join(base_dir, local_path))
    if os.path.exists(abs_path):
        return "valid"
    else:
        return "broken"


def process_html_file(file_path, output_fh):

    """
    Given an HTML file, find URLs in any src/href attributes and check their availability.
    """
    base_dir = os.path.dirname(file_path)
    try:
        with open(file_path, "r", encoding="utf-8") as f:
            soup = BeautifulSoup(f, "html.parser")
    except Exception as e:
        sys.stderr.write(f"Error reading {file_path}: {e}\n")
        return

    # Loop over tags that might contain URLs in attributes "href" or "src"
    for tag in soup.find_all():
        for attr in ("href", "src"):
            url = tag.get(attr)
            if not url:
                continue  # no URL provided

            # Determine the link status.
            if is_external(url):
                status = "external"
            else:
                # For local URLs, check if the file exists.
                status = check_local_file(base_dir, url)

            # Write the result block to the output file.
            output_fh.write(f"{os.path.basename(file_path)}\t{url}\t{status}\n")


def main():
    if len(sys.argv) != 3:
        sys.stderr.write("Usage: python3 check_links.py html_directory output_file\n")
        sys.exit(1)

    html_directory = sys.argv[1]
    output_file = sys.argv[2]

    if not os.path.isdir(html_directory):
        sys.stderr.write(f"Error: The provided directory does not exist: {html_directory}\n")
        sys.exit(1)

    # Open output file.
    try:
        with open(output_file, "w", encoding="utf-8") as out_fh:
            for root, dirs, files in os.walk(html_directory):
                for filename in files:
                    if filename.lower().endswith((".html", ".htm")):
                        file_path = os.path.join(root, filename)
                        process_html_file(file_path, out_fh)
    except Exception as e:
        sys.stderr.write(f"Error writing to output file {output_file}: {e}\n")
        sys.exit(1)


if __name__ == "__main__":
    main()