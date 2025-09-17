from bs4 import BeautifulSoup
import os
import json
import re
import time

# Path to Java docs
DOCS_ROOT = os.path.join('..', 'SourceDocs', 'JavaDocs', 'html', 'api')
OUT_DIR = "out"

if not os.path.exists(OUT_DIR):
    os.makedirs(OUT_DIR)

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

DEVICE_PATH_TO_JAVADOCS = 'j/'

ENTRY_STATUS_MSGS = dict()
ENTRY_STATUS_MSGS[ENTRY_STATUSES['has_content']] = "has content"
ENTRY_STATUS_MSGS[ENTRY_STATUSES['no_content']] = "has no content"

LOGFILE = "log.txt"

# LOGGING

START_TIME = time.time()
PRINT_LOG_MSGS = True

# Clear log file
open(LOGFILE, "w").write("")

# Open log file in append mode
LOG_HANDLE = open(LOGFILE, "a")


def log(msg, precision=4):
    msg_with_time = str(round(time.time() - START_TIME, precision)) + ": " + msg
    if PRINT_LOG_MSGS:
        print(msg_with_time)
    LOG_HANDLE.write(msg_with_time + "\n")


# Get HTML in description.
# Single newlines are ignored in block display
# and multiple spaces are ignored by default.
def get_normalized_html(tag):
    text = tag.get_text()

    single_newlines_removed = re.sub('(?<!\n)\r?\n(?!\r?\n)', '', text)
    multiple_spaces_removed = re.sub(' +', ' ', single_newlines_removed)
    final_text = multiple_spaces_removed.strip()

    return final_text


def gen_tooltip_entry(name, summary, desc, url, article_type, verbose=False):
    entry = dict()

    # name
    entry[ENTRY_NAME] = name
    # tier 1 summary
    entry[ENTRY_SUMMARY] = summary
    # tier 2 desc
    entry[ENTRY_DESC] = desc
    # tier 3 link message
    entry[BUTTON_LIST] = [['Learn more about ' + article_type + " " + name,
                           DEVICE_PATH_TO_JAVADOCS + url]]

    # hard-coded java category
    entry[CATEGORY] = 'java'

    if verbose:
        print("Processed " + article_type + " " + name)

    return entry


# Process two-column javadoc table for package/module/class names, descriptions, and article links
def process_table(all_divs, entry_type):
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

            log(entry_type + " " + names[-1] + " summary " + ENTRY_STATUS_MSGS[status])

    return names, summaries, urls


def process_index(index_file, entry_type, truncate_len=500):
    html = open(index_file, "r").read()
    soup = BeautifulSoup(html, 'html.parser')

    entries = []

    log("Processing table in " + index_file)
    names, summaries, urls = process_table(soup.find_all('div'), entry_type)
    log("Finished processing table in " + index_file)

    for data in zip(names, summaries, urls):
        name, summary, url = data
        log("Processing " + entry_type + " " + name)

        detail_html = open(os.path.join(DOCS_ROOT, url), 'r').read()

        # summary_soup = BeautifulSoup(package_summary_html, 'html.parser')
        detail_soup = BeautifulSoup(detail_html, 'lxml')

        # Determine the fully-qualified name
        if entry_type == "class":
            # Try to get the full name from the 'keywords' meta tag first
            meta_keywords = detail_soup.find('meta', {'name': 'keywords'})
            if meta_keywords and meta_keywords.get('content'):
                content = meta_keywords['content']
                # The full name is the first part of the string, before 'class' or 'interface'
                full_name_match = re.search(r'([\w\.]+)\s+(?:class|interface)', content)
                if full_name_match:
                    full_name = full_name_match.group(1)
            else:
                # Fallback to the previous method if 'keywords' tag is not found or empty
                meta_description = detail_soup.find('meta', {'name': 'description'})
                if meta_description:
                    content = meta_description['content']
                    match = re.search(r'declaration: module: (.*?), package: (.*?), (interface|class): (.*)', content)
                    if match:
                        module_name, package_name, article_type, simple_name = match.groups()
                        full_name = f"{package_name}.{simple_name}"
        elif entry_type == "package":
            # Find the package name in the sub-title div
            package_name_div = detail_soup.find('div', class_='package-signature')
            full_name = package_name_div.find('span', class_='element-name').get_text().strip()
        elif entry_type == "module":
            # Find the module name in the module-signature div
            module_name_div = detail_soup.find('div', class_='module-signature')
            full_name = module_name_div.find('span', class_='element-name').get_text().strip()
        else:
            full_name = name

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

        log(entry_type + " " + name + " detail " + ENTRY_STATUS_MSGS[status])

        entries += [gen_tooltip_entry(full_name, summary, desc, url, entry_type)]

    return entries


def main():
    # entries = process_classes(os.path.join(DOCS_ROOT, "allclasses-index.html"))
    log("Processing modules ")
    entries_module = process_index(os.path.join(DOCS_ROOT, 'index.html'), "module")

    log("Processing packages")
    entries_package = process_index(os.path.join(DOCS_ROOT, 'allpackages-index.html'), "package")

    log("Processing classes")
    entries_classes = process_index(os.path.join(DOCS_ROOT, 'allclasses-index.html'), "class")

    all_entries = entries_module + entries_package + entries_classes
    full_json = json.dumps(all_entries, indent=4)
    open(os.path.join(OUT_DIR, "java_tooltips.json"), "w", encoding='utf-8').write(full_json)


if __name__ == "__main__":
    main()