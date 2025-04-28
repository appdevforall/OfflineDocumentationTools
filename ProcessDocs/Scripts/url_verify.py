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
    Given the base directory (the directory of the HTML file)
    and the URL (which may be a relative or absolute URL path),
    determine if the file exists.

    Returns: "valid" if the file exists, otherwise "broken".
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
    Process a single HTML file to check all 'href' and 'src' attributes,
    then write a block to output_fh for each encountered URL.
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
            # Note: file_path is the full path on disk; you may wish to output just
            # the file name using os.path.basename(file_path)
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
            # Walk the directory tree and process HTML files.
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