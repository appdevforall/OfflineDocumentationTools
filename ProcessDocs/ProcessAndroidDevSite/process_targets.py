#!/usr/bin/env python3
# Rewritten by o4-mini to use ccommand line arguments!

import argparse
import pickle
from urllib.parse import urljoin
import sys

def main():
    parser = argparse.ArgumentParser(
        description="Classify a list of URLs as valid or invalid against "
                    "a pickle‑serialized URL->file map, and emit the target files."
    )
    parser.add_argument(
        "--url-file-map",
        required=True,
        help="Path to pickle file holding the URL->file map"
    )
    parser.add_argument(
        "--links-file",
        required=True,
        help="Path to text file containing one URL (or URL fragment) per line"
    )
    parser.add_argument(
        "--output-files",
        required=True,
        help=(
            "Comma‑separated output filenames in this order:\n"
            "  valid_urls.txt,invalid_urls.txt,target_pages.txt"
        )
    )

    args = parser.parse_args()

    # split the three output filenames
    try:
        valid_out, invalid_out, target_out = args.output_files.split(",")
    except ValueError:
        parser.error(
            "Expected --output-files to be three comma‑separated "
            "paths: valid,invalid,target"
        )

    # read and normalize links
    with open(args.links_file, "r") as f:
        links = [
            urljoin("https://developer.android.com/", line.strip())
            for line in f
            if line.strip()
        ]

    # load the URL->file map
    with open(args.url_file_map, "rb") as f:
        url_map = pickle.load(f)

    # classify
    valid_urls = [u for u in links if u in url_map]
    invalid_urls = [u for u in links if u not in url_map]
    target_pages = [url_map[u] for u in valid_urls]

    # write outputs
    with open(valid_out, "w") as f:
        f.write("\n".join(valid_urls))
    with open(invalid_out, "w") as f:
        f.write("\n".join(invalid_urls))
    with open(target_out, "w") as f:
        f.write("\n".join(target_pages))

if __name__ == "__main__":
    main()