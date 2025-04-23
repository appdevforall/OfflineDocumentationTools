import argparse
import csv
import json
import os
import sys

def csv_to_json(csv_file_path, json_file_path):
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
                    if "https://kotlinlang.org/" in item[uri]:
                        item[uri] = item[uri].replace("https://kotlinlang.org/docs/",
                                                      "file:///android_asset/CoGoTooltips/html/kmenu_out_resp/")
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
        prog='generate_manual_tooltips.py',
        description='Generates tooltips for Code On The Go from the Java API documentation.')
    parser.add_argument('--out-file', help='Path to root directory of the Java API documentation.',
                        required=True)
    parser.add_argument('--input-json', help='Comma-separated list of tooltip JSON files to include in JSON output.',
                        required=True)
    csv_to_json(csv_file_path, json_file_path)

    print(f"Converted '{csv_file_path}' to '{json_file_path}'")

    java_classes_tooltips_path = os.path.join('java_docs_tooltips', 'out', 'classes.json')
    java_modules_tooltips_path = os.path.join('java_docs_tooltips', 'out', 'modules.json')
    java_packages_tooltips_path = os.path.join('java_docs_tooltips', 'out', 'packages.json')

    with open(java_classes_tooltips_path, 'r') as file:
        classes_data = json.load(file)
    with open(java_modules_tooltips_path, 'r') as file:
        modules_data = json.load(file)
    with open(java_packages_tooltips_path, 'r') as file:
        packages_data = json.load(file)
    with open(json_file_path, 'r') as file:
        internal_data = json.load(file)

    tooltips_file_path = "CoGoTooltips.json"

    json_dump = json.dumps([e for e in modules_data + packages_data + classes_data + internal_data], indent=4)
    open(tooltips_file_path, "w", encoding='utf-8').write(json_dump)

    print("Combined class tooltips with internally-written tooltips in " + tooltips_file_path)

if __name__ == "__main__":
    main()