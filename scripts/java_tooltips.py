from bs4 import BeautifulSoup
import os
import json
import re
import time
import argparse
from tooltips import TooltipDatabase
import sqlite3
from tqdm_loggable.auto import tqdm
import logging

# Set up logging
logging.basicConfig(
    filename='java_tooltips_debug.log',
    level=logging.DEBUG,
    format='%(asctime)s - %(levelname)s - %(message)s'
)

def analyze_classes(directory_path):
    """
    Parse allclasses-index.html to create a mapping of class URLs to their descriptions.
    Handles both regular and deprecated classes, combining deprecation messages with their descriptions.
    
    Args:
        directory_path (str): Path to the directory containing allclasses-index.html
        
    Returns:
        dict: Mapping of class URLs to their descriptions
    """
    class_map = {}
    index_path = os.path.join(directory_path, 'allclasses-index.html')
    
    with open(index_path, 'r') as f:
        soup = BeautifulSoup(f.read(), 'html.parser')
        # Find the div containing class information
        table_panel = soup.find('div', id='all-classes-table.tabpanel')
        if table_panel:
            # Get all divs with class names containing 'col-first' and 'col-last'
            col_first_divs = table_panel.find_all('div', class_='col-first')
            col_last_divs = table_panel.find_all('div', class_='col-last')
            
            # Process each pair of divs
            for first_div, last_div in zip(col_first_divs, col_last_divs):
                # Skip header divs
                if 'table-header' in first_div.get('class', []):
                    continue
                
                # Get the class URL from the href
                class_link = first_div.find('a')
                if class_link and 'href' in class_link.attrs:
                    class_url = class_link['href']
                    
                    # Check if this is a deprecated class
                    if last_div.find(string=lambda s: s.strip() == 'Deprecated.'):
                        # Get the deprecation comment
                        deprecation_div = last_div.find('div', class_='deprecation-comment')
                        if deprecation_div:
                            description = 'Deprecated. ' + str(deprecation_div)
                        else:
                            description = 'Deprecated.'
                    else:
                        # Regular class with block description
                        block_div = last_div.find('div', class_='block')
                        if block_div:
                            description = str(block_div)
                        else:
                            description = ""
                    
                    # Replace newlines with spaces
                    description = description.replace('\n', ' ')
                    class_map[class_url] = description
    
    return class_map

def get_summary(file_path):
    """
    Extract the summary text from the HTML file.
    
    Args:
        file_path (str): Path to the HTML file to parse
        
    Returns:
        str: The summary text or empty string if not found
    """
    pass

def get_detail(file_path):
    """
    Extract the detailed description from the HTML file.
    
    Args:
        file_path (str): Path to the HTML file to parse
        
    Returns:
        str: The detailed description or empty string if not found
    """
    with open(file_path, 'r') as f:
        soup = BeautifulSoup(f.read(), 'html.parser')
        desc_section = soup.find('section', class_='class-description')
        if desc_section:
            # Get all content within the section, excluding the type-signature div
            content = []
            for element in desc_section.children:
                if element.name != 'div' or 'type-signature' not in element.get('class', []):
                    content.append(str(element))
            return str(BeautifulSoup(''.join(content), 'html.parser'))
    return "No detail found"

def normalize_button_uri(file_path, button_uri):
    """
    Normalize a button URI by converting relative paths to absolute URLs.
    
    Args:
        file_path (str): The full path to the current HTML file
        button_uri (str): The relative URI from the button
        
    Returns:
        str: The normalized absolute URL
    """
    # Get the directory path relative to the api directory
    api_index = file_path.find('/api/')
    if api_index == -1:
        return button_uri  # Return original if we can't find /api/
    
    rel_path = file_path[api_index + 5:]  # Skip '/api/'
    dir_path = os.path.dirname(rel_path)
    
    # Count the number of parent directory references
    parent_refs = button_uri.count('../')
    
    # Remove parent directory references from the button URI
    clean_uri = button_uri.replace('../', '')
    
    # Split the directory path and remove the appropriate number of directories
    dir_parts = dir_path.split('/')
    if parent_refs > 0:
        dir_parts = dir_parts[:-parent_refs]
    
    # Construct the final path
    final_path = '/'.join(dir_parts)
    if final_path:
        final_path += '/'
    
    return f"http://localhost:6174/JavaDocs/html/api/{final_path}{clean_uri}"

def get_buttons(file_path):
    """
    Extract module and package buttons from the HTML file header.
    
    Args:
        file_path (str): Path to the HTML file to parse
        
    Returns:
        list: List of button tuples (label, url) for module and package links
    """
    buttons = []
    with open(file_path, 'r') as f:
        soup = BeautifulSoup(f.read(), 'html.parser')
        header = soup.find('div', class_='header')
        if header:
            # Find all sub-title divs
            sub_titles = header.find_all('div', class_='sub-title')
            for sub_title in sub_titles:
                # Check for module
                module_span = sub_title.find('span', class_='module-label-in-type')
                if module_span and sub_title.a:
                    module_name = sub_title.a.text
                    module_url = normalize_button_uri(file_path, sub_title.a['href'])
                    buttons.append([f"Learn more about the {module_name} module", module_url])
                
                # Check for package
                package_span = sub_title.find('span', class_='package-label-in-type')
                if package_span and sub_title.a:
                    package_name = sub_title.a.text
                    package_url = normalize_button_uri(file_path, sub_title.a['href'])
                    buttons.append([f"Learn more about {package_name} package", package_url])
    
    # Add self-referential button
    rel_path = os.path.relpath(file_path, os.path.dirname(file_path))
    self_url = normalize_button_uri(file_path, rel_path)
    buttons.append(["View full documentation", self_url])
    
    return buttons

