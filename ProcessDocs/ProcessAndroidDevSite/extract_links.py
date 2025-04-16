# Rewritten to use command line args by o4-mini

#!/usr/bin/env python3

import argparse
from bs4 import BeautifulSoup

def extract_links(html_file, output_file):
    # Read the input HTML file
    with open(html_file, 'r', encoding='utf8') as file:
        html_content = file.read()

    # Parse the HTML using BeautifulSoup
    soup = BeautifulSoup(html_content, 'html.parser')

    # Find all <a> tags and extract their href attributes
    hrefs = []
    for link in soup.find_all('a'):
        href = link.get('href')
        if href:
            hrefs.append(href)

    # Write the list of URLs to the output file, one per line
    with open(output_file, 'w', encoding='utf8') as out_file:
        for href in hrefs:
            out_file.write(href + '\n')

    print(f"Extracted {len(hrefs)} link(s) and wrote them to '{output_file}'.")

def main():
    parser = argparse.ArgumentParser(
        description="Extract all hyperlinks from an HTML file and save them to a text file."
    )
    parser.add_argument(
        '--input-file',
        required=True,
        help="Input HTML file to extract links from."
    )
    parser.add_argument(
        '--output-file',
        required=True,
        help="Output text file containing a list of extracted links."
    )
    args = parser.parse_args()

    extract_links(args.input_file, args.output_file)

if __name__ == "__main__":
    main()