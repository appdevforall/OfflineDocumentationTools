#!/usr/bin/env python3

import sys
from bs4 import BeautifulSoup


def extract_links(html_file, output_file):
    # Read the input HTML file
    with open(html_file, 'r', encoding='utf8') as file:
        html_content = file.read()

    # Parse the HTML using BeautifulSoup
    soup = BeautifulSoup(html_content, 'html.parser')

    # Find all <a> tags
    links = soup.find_all('a')

    # Extract the href attribute from each <a> tag
    hrefs = []
    for link in links:
        href = link.get('href')
        if href:  # Check if href exists
            hrefs.append(href)

    # Write the list of URLs to the output file, one per line
    with open(output_file, 'w', encoding='utf8') as out_file:
        for href in hrefs:
            out_file.write(href + '\n')

    print(f"Extracted {len(hrefs)} link(s) and wrote them to '{output_file}'.")


if __name__ == "__main__":
    # Ensure the user provided input and output file names
    if len(sys.argv) != 3:
        print("Usage: python link_extract.py input.html output.txt")
        sys.exit(1)

    input_html = sys.argv[1]
    output_text = sys.argv[2]
    extract_links(input_html, output_text)