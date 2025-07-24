#!/usr/bin/env python3
import csv
import os
import sys
import html
import requests
from collections import defaultdict
from bs4 import BeautifulSoup

DOCS_LOCATION = "/home/elissa/ADFA/kotlinstddocs/kotlin/libraries/tools/kotlin-stdlib-docs/build/doc/previous/all-libs/2.2/kotlin-stdlib"

def extract_fully_qualified_name(url, fallback):
    """
    Given a URL to a documentation page, fetch the page and extract the
    fully qualified name from the breadcrumbs div. The breadcrumbs HTML
    is assumed to look like this:

      <div class="breadcrumbs">
        <a href="../../index.html">kotlin-stdlib</a>
        <span class="delimiter">/</span>
        <a href="../index.html">kotlin.enums</a>
        <span class="delimiter">/</span>
        <span class="current">EnumEntries</span>
      </div>

    This function will strip out the "kotlin-stdlib" portion and join the
    remaining items with a period to produce:
         kotlin.enums.EnumEntries
    If any error occurs, the fallback (usually the original full symbol name)
    is returned.
    """
    try:
        soup = BeautifulSoup(open(os.path.join(DOCS_LOCATION, url), "r").read(), "html.parser")
        breadcrumbs_div = soup.find("div", class_="breadcrumbs")
        if not breadcrumbs_div:
            print("?")
            return fallback

        # Find all <a> and <span> elements under the breadcrumbs div.
        parts = []
        for element in breadcrumbs_div:
            # For delimiter spans, skip.
            if element.name == "span" and "delimiter" in element.get("class"):
                continue
            text = element.get_text(strip=True)
            # Skip the initial "kotlin-stdlib"
            if text == "kotlin-stdlib":
                continue
            parts.append(text)
        if parts:
            return ".".join(parts)
        else:
            print(breadcrumbs_div)
            return fallback
    except Exception as e:
        # In case of an error (e.g., network issue/HTML structure change),
        # return the fallback value.
        return fallback

def generate_html_page(selected_symbol, entries, output_dir):
    # Prepare a safe filename for the selected symbol.
    safe_name = selected_symbol.replace(" ", "_")
    output_file = os.path.join(output_dir, f"{safe_name}.html")

    with open(output_file, "w", encoding="utf-8") as f:
        f.write("<!DOCTYPE html>\n")
        f.write("<html lang='en'>\n")
        f.write("<head>\n")
        f.write("  <meta charset='utf-8'>\n")
        f.write(f"  <title>{selected_symbol} – Disambiguation</title>\n")
        f.write("</head>\n")
        f.write("<body>\n")
        f.write(f"  <h1>{selected_symbol}</h1>\n")
        f.write("  <p>The symbol <strong>{}</strong> may represent one of the following definitions:</p>\n".format(selected_symbol))
        f.write("  <ul>\n")
        for full_symbol, url in entries:
            # Instead of simply reusing the URL, fetch the page and
            # extract the fully qualified name from the breadcrumbs.
            fq_name = extract_fully_qualified_name(url, full_symbol)
            f.write(f"    <li><a href=\"/kotlin-stdlib/{url}\">{full_symbol}</a> at {fq_name}</li>\n")
        f.write("  </ul>\n")
        f.write("</body>\n")
        f.write("</html>\n")

    return f"Disambiguation\t{selected_symbol}\t{output_file}\n"

def main(tsv_path, output_dir):
    log_file = "log.txt"
    log_content = ""

    # Group entries by the selected symbol name.
    groups = defaultdict(list)

    with open(tsv_path, newline='', encoding="utf-8") as f:
        reader = csv.reader(f, delimiter="\t")
        for row in reader:
            if len(row) < 3:
                continue  # Skip rows that do not contain enough columns.
            selected_symbol, full_symbol, doc_url = row[0].strip(), row[1].strip(), row[2].strip()
            groups[selected_symbol].append((full_symbol, doc_url))

    # Create the output directory if it doesn't exist.
    os.makedirs(output_dir, exist_ok=True)

    # For each ambiguous symbol (more than one meaning), generate an HTML disambiguation page.
    for symbol, entries in groups.items():
        if len(entries) > 1:
            log_content += generate_html_page(symbol, entries, output_dir)
        else:
            log_content += f"One meaning for symbol\t{symbol}\t{entries[0][0]}\t{entries[0][1]}\n"

    with open(log_file, "w", encoding="utf-8") as log_f:
        log_f.write(log_content)

if __name__ == "__main__":
    if len(sys.argv) != 3:
        print("Usage: {} <input_file.tsv> <output_directory>".format(sys.argv[0]))
        sys.exit(1)

    tsv_file = sys.argv[1]
    output_directory = sys.argv[2]
    main(tsv_file, output_directory)