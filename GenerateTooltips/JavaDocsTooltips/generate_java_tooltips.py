import argparse
from bs4 import BeautifulSoup
import json
import os
import re
import time

# CONSTANTS

# Global keys for tooltip entry dict
# Entry name (package/class/module)
ENTRY_NAME = 'tag'
# Entry summary (tier 1)
ENTRY_SUMMARY = 'summary'
# Entry description (tier 2)
ENTRY_DESC = 'detail'
# buttonList (tier 3)
BUTTON_LIST = 'buttons'
# Ccategory
CATEGORY = 'category'

ENTRY_STATUSES = dict()
ENTRY_STATUSES['has_content'] = 0
ENTRY_STATUSES['no_content'] = 1

ENTRY_STATUS_MSGS = dict()
ENTRY_STATUS_MSGS[ENTRY_STATUSES['has_content']] = "has content"
ENTRY_STATUS_MSGS[ENTRY_STATUSES['no_content']] = "has no content"

# LOGGING
START_TIME = time.time()
def log(msg, log_handle, precision=4):
    msg_with_time = str(round(time.time() - START_TIME, precision)) + ": " + msg
    print(msg_with_time)
    log_handle.write(msg_with_time + "\n")

# Get HTML in description.
# Single newlines are ignored in block display
# and multiple spaces are ignored by default.
def get_normalized_html(tag):
    html = "".join([str(c) for c in tag.contents])

    single_newlines_removed = re.sub('(?<!\n)\r?\n(?!\r?\n)', '', html)
    multiple_spaces_removed = re.sub(' +', ' ', single_newlines_removed)

    return multiple_spaces_removed


def gen_tooltip_entry(name, summary, desc, url, article_type, device_javadoc_path, verbose=False):
    entry = dict()

    # name
    entry[ENTRY_NAME] = name
    # tier 1 summary
    entry[ENTRY_SUMMARY] = summary
    # tier 2 desc
    entry[ENTRY_DESC] = desc
    # tier 3 link message
    entry[BUTTON_LIST] = [['Learn more about ' + article_type + " " + name,
                           device_javadoc_path + url]]

    # hard-coded java category
    entry[CATEGORY] = 'java'

    if verbose:
        print("Processed " + article_type + " " + name)

    return entry

# Process two-column javadoc table for package/module/class names, descriptions, and article links
def process_table(all_divs, entry_type, log_handle):
    # Package name
    names = []

    # Summary (tier 1)
    summaries = []

    # Package page URLs (tier 3)
    urls = []

    for cell in all_divs:
        if cell.has_attr('class') and 'table-header' in cell['class']:
            continue

        if cell.has_attr('class') and 'col-first' in cell['class']:
            names += [cell.get_text()]
            urls += [cell.a['href']]
        if cell.has_attr('class') and 'col-last' in cell['class']:
            if cell.div:
                html = get_normalized_html(cell.div)
                status = ENTRY_STATUSES['has_content']
                summaries += [html]
            else:
                status = ENTRY_STATUSES['no_content']
                summaries += [""]

            log(entry_type + " " + names[-1] + " summary " + ENTRY_STATUS_MSGS[status], log_handle)

    return names, summaries, urls


def process_index(index_file, docs_path, log_handle, device_javadoc_path, entry_type):
    html = open(index_file, "r").read()
    soup = BeautifulSoup(html, 'html.parser')

    entries = []

    log("Processing table in " + index_file, log_handle)
    names, summaries, urls = process_table(soup.find_all('div'), entry_type, log_handle)
    log("Finished processing table in " + index_file, log_handle)

    for data in zip(names, summaries, urls):
        name, summary, url = data
        log("Processing " + entry_type + " " + name, log_handle)

        detail_html = open(os.path.join(docs_path, url), 'r').read()

        # summary_soup = BeautifulSoup(package_summary_html, 'html.parser')
        detail_soup = BeautifulSoup(detail_html, 'lxml')
        desc_section = detail_soup.find('section', id=entry_type + '-description')

        if not desc_section:
            desc = [""]
            status = ENTRY_STATUSES['no_content']
        else:
            desc_div = desc_section.find('div', class_="block")
            if not desc_div:
                desc = [""]
                status = ENTRY_STATUSES['no_content']
            else:
                desc = get_normalized_html(desc_div)
                status = ENTRY_STATUSES['has_content']

        log(entry_type + " " + name + " detail " + ENTRY_STATUS_MSGS[status], log_handle)

        entries += [gen_tooltip_entry(name, summary, desc, url, entry_type, device_javadoc_path)]

    return entries

# DEVICE_PATH_TO_JAVADOCS = 'file:///android_asset/CoGoTooltips/external/javadocs/api/'

def main():
    parser = argparse.ArgumentParser(
        prog='generate_java_tooltips.py',
        description='Generates tooltips for Code On The Go from the Java API documentation.')
    parser.add_argument('--path-to-docs', help='Path to root directory of the Java API documentation.',
                        required=True)
    parser.add_argument('--out-path', help='Path to output directory.',
                        required=True)
    parser.add_argument('--log-file', help='Path to output log file.',
                        required=True)
    parser.add_argument('--device-javadoc-path', help='Path to Java API docs on Android device.',
                        required=True)

    args = parser.parse_args()

    docs_path = os.path.join(args.path_to_docs, "api")
    out_path = args.out_path

    device_javadoc_path = args.device_javadoc_path

    log_file = args.log_file

    log_dir = os.path.dirname(log_file)

    if log_dir and not os.path.exists(log_dir):
        os.makedirs(log_dir)

    if os.path.exists(log_file):
        open(log_file, "w").write("")

    log_handle = open(log_file, "a")

    if not os.path.exists(out_path):
        os.makedirs(out_path)

    #entries = process_classes(os.path.join(DOCS_ROOT, "allclasses-index.html"))
    log("Processing modules", log_handle)
    entries = process_index(os.path.join(docs_path, 'index.html'), docs_path, log_handle, device_javadoc_path, "module")
    json_dump = json.dumps([e for e in entries], indent=4)
    open(os.path.join(out_path, "modules.json"), "w", encoding='utf-8').write(json_dump)

    log("Processing packages", log_handle)
    entries = process_index(os.path.join(docs_path, 'allpackages-index.html'), docs_path, log_handle, device_javadoc_path, "package")
    json_dump = json.dumps([e for e in entries], indent=4)
    open(os.path.join(out_path, "packages.json"), "w", encoding='utf-8').write(json_dump)

    log("Processing classes", log_handle)
    entries = process_index(os.path.join(docs_path, 'allclasses-index.html'), docs_path, log_handle, device_javadoc_path, "class")
    json_dump = json.dumps([e for e in entries], indent=4)
    open(os.path.join(out_path, "classes.json"), "w", encoding='utf-8').write(json_dump)


if __name__ == "__main__":
    main()