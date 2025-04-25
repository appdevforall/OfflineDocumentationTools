#!/usr/bin/env python3
"""
This script processes Kotlin documentation HTML pages in an input directory
by removing JavaScript, page styling, navigation-link elements, and then extracting
the <article> element content. It also processes links and code blocks as specified.
The output pages are placed in an output directory with a standard HTML skeleton,
with an added “Menu” link at the top (the menu file is provided by a command‐line
argument). A CSS stylesheet is generated in the output directory with a given name.
The images subdirectory is also copied.
All events (successful processing or errors) are logged to a file.
"""

import os
import sys
import argparse
import logging
import shutil
import traceback
from bs4 import BeautifulSoup

def setup_logging():
    logging.basicConfig(stream=sys.stdout,
                        level=logging.INFO,
                        format='%(asctime)s [%(levelname)s] %(message)s',
                        datefmt='%Y-%m-%d %H:%M:%S')

def process_html_file(input_filepath, output_filepath, menu_filename, stylesheet_filename):
    """
    Process a single .html file:
      - Removes all <script> and <style> tags, and also all <link> tags that refer to stylesheets.
      - Removes any element whose "class" attribute includes "navigation-links" or "naviation-links".
      - Extracts the content of the first <article> element. If none exists the file is skipped.
      - Processes all <a> tags: external links are given inline red color; local links blue.
      - For any element with class containing "code-block", add inline CSS to display as a preformatted, monospace block.
      - Wrap the processed article content inside a new HTML document that includes:
            • A <head> with a link to the given stylesheet.
            • A <body> starting with a centered “Menu” link that points to the given menu file.
    """
    try:
        with open(input_filepath, "r", encoding="utf-8") as f:
            html_content = f.read()
    except Exception as e:
        logging.error("Error reading file %s: %s", input_filepath, e)
        return False

    try:
        soup = BeautifulSoup(html_content, "html.parser")

        # Remove all <script> tags.
        for script in soup.find_all("script"):
            script.decompose()

        # Remove all <style> tags.
        for style in soup.find_all("style"):
            style.decompose()

        # Remove <link> tags that are stylesheets.
        for link in soup.find_all("link", rel=lambda x: x and "stylesheet" in x.lower()):
            link.decompose()

        # Remove any element whose class includes 'navigation-links' or the misspelling 'naviation-links'
        for tag in soup.find_all(class_=lambda value: value and ("navigation-links" in " ".join(value) or "naviation-links" in " ".join(value))):
            tag.decompose()

        # Find the first article in the file.
        article = soup.find("article")
        if not article:
            logging.info("No <article> element found in %s; copying file over in raw form.", os.path.basename(input_filepath))
            shutil.copy(input_filepath, output_filepath)
            return False

        # Process links: style external links red, local links blue.
        for a in article.find_all("a"):
            href = a.get("href", "")
            # Assume that if href starts with "http://" or "https://", it is external.
            if href.startswith("http://") or href.startswith("https://"):
                # Append inline style; if already present, append to it.
                style = a.get("style", "")
                a['style'] = style + "color: red !important;"
            else:
                style = a.get("style", "")
                a['style'] = style + "color: blue !important;"

        # Process code-blocks: add inline style for monospace font and pre-wrapped formatting.
        for code_block in article.find_all(class_=lambda value: value and "code-block" in " ".join(value)):
            existing_style = code_block.get("style", "")
            # Setting monospace font, preserving white-space, and giving a light background.
            code_block["style"] = existing_style + "font-family: monospace; white-space: pre-wrap; background-color: #f6f8fa; padding: 10px; border: 1px solid #ddd;"

        # Create a new BeautifulSoup document for the output
        new_soup = BeautifulSoup("", "html.parser")

        # Build the HTML structure.
        html_tag = new_soup.new_tag("html")
        head_tag = new_soup.new_tag("head")
        meta_charset = new_soup.new_tag("meta", charset="UTF-8")
        head_tag.append(meta_charset)
        title_tag = new_soup.new_tag("title")
        # Reuse the original title if it exists, otherwise leave a default title.
        original_title = soup.title.string if soup.title else os.path.basename(input_filepath)
        title_tag.string = original_title
        head_tag.append(title_tag)
        # Link the stylesheet (using the provided stylesheet filename; assumed relative path)
        link_tag = new_soup.new_tag("link", rel="stylesheet", href=stylesheet_filename)
        head_tag.append(link_tag)

        # For responsive design add a viewport meta tag.
        viewport_tag = new_soup.new_tag("meta", name_="viewport", content="width=device-width, initial-scale=1")
        head_tag.append(viewport_tag)

        html_tag.append(head_tag)

        body_tag = new_soup.new_tag("body")
        # Add centered Menu link at the top.
        center_div = new_soup.new_tag("div", style="text-align:center; margin: 20px 0;")
        menu_link = new_soup.new_tag("a", href=menu_filename, style="font-size: xx-large; text-decoration: none;")
        menu_link.string = "Menu"
        center_div.append(menu_link)
        body_tag.append(center_div)

        # Append the content of the <article> element.
        # Use .contents rather than .string to preserve tag hierarchy.
        for content in article.contents:
            body_tag.append(content)

        html_tag.append(body_tag)
        new_soup.append(html_tag)

        # Write the new HTML document to the output file.
        with open(output_filepath, "w", encoding="utf-8") as outf:
            outf.write(new_soup.prettify())

        logging.info("Successfully processed file %s", os.path.basename(input_filepath))
        return True

    except Exception as e:
        logging.error("Error processing file %s: %s\n%s", os.path.basename(input_filepath), e, traceback.format_exc())
        return False

