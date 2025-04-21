"""
process_kotlin_docs.py

Usage:
    python process_kotlin_docs.py input_directory output_directory menu_url

This script processes individual Kotlin documentation HTML pages in the input_directory.
It performs the following:
  - Removes any JavaScript (<script> tags, including inline scripts and those with external sources).
  - Removes any in-page styling (both <style> tags and <link> tags that reference stylesheets).
  - Removes any element whose class attribute contains the string "navigation-links".
  - Extracts the content of the <article> element.
  - Wraps that content in a new HTML document that includes:
        • A <head> with a reference to an external stylesheet (doc_style.css)
        • A <body> that starts with a “MENU” link (using the given menu_url) styled to be dark blue, centered, and sized similarly to an h1,
          followed by a horizontal rule (<hr>), and then the article content.
  - Writes the processed HTML to the output_directory using the same filename.
  - Copies the “images” subdirectory from the input directory into the output directory.

A stylesheet (doc_style.css) is also generated in the output directory to support mobile-friendly and readable formatting.
"""

import os
import sys
import shutil
import re
from bs4 import BeautifulSoup



def remove_scripts_and_styles(soup):
    # Remove all <script> tags (both inline and with external src)
    for script in soup.find_all("script"):
        script.decompose()

    # Remove any <style> tags
    for style in soup.find_all("style"):
        style.decompose()

    # Remove any link tag that references a stylesheet (e.g. <link rel="stylesheet" ...>)
    for link in soup.find_all("link", rel=re.compile("stylesheet", re.I)):
        link.decompose()


def remove_navigation_links(soup):
    # Remove any element whose 'class' attribute contains the string "navigation-links"
    # Note: tag["class"] is a list if present.
    for tag in soup.find_all(attrs={"class": True}):
        if  tag.name is None:
            continue
        classes = tag.get("class")
        if any("navigation-links" in cls for cls in classes):
            tag.decompose()


def process_html_file(input_filepath, output_filepath, menu_url):
    # Read the entire HTML file.
    with open(input_filepath, "r", encoding="utf-8") as infile:
        content = infile.read()

    soup = BeautifulSoup(content, "html.parser")

    # Remove all JavaScript and styling from the input document.
    remove_scripts_and_styles(soup)

    # Remove any element with class containing "navigation-links"
    remove_navigation_links(soup)

    # Look for the <article> element in the document.
    article = soup.find("article")
    if article is None:
        print(f"Warning: No <article> element found in {os.path.basename(input_filepath)}. Skipping file.")
        return

    # Build the new HTML document.
    new_soup = BeautifulSoup("", "html.parser")

    # Create HTML structure
    html_tag = new_soup.new_tag("html")
    new_soup.append(html_tag)

    head_tag = new_soup.new_tag("head")
    # Add meta charset info (optional) can be adjusted.
    meta_tag = new_soup.new_tag("meta", charset="UTF-8")
    head_tag.append(meta_tag)
    # Add stylesheet reference to doc_style.css
    link_tag = new_soup.new_tag("link", rel="stylesheet", href="doc_style.css")
    head_tag.append(link_tag)
    html_tag.append(head_tag)

    body_tag = new_soup.new_tag("body")

    # Insert at the top a link as specified:
    menu_link = new_soup.new_tag("a", href=menu_url, **{"class": "menu-link"})
    menu_link.string = "MENU"
    body_tag.append(menu_link)

    hr_tag = new_soup.new_tag("hr")
    body_tag.append(hr_tag)

    # Append the content of the <article> element.
    # We use .decode_contents() to get the inner HTML while keeping the tag structure.
    article_container = new_soup.new_tag("div", **{"class": "article-content"})
    # Parse the article's inner HTML into new_soup fragment and then append.
    article_content = BeautifulSoup(article.decode_contents(), "html.parser")
    article_container.append(article_content)
    body_tag.append(article_container)

    html_tag.append(body_tag)

    # Write the output file.
    with open(output_filepath, "w", encoding="utf-8") as outfile:
        outfile.write(str(new_soup.prettify()))
    print(f"Processed {os.path.basename(input_filepath)} -> {os.path.basename(output_filepath)}")


def generate_stylesheet(css_filepath):
    # Generate a basic CSS file that meets the following:
    # - Clearly separated paragraphs and lists.
    # - Render code blocks (elements with class "code-block") in monospace font
    #   with newlines preserved.
    # - Inline code is rendered in monospace font.
    # - The “MENU” link is styled with dark blue text, centered, and sized like an h1.
    # - Responsive design: images scale, overall padding for mobile.
    css_content = """
/* Overall styling */
body {
    font-family: Arial, sans-serif;
    margin: 1em;
    padding: 0;
    line-height: 1.6;
}

/* MENU link styling */
a.menu-link {
    display: block;
    text-align: center;
    color: darkblue;
    font-size: 2em;  /* approximates an h1 */
    text-decoration: none;
    margin-bottom: 0.5em;
}

/* Horizontal rule styling */
hr {
    border: none;
    border-top: 1px solid #ccc;
    margin-bottom: 1em;
}

/* Paragraphs and lists separation */
p, ul, ol {
    margin-bottom: 1em;
}

/* Code blocks */
.code-block {
    font-family: "Courier New", monospace;
    background-color: #f8f8f8;
    border: 1px solid #ddd;
    padding: 1em;
    white-space: pre-wrap; /* allows newlines to be preserved */
    overflow-x: auto;
    margin-bottom: 1em;
}

/* Inline code */
code {
    font-family: "Courier New", monospace;
    background-color: #f2f2f2;
    padding: 2px 4px;
    border-radius: 3px;
}

/* Responsive images */
img {
    max-width: 100%;
    height: auto;
}

/* Responsive design: ensure layout adapts to mobile devices */
@media (max-width: 600px) {
    body {
        margin: 0.5em;
    }
    a.menu-link {
        font-size: 1.8em;
    }
}
"""
    with open(css_filepath, "w", encoding="utf-8") as css_file:
        css_file.write(css_content)
    print(f"Generated stylesheet: {os.path.basename(css_filepath)}")


def main():
    if len(sys.argv) != 4:
        sys.exit("Usage: python process_kotlin_docs.py input_directory output_directory menu_url log_file")

    input_dir = sys.argv[1]
    output_dir = sys.argv[2]
    menu_url = sys.argv[3]
    log_file = sys.argv[4]

    # Make sure the input directory exists.
    if not os.path.isdir(input_dir):
        sys.exit("Error: The input directory does not exist.")

    # Create the output directory if it does not exist.
    if not os.path.isdir(output_dir):
        os.makedirs(output_dir)

    # Process each .html file in the input directory (first level only)
    for filename in os.listdir(input_dir):
        if filename.lower().endswith(".html"):
            input_filepath = os.path.join(input_dir, filename)
            output_filepath = os.path.join(output_dir, filename)
            process_html_file(input_filepath, output_filepath, menu_url)

    # Generate doc_style.css in the output directory.
    css_filepath = os.path.join(output_dir, "doc_style.css")
    generate_stylesheet(css_filepath)

    # Copy the "images" subdirectory (if it exists) from input_dir to output_dir.
    src_images = os.path.join(input_dir, "images")
    dest_images = os.path.join(output_dir, "images")
    if os.path.isdir(src_images):
        # Use copytree; if dest_images already exists, remove it first.
        if os.path.exists(dest_images):
            shutil.rmtree(dest_images)
        shutil.copytree(src_images, dest_images)
        print("Copied images directory.")
    else:
        print("No 'images' directory found in input.")


if __name__ == "__main__":
    main()