# Import the argparse library, which is the standard way to handle command-line arguments in Python.
import argparse
import json
import sqlite3
import csv
import os
import html
import brotli
import sys
from collections import defaultdict


"""
TODO @Alex: update to work with current schema
"""

PLACEHOLDER_T1_MESSAGE = "Placeholder T1 tooltip"
PLACEHOLDER_T2_MESSAGE = "Placeholder T2 tooltip"

def generate_html_page(selected_symbol, entries, output_dir):
    # Create a safe filename for the selected symbol.
    # (Here we just replace spaces with underscores; you might need further sanitizing.)
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
        f.write("  <p>The symbol <strong>{}</strong> may represent one of the following definitions:</p>\n".format(
            selected_symbol))
        f.write("  <ul>\n")
        for full_symbol, url in entries:
            f.write(f"    <li><a href=\"]{url}\">{full_symbol}</a> at {url}</li>\n")
        f.write("  </ul>\n")
        f.write("</body>\n")
        f.write("</html>\n")

    return output_file

def generate_tooltips(pages_json_file):
    json_data = json.load(open(pages_json_file, "r"))


def main():
    logfile = "log.txt"
    open(logfile, "w").write(" ")
    log_handle = open(logfile, "a")

    parser = argparse.ArgumentParser(
        description="A script to process an input file and update a database."
    )

    # input file
    parser.add_argument(
        "-i", "--input",
        type=str,
        required=True,
        help="The path to the input file to be processed."
    )

    # Add the "-d" or "--database" argument. This is also required.
    parser.add_argument(
        "-d", "--database",
        type=str,
        required=True,
        help="The name of the database to be updated."
    )

    # Add the "-d" or "--database" argument. This is also required.
    parser.add_argument(
        "-p", "--disambiguation-dir",
        type=str,
        required=True,
        help="The name of the database to be updated."
    )

    args = parser.parse_args()

    input_file = args.input
    database_name = args.database
    disambiguation_dir = args.disambiguation_dir

    if not os.path.exists(disambiguation_dir):
        os.makedirs(disambiguation_dir)

    print(f"Input file path: {input_file}")
    print(f"Database name: {database_name}")
    json_data = json.load(open(input_file, "r"))

    groups = {}

    for api_entry in json_data:
            symbol_basename = api_entry["searchKeys"][0]
            full_symbol = api_entry["name"]
            url = "ks/" + api_entry["location"]
            if symbol_basename in groups:
                groups[symbol_basename].append((full_symbol, url))
            else:
                groups[symbol_basename] = [(full_symbol, url)]


    # tooltipCategory,tooltipTag,tooltipSummary,tooltipDetail,tooltipButtons
    tooltips_json = []

    # Holding constant for now
    tooltip_category = "kotlin"
    tooltip_summary = PLACEHOLDER_T1_MESSAGE
    tooltip_detail = PLACEHOLDER_T2_MESSAGE

    conn = sqlite3.connect(database_name)
    cursor = conn.cursor()

    for symbol, entries in list(groups.items()):
        if len(entries) > 1:
            disamb_page = generate_html_page(symbol, entries, disambiguation_dir)
            disamb_content = brotli.compress(open(disamb_page, "rb").read())
            tooltip_url =  "ks/" + disamb_page
            log_handle.write("Made page for ambiguous symbol " + symbol + "\n")
            #print(disamb_page)
            #cursor.execute(
            #   "INSERT INTO Content (path, languageID, content, contentTypeID) VALUES (?, ?, ?, ?)",
            #    (disamb_page, "en-US", disamb_content, 12))

        else:
            tooltip_url = entries[0][1]
            log_handle.write(f"One meaning for symbol {symbol}:\n{entries[0][0]} \t {tooltip_url}\n")

        tooltip_tag = symbol
        tooltip_buttons = json.dumps([{"first": "See documentation for " + symbol + " in the Kotlin standard library.", "second": tooltip_url}])

        # command = f"""INSERT OR REPLACE INTO ide_tooltip_table
        #                (tooltipCategory, tooltipTag, tooltipSummary, tooltipDetail, tooltipButtons)
        #                VALUES ({tooltip_category}, {tooltip_tag}, {tooltip_summary}, {tooltip_detail}, {tooltip_buttons})"""

        #print(command)
        #cursor.execute("""
        #    INSERT OR REPLACE INTO ide_tooltip_table
        #    (tooltipCategory, tooltipTag, tooltipSummary, tooltipDetail, tooltipButtons)
        #    VALUES (?, ?, ?, ?, ?)
        #""", (tooltip_category, tooltip_tag, tooltip_summary, tooltip_detail, tooltip_buttons))
        # cursor.execute(command)
        #conn.commit()

    conn.close()

    # group entries by the selected symbol name
    # groups = defaultdict(list)


if __name__ == "__main__":
    main()
