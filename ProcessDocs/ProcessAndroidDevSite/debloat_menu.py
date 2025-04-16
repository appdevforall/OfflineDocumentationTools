#!/usr/bin/env python3
"""
debloat_menu.py

This script processes a developer.android.com navigation menu HTML file and “de-bloats” it by:
  • Removing all attributes except for "href" (which is kept).
  • Preserving only the list structure (i.e. the hierarchy of <ul> and <li> tags).
  • Adjusting every <a> element’s "href" attribute as follows:
       - Absolute URLs are left unchanged.
       - For relative URLs: the first slash is discarded,
         every subsequent "/" is replaced with an underscore,
         and ".html" is appended.
For example, "/develop/ui/views/layout/declaring-layout" becomes "develop_ui_views_layout_declaring-layout.html".
"""

import argparse
import logging
import sys

from bs4 import BeautifulSoup

def parse_args():
    parser = argparse.ArgumentParser(description="Debloat an HTML menu file.")
    parser.add_argument("--input", required=True, help="Input menu HTML file")
    parser.add_argument("--output", required=True, help="Output (debloated) menu HTML file")
    return parser.parse_args()

def adjust_menu_href(href):
    # If absolute URL, return it unchanged.
    if href.startswith("http://") or href.startswith("https://"):
        return href
    # For relative URLs: remove first character if '/', then replace '/' with '_' and add .html
    if href.startswith("/"):
        href = href[1:]
    new_href = href.replace("/", "_") + ".html"
    return new_href

def remove_extra_attributes(soup):
    # For every tag, remove all attributes except href if present.
    for tag in soup.find_all():
        if tag.name == "a":
            # Preserve href only.
            if "href" in tag.attrs:
                tag.attrs = {"href": tag["href"]}
            else:
                tag.attrs = {}
        else:
            # Remove all attributes.
            tag.attrs = {}
    return soup

def adjust_menu_links(soup):
    for a in soup.find_all("a", href=True):
        original = a["href"]
        a["href"] = adjust_menu_href(original)
    return soup

def main():
    args = parse_args()
    try:
        with open(args.input, "r", encoding="utf-8") as f:
            content = f.read()
        soup = BeautifulSoup(content, "html.parser")
        # Remove all extra attributes; keep only the list structure.
        soup = remove_extra_attributes(soup)
        # Adjust the href attributes of <a> elements.
        soup = adjust_menu_links(soup)
        with open(args.output, "w", encoding="utf-8") as f:
            f.write(soup.prettify())
    except Exception as e:
        logging.exception("Error processing menu file")
        sys.exit(1)

if __name__ == "__main__":
    main()