def get_doc_type(file_path):
    """
    Extract the documentation type (class/module/package) and tag from an HTML file.
    
    Args:
        file_path (str): Path to the HTML file to parse
        
    Returns:
        tuple: (type, tag) where type is the documentation type (class/module/package) or 'unknown',
              and tag is the package/module path without the last component (e.g. 'javax.xml.parsers' for DocumentBuilderFactory)
    """
    with open(file_path, 'r') as f:
        soup = BeautifulSoup(f.read(), 'html.parser')
        meta_keywords = soup.find_all('meta', attrs={'name': 'keywords'})
        for meta in meta_keywords:
            if 'content' in meta.attrs:
                content = meta['content']
                # Skip method keywords (those containing parentheses)
                if '(' in content:
                    continue
                parts = content.split()
                if len(parts) == 2:
                    tag, type = parts
                    return type, tag
    return 'unknown', 'unknown'

def traverse(directory_path, db_path):
    """
    Traverse the Java documentation structure starting from the given directory.
    
    Args:
        directory_path (str): The root directory path to start traversal from
        db_path (str): Path to the SQLite database file
    """
    # Initialize the database
    db = TooltipDatabase(db_path, None)  # None for xlsx_path since we're not importing from Excel
    
    # Get class summaries before traversal
    class_map = analyze_classes(directory_path)
    
    # Open database connection once for all processing
    conn = sqlite3.connect(db_path)
    cursor = conn.cursor()
    
    try:
        # Get total number of files to process for progress bar
        total_files = 0
        for root, dirs, files in os.walk(directory_path):
            # Remove class-use directories from the walk
            if 'class-use' in dirs:
                dirs.remove('class-use')
            # Count only files that match our criteria
            total_files += sum(1 for file in files
                             if (file.endswith('.html') and 
                                 not re.match(r'all(classes|packages|modules)-index\.html', file) and 
                                 not re.match(r'(module|package|class)-summary\.html', file) and 
                                 not re.match(r'index-\d+\.html', file) and 
                                 not re.match(r'coll-.+\.html', file) and
                                 file not in ['Object.html', 'Comparable.html', 'package-use.html', 'package-tree.html',
                                             'constant-values.html', 'overview-tree.html', 'index.html', 'new-list.html', 'threadPrimitiveDeprecation.html',
                                             'serialized-form.html', 'deprecated-list.html', 'overview-summary.html', 'ValueBased.html', 
                                             'help-doc.html', 'system-properties.html', 'preview-list.html', 'net-properties.html']))
        
        # Create progress bar
        pbar = tqdm(total=total_files, desc="Processing Java docs")
        
        for root, dirs, files in os.walk(directory_path):
            if 'class-use' in dirs:
                dirs.remove('class-use')
            for file in files:
                if (file.endswith('.html') and 
                    not re.match(r'all(classes|packages|modules)-index\.html', file) and 
                    not re.match(r'(module|package|class)-summary\.html', file) and 
                    not re.match(r'index-\d+\.html', file) and 
                    not re.match(r'coll-.+\.html', file) and
                    file not in ['Object.html', 'Comparable.html', 'package-use.html', 'package-tree.html',
                                'constant-values.html', 'overview-tree.html', 'index.html', 'new-list.html', 'threadPrimitiveDeprecation.html',
                                'serialized-form.html', 'deprecated-list.html', 'overview-summary.html', 'ValueBased.html', 
                                'help-doc.html', 'system-properties.html', 'preview-list.html', 'net-properties.html']):
                    full_path = os.path.join(root, file)
                    doc_type, tag = get_doc_type(full_path)
                    buttons = get_buttons(full_path)
                    summary = get_summary(full_path)
                    detail = get_detail(full_path)
                    category = 'java'
                    
                    # Get relative path for class lookup
                    rel_path = os.path.relpath(full_path, directory_path)
                    class_summary = class_map.get(rel_path, "No summary available")
                    
                    # Convert buttons to hash format
                    button_hashes = []
                    for descr, uri in buttons:
                        button_hash = db._create_button_hash(descr, uri)
                        if button_hash:
                            button_hashes.append(button_hash)
                    
                    # Convert button hashes to JSON string
                    button_json = json.dumps(tuple(button_hashes))
                    
                    # Insert into database
                    cursor.execute("""
                        INSERT OR REPLACE INTO ide_tooltip_table 
                        (tooltipCategory, tooltipTag, tooltipSummary, tooltipDetail, tooltipButtons) 
                        VALUES (?, ?, ?, ?, ?)
                    """, (category, tag, class_summary, detail, button_json))
                    
                    # Log debug information
                    logging.debug(f"Found type: {doc_type} tag: '{tag}' file: {file} at {full_path} category: {category}")
                    logging.debug(f"  Summary: {class_summary}")
                    logging.debug(f"  Detail: {detail}")
                    logging.debug(f"  Buttons: {buttons}")
                    logging.debug("")  # Extra newline after each entry
                    
                    # Update progress bar
                    pbar.update(1)
        
        # Close progress bar
        pbar.close()
        
        # Commit all changes at the end
        conn.commit()
    finally:
        # Always close the connection
        conn.close()

if __name__ == "__main__":
    parser = argparse.ArgumentParser(description='Process Java documentation for tooltips')
    parser.add_argument('-i', '--import', dest='java_docs_dir', required=True, help='Root directory path to start import traversal from')
    parser.add_argument('-d', '--database', dest='sqlite_db', required=True, help='Path to the SQLite database file to create/update')
    args = parser.parse_args()
    
    traverse(args.java_docs_dir, args.sqlite_db)