def generate_css(stylesheet_path):
    """
    Produce a CSS stylesheet that is mobile friendly and responsive.
    Styles include:
      - A global sans-serif font.
      - Responsive layout (viewport meta tag is in HTML).
      - External links styled red and local links blue.
      - A definition for code blocks that displays them in monospace with preserved whitespace.
    """
    css_content = """
/* Base style */
body {
    margin: 0;
    padding: 20px;
    font-family: Arial, sans-serif;
    line-height: 1.6;
    background-color: #ffffff;
    color: #333333;
}

/* Responsive container */
.container {
    max-width: 800px;
    margin: auto;
    padding: 0 10px;
}

/* Headings */
h1, h2, h3, h4, h5, h6 {
    margin-top: 1.2em;
    margin-bottom: 0.5em;
}

/* Links */
a {
    text-decoration: none;
}

/* External links (can be overridden by inline styles) */
a[href^="http://"],
a[href^="https://"] {
    color: red;
}

/* Local links */
a:not([href^="http://"]):not([href^="https://"]) {
    color: blue;
}

/* Code blocks styling */
.code-block {
    font-family: monospace;
    white-space: pre-wrap;
    background-color: #f6f8fa;
    padding: 10px;
    border: 1px solid #ddd;
    overflow-x: auto;
}

/* Make images responsive */
img {
    max-width: 100%;
    height: auto;
}

/* Mobile first responsive design */
@media only screen and (max-width: 480px) {
    body {
        padding: 10px;
        font-size: 16px;
    }
}
"""
    try:
        with open(stylesheet_path, "w", encoding="utf-8") as css_file:
            css_file.write(css_content)
        logging.info("Successfully generated stylesheet at %s", stylesheet_path)
    except Exception as e:
        logging.error("Error writing stylesheet %s: %s\n%s", stylesheet_path, e, traceback.format_exc())
        sys.exit(1)

def main():
    parser = argparse.ArgumentParser(description="Pare down Kotlin documentation HTML pages for limited-storage devices.")
    parser.add_argument("--input-dir", required=True,
                        help="Path to the input directory containing HTML files and an 'images' subdirectory.")
    parser.add_argument("--output-dir", required=True,
                        help="Path to the output directory where processed HTML files and images will be placed.")
    parser.add_argument("--menu-file", required=True,
                        help="Filename for the navigation menu HTML page to link at the top of every page.")
    parser.add_argument("--stylesheet", required=True,
                        help="Filename for the CSS stylesheet to be generated and linked in every output page.")

    # CLI logging
    # parser.add_argument("--log-file", required=True,
    #                    help="Path to the log file for recording processing messages.")

    args = parser.parse_args()

    try:
        setup_logging()
        logging.info("Started processing with input dir: %s, output dir: %s", args.input_dir, args.output_dir)

        # Create the output directory if it doesn't exist.
        if not os.path.exists(args.output_dir):
            os.makedirs(args.output_dir)

        # Copy docs commit hash
        if os.path.exists(args.input_dir):
            shutil.copy(os.path.join(args.input_dir, "..", "docs_version.txt"), args.output_dir)
        else:
            logging.info("Input directory " + args.input_dir + " does not exist.")
            sys.exit(1)

        # Process HTML files in the input directory (only files in the immediate directory ending with .html).
        for filename in os.listdir(args.input_dir):
            input_path = os.path.join(args.input_dir, filename)
            if os.path.isfile(input_path) and filename.lower().endswith(".html"):
                output_file_path = os.path.join(args.output_dir, filename)
                process_html_file(input_path, output_file_path, args.menu_file, args.stylesheet)

        # Generate the CSS stylesheet in the output directory.
        stylesheet_path = os.path.join(args.output_dir, args.stylesheet)
        generate_css(stylesheet_path)

        # Copy the 'images' subdirectory from input to output.
        input_images_dir = os.path.join(args.input_dir, "images")
        output_images_dir = os.path.join(args.output_dir, "images")
        if os.path.exists(input_images_dir) and os.path.isdir(input_images_dir):
            # If output_images_dir exists, remove it first.
            if os.path.exists(output_images_dir):
                shutil.rmtree(output_images_dir)
            shutil.copytree(input_images_dir, output_images_dir)
            logging.info("Successfully copied images directory.")
        else:
            logging.info("No images directory found in input; skipping images copy.")

        # Copy over Writerside artifcat PNGs
        shutil.copy(os.path.join(args.input_dir, "writerside_32.png"), os.path.join(args.output_dir, "writerside_32.png"))
        shutil.copy(os.path.join(args.input_dir, "writerside_64.png"), os.path.join(args.output_dir, "writerside_64.png"))
        logging.info("Copied over Writerside icon PNG artifacts.")
        logging.info("Processing complete.")


    except Exception as e:
        # Log full traceback if there's any exception.
        logging.error("Unhandled exception: %s\n%s", e, traceback.format_exc())
        sys.exit(1)

if __name__ == "__main__":
    main()