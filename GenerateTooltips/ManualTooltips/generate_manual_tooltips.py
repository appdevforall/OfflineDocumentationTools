import argparse
import csv
import json
import os
import sys

def csv_to_json(csv_file_path, json_file_path, html_path, substitution_file):
    substitutions = {}
    sub_lines = open(substitution_file, "r").readlines()

    for line in sub_lines:
        target, replacement = line.strip().split("\t")
        substitutions[target] = replacement

    # Read the CSV file with '^' as the delimiter
    with open(csv_file_path, mode='r', newline='', encoding='cp1252') as csv_file:
        csv_reader = csv.DictReader(csv_file, delimiter='^')

        # Convert CSV data to a list of dictionaries
        data = [row for row in csv_reader]

        for item in data:
            button = []
            buttons = []

            for num in {"1", "2", "3"}:
                descr = 'buttonDescr' + num
                uri = 'buttonURI' + num

                if item[uri] is not None and not item[uri] == "":
                    for target in substitutions:
                        if target in item[uri]:
                            item[uri] = item[uri].replace(target, substitutions[target])
                    #if "https://kotlinlang.org/" in item[uri]:
                    #    item[uri] = item[uri].replace("https://kotlinlang.org/docs/",
                    #                                  "file:///android_asset/CoGoTooltips/html/kmenu_out_resp/")
                    else:
                        item[uri] = "file:///android_asset/CoGoTooltips/html/" + item[uri]
                    button.append(item[descr])
                    button.append(item[uri])
                    buttons.append(button)
                    ## print(item["tag"], num, buttons)
                    button = []
                item.pop(descr)
                item.pop(uri)

            item['buttons'] = buttons

    # Write the JSON file
    with open(json_file_path, mode='w', encoding='utf-8') as json_file:
        json.dump(data, json_file, indent=4)

def main():
    parser = argparse.ArgumentParser(
        prog='csv_to_json.py',
        description='Convert a CSV file with CoGo tooltip data to CoGo tooltip JSON.')

    parser.add_argument('--input-csv-file', help='CSV file to convert to CoGo tooltips JSON.',
                        required=True)
    parser.add_argument('--output-file', help='CSV file to convert to CoGo tooltips JSON.',
                        required=True)
    parser.add_argument('--html-path', help='Path to "html" folder in Android assets.',
                        required=True)
    parser.add_argument('--substitution-file', help='Path to TSV file specifying any substitutions to be made in tier 3 button URIs.\nFormat <target>\\t<replacement> }.\nTo be used on placeholders in manually-written tooltip data.',
                        required=True)

    args = parser.parse_args()
    csv_to_json(args.input_csv_file, args.output_file, args.html_path, args.substitution_file)

if __name__ == "__main__":
    main()