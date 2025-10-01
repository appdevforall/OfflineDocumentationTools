#!/usr/bin/env python3
"""
This script:
  1. Opens an input PDF (here "input.pdf") and copies its pages.
  2. Walks through its bookmarks (outline tree) and adds a named destination for
     each bookmark using add_named_destination_array().
     (The destination array exactly matches that in the original document.)
  3. Writes the updated PDF to the file provided as a command-line argument.
  4. Generates an HTML file ("bookmarks.html") that lists links (with the bookmark titles)
     to each named destination using the PDF’s nameddest URL parameter.

Usage:
    python script.py updated_output.pdf input_pdf_name.pdf
"""
import sys
import os
import urllib.parse
from pypdf import PdfReader, PdfWriter
from pypdf.generic import TextStringObject, DictionaryObject

# def clean_title

# Helper function to recursively flatten the bookmark outline.
def flatten_outlines(outlines):
    """
    Given the outlines returned by PdfReader.outline (which may be nested),
    return a flat list of bookmark entries (Destination objects).
    """
    flat = []
    for obj in outlines:
        # When a bookmark has children bookmarks, it will be a list
        if isinstance(obj, list):
            flat.extend(flatten_outlines(obj))
        else:
            # We assume that obj is a Destination (i.e. an outline entry)
            flat.append(obj)
    return flat

# Method to convert TOC entries to anchors for Java Notes for Professionals
def get_title_javanotes(title):
    return title.split(":")[0].replace(" ", "")

def main():
    if len(sys.argv) != 4:
        print("Usage: python script.py updated_output.pdf input_pdf_name.pdf bookmarks_list_file.html")
        sys.exit(1)

    updated_pdf_filename = sys.argv[1]
    input_pdf_filename = sys.argv[2]
    bookmarks_list_file = sys.argv[3]


    # Change this if your input file name is different.
    if not os.path.exists(input_pdf_filename):
        print(f"Input file {input_pdf_filename} not found!")
        sys.exit(1)

    # Read the original PDF.
    reader = PdfReader(input_pdf_filename)
    writer = PdfWriter()

    # Copy all pages into the writer.
    for page in reader.pages:
        writer.add_page(page)

    # Get the outlines (bookmarks). Depending on your PDF they may be nested.
    try:
        outlines = reader.outline
    except Exception as e:
        print("Could not retrieve bookmarks from the input PDF.")
        sys.exit(1)

    bookmarks = flatten_outlines(outlines)

    # List to hold tuples (title, destination title string) for the HTML page.
    # Note: the PDF viewer will jump if you add "#nameddest=Title" to the filename.
    bookmark_links = []

    # For each bookmark add the named destination.
    for idx, bm in enumerate(bookmarks):
        # bm is a Destination object. Its title attribute is used for the named destination.
        title = bm.title
        num = reader.get_destination_page_number(bm)
        encoded_title = get_title_javanotes(title)

        # ToC handling
        if "JavaNotesForProfessionals.pdf" in input_pdf_filename:
            if encoded_title == "Contentlist":
                encoded_title = "ToC"

        writer.add_named_destination(encoded_title, num)

        bookmark_links.append((title, encoded_title))

    # Write the updated PDF.
    with open(updated_pdf_filename, "wb") as out_f:
        writer.write(out_f)

    print(f"Updated PDF written as: {updated_pdf_filename}")

    # Write an HTML file containing a list of links to the named destinations.
    with open(bookmarks_list_file, "w", encoding="utf-8") as html_f:
        html_f.write("<!DOCTYPE html>\n<html>\n<head>\n")
        html_f.write("  <meta charset='UTF-8'>\n")
        html_f.write("  <title>PDF Bookmarks</title>\n")
        html_f.write("</head>\n<body>\n")
        html_f.write("<h1>PDF Bookmarks</h1>\n")
        html_f.write("<ul>\n")
        for title, encoded_title in bookmark_links:
            # The link points to the updated PDF with the nameddest fragment.
            # Some PDF viewers will jump to the named destination if the URL is like:
            # updated_pdf.pdf#nameddest=<name>

            link = f"{updated_pdf_filename}#nameddest={encoded_title}"
            html_f.write(f"  <li><a href='{link}' target='_blank'>{title}</a></li>\n")
        html_f.write("</ul>\n</body>\n</html>")
    print(f"HTML file with bookmark links written as: {html_filename}")


if __name__ == "__main__":
    main()
