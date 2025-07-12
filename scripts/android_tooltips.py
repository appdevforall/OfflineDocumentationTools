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
from android_html_page import AndroidHtmlPage
from DocumentationDatabase import DocumentationDatabase

def generate_tooltip_tag(class_name, class_url):
    """
    Generate the correct tooltipTag for a class based on its URL.
    
    Args:
        class_name (str): The class name from the HTML link
        class_url (str): The URL path for the class
        
    Returns:
        str: The tooltipTag with appropriate prefix
    """
    if class_url.startswith('/reference/android/'):
        # For android classes, add 'android.' prefix
        # Remove '/reference/android/' prefix and convert path to package notation
        package_path = class_url[18:]  # Remove '/reference/android/'
        if package_path.startswith('/'):
            package_path = package_path[1:]
        package_dots = package_path.replace('/', '.')
        return f"android.{package_dots}"
    else:
        # For androidx and other classes, just use the class name
        return class_name

def analyze_classes(directory_path):
    """
    Parse classes.html to create a mapping of class names to their descriptions and URLs.
    Handles the Android documentation format with tables containing class links and descriptions.
    
    Args:
        directory_path (str): Path to the directory containing classes.html
        
    Returns:
        dict: Mapping of class names to (description, url) tuples
    """
    class_map = {}
    classes_path = os.path.join(directory_path, 'classes.html')
    
    with open(classes_path, 'r', encoding='utf-8') as f:
        soup = BeautifulSoup(f.read(), 'html.parser')
        
        # Find all tables in the document
        tables = soup.find_all('table')
        
        for table in tables:
            # Find all rows in the table
            rows = table.find_all('tr')
            
            for row in rows:
                # Skip rows without the data-version-added attribute (likely headers)
                if not row.get('data-version-added'):
                    continue
                
                # Find the link column and description column
                link_cell = row.find('td', class_='jd-linkcol')
                desc_cell = row.find('td', class_='jd-descrcol')
                
                if link_cell and desc_cell:
                    # Extract the link
                    link = link_cell.find('a')
                    if link and 'href' in link.attrs:
                        class_url = link['href']
                        class_name = link.get_text(strip=True)
                        
                        # Filter: only include classes from /reference/android or /reference/androidx
                        if not (class_url.startswith('/reference/android/') or class_url.startswith('/reference/androidx/')):
                            continue
                        
                        # Extract the description text
                        description = desc_cell.get_text(strip=True)
                        
                        # Clean up the description - remove extra whitespace and &nbsp;
                        description = re.sub(r'\s+', ' ', description)
                        description = description.replace('&nbsp;', ' ').strip()
                        
                        class_map[class_name] = (description, class_url)
    
    return class_map

def get_detail(file_path):
    """
    Extract the detailed description from the Android HTML file.
    
    Args:
        file_path (str): Path to the HTML file to parse
        
    Returns:
        str: The detailed description or empty string if not found
    """
    if not os.path.exists(file_path):
        logging.warning(f"File does not exist for get_detail: {file_path}")
        return "No detail found - file missing"
    
    try:
        with open(file_path, 'r', encoding='utf-8') as f:
            soup = BeautifulSoup(f.read(), 'html.parser')
            
            # Look for the description section in Android docs
            desc_section = soup.find('div', class_='jd-details-descr')
            if desc_section:
                # Get all content within the description section
                content = []
                for element in desc_section.children:
                    if element.name:
                        content.append(str(element))
                return str(BeautifulSoup(''.join(content), 'html.parser'))
            
            # Fallback: look for any description-like content
            desc_divs = soup.find_all('div', class_=lambda x: x and 'descr' in x)
            if desc_divs:
                return str(desc_divs[0])
    except Exception as e:
        logging.error(f"Error reading file {file_path}: {str(e)}")
        return f"No detail found - error reading file: {str(e)}"
    
    return "No detail found"

def normalize_button_uri(file_path, button_uri):
    """
    Normalize a button URI by converting relative paths to absolute URLs for Android docs.
    
    Args:
        file_path (str): The full path to the current HTML file
        button_uri (str): The relative URI from the button
        
    Returns:
        str: The normalized absolute URL
    """
    # If it's already an absolute URL, return as is
    if button_uri.startswith('http'):
        return button_uri
    
    # For Android docs, convert relative paths to absolute localhost URLs
    if button_uri.startswith('/reference/'):
        # Remove the leading slash and add the localhost prefix
        clean_uri = button_uri[1:]  # Remove leading '/'
        return f"http://localhost:6174/AndroidDocs/API/Classes/developer.android.com/{clean_uri}.html"
    
    # For other relative paths, try to construct based on the current file location
    # Get the directory path relative to the API directory
    api_index = file_path.find('/API/')
    if api_index != -1:
        rel_path = file_path[api_index + 5:]  # Skip '/API/'
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
        
        return f"http://localhost:6174/AndroidDocs/API/{final_path}{clean_uri}"
    
    return button_uri

def get_buttons(file_path):
    """
    Extract navigation buttons from the Android HTML file header.
    
    Args:
        file_path (str): Path to the HTML file to parse
        
    Returns:
        list: List of button tuples (label, url) for navigation links
    """
    buttons = []
    
    if not os.path.exists(file_path):
        logging.warning(f"File does not exist for get_buttons: {file_path}")
        return buttons
    
    try:
        with open(file_path, 'r', encoding='utf-8') as f:
            soup = BeautifulSoup(f.read(), 'html.parser')
            
            # Look for breadcrumb navigation or package links
            breadcrumbs = soup.find_all('a', href=True)
            for link in breadcrumbs:
                href = link.get('href')
                text = link.get_text(strip=True)
                
                # Look for package or class references
                if href and text and ('package' in href.lower() or 'class' in href.lower()):
                    normalized_url = normalize_button_uri(file_path, href)
                    buttons.append([f"View {text}", normalized_url])
        
        # Add self-referential button
        rel_path = os.path.relpath(file_path, os.path.dirname(file_path))
        self_url = normalize_button_uri(file_path, rel_path)
        buttons.append(["View full documentation", self_url])
    except Exception as e:
        logging.error(f"Error reading file {file_path}: {str(e)}")
    
    return buttons

def get_doc_type(file_path):
    """
    Extract the documentation type (class/module/package) and tag from an Android HTML file.
    
    Args:
        file_path (str): Path to the HTML file to parse
        
    Returns:
        tuple: (type, tag) where type is the documentation type (class/module/package) or 'unknown',
              and tag is the package/module path without the last component
    """
    if not os.path.exists(file_path):
        logging.warning(f"File does not exist for get_doc_type: {file_path}")
        return ('unknown', 'android')
    
    try:
        with open(file_path, 'r', encoding='utf-8') as f:
            soup = BeautifulSoup(f.read(), 'html.parser')
            
            # Look for title or heading that indicates the type
            title = soup.find('title')
            if title:
                title_text = title.get_text()
                if 'Class' in title_text:
                    return ('class', 'android')
                elif 'Package' in title_text:
                    return ('package', 'android')
                elif 'Module' in title_text:
                    return ('module', 'android')
            
            # Look for breadcrumb navigation to determine type
            breadcrumbs = soup.find_all('a', href=True)
            for link in breadcrumbs:
                href = link.get('href')
                if href and 'package' in href:
                    return ('package', 'android')
                elif href and 'class' in href:
                    return ('class', 'android')
    except Exception as e:
        logging.error(f"Error reading file {file_path}: {str(e)}")
    
    return ('unknown', 'android')

def get_all_file_data(file_path):
    """
    Extract all data from an Android HTML file in a single read operation.
    
    Args:
        file_path (str): Path to the HTML file to parse
        
    Returns:
        tuple: (detail, buttons, doc_type, tag) where:
            - detail is the detailed description
            - buttons is a list of button tuples (label, url)
            - doc_type is the documentation type (class/module/package)
            - tag is the package/module path
    """
    if not os.path.exists(file_path):
        logging.warning(f"File does not exist for get_all_file_data: {file_path}")
        return ("No detail found - file missing", [], 'unknown', 'android')
    
    try:
        with open(file_path, 'r', encoding='utf-8') as f:
            content = f.read()
            
            # Extract detail using regex for speed
            detail = "No detail found"
            import re
            desc_match = re.search(r'<div[^>]*class="[^"]*jd-details-descr[^"]*"[^>]*>(.*?)</div>', content, re.DOTALL | re.IGNORECASE)
            if desc_match:
                detail = desc_match.group(1).strip()
                # Clean up HTML tags
                detail = re.sub(r'<[^>]+>', '', detail)
                detail = re.sub(r'\s+', ' ', detail).strip()
                if len(detail) > 1000:
                    detail = detail[:1000] + "..."
            
            # Only include a button to the exact page for the specific class
            rel_path = os.path.relpath(file_path, os.path.dirname(file_path))
            self_url = normalize_button_uri(file_path, rel_path)
            buttons = [["View full documentation", self_url]]
            
            # Extract doc type - simplified
            doc_type = 'class'  # Default to class since we're processing classes
            
            return (detail, buttons, doc_type, 'android')
            
    except Exception as e:
        logging.error(f"Error reading file {file_path}: {str(e)}")
        return (f"No detail found - error reading file: {str(e)}", [], 'unknown', 'android')

def traverse(directory_path, db_path, debug_mode=False):
    """
    Traverse the Android documentation directory and populate the tooltip database.
    
    Args:
        directory_path (str): Path to the Android documentation directory
        db_path (str): Path to the SQLite database file
        debug_mode (bool): If True, only process the first 10 items
    """
    # First, analyze the classes.html to get class mappings
    class_map = analyze_classes(directory_path)
    
    # In debug mode, limit to first 10 items
    if debug_mode:
        class_map = dict(list(class_map.items())[:10])
        print(f"DEBUG MODE: Processing only first 10 items out of {len(analyze_classes(directory_path))} total")
    
    # Ensure database schema is properly initialized
    doc_db = None
    try:
        # Try to initialize DocumentationDatabase
        doc_db = DocumentationDatabase(db_path)
        print(f"Content database initialized: {db_path}")
    except ValueError as e:
        # Database exists but has missing tables - create missing tables using DocumentationDatabase methods
        print(f"Database schema incomplete, creating missing tables: {e}")
        with sqlite3.connect(db_path) as conn:
            cursor = conn.cursor()
            
            # Use DocumentationDatabase methods to create tables and populate data
            temp_db = DocumentationDatabase.__new__(DocumentationDatabase)
            temp_db.database_path = db_path
            temp_db.create_tables(cursor)
            temp_db.populate_content_types(cursor)
            temp_db.populate_languages(cursor)
            
            conn.commit()
        
        # Now try to initialize DocumentationDatabase again
        doc_db = DocumentationDatabase(db_path)
        print(f"Content database schema completed: {db_path}")
    except Exception as e:
        print(f"Warning: Could not initialize content database: {e}")
        print("HTML content will not be stored in Content table")
    
    # Create the tooltip table if it doesn't exist
    with sqlite3.connect(db_path) as conn:
        cursor = conn.cursor()
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS `ide_tooltip_table` (
            `tooltipCategory` TEXT NOT NULL, 
            `tooltipTag` TEXT NOT NULL, 
            `tooltipSummary` TEXT NOT NULL, 
            `tooltipDetail` TEXT NOT NULL, 
            `tooltipButtons` TEXT NOT NULL, 
            PRIMARY KEY(`tooltipCategory`, `tooltipTag`));
        """)
        conn.commit()
    
    # Process all classes with proper connection management
    processed_count = 0
    error_count = 0
    
    # Prepare batch insert data
    batch_data = []
    batch_size = 100  # Process in batches of 100

    # Process each class in the mapping
    for class_name, (description, class_url) in tqdm(class_map.items(), desc="Processing Android classes"):
        try:
            # Construct the full file path
            if class_url.startswith('/reference/'):
                # Remove the /reference/ prefix and any leading slash, then add .html extension
                relative_path = class_url[10:]  # Remove '/reference/' (10 characters)
                if relative_path.startswith('/'):
                    relative_path = relative_path[1:]
                relative_path += '.html'
                full_path = os.path.join(directory_path, relative_path)
            else:
                full_path = os.path.join(directory_path, class_url + '.html')
            
            # Check if the file exists at the expected path
            if not os.path.exists(full_path):
                print(f"Skipping missing file for class '{class_name}': {full_path}")
                logging.warning(f"File not found for class '{class_name}': {full_path}")
                error_count += 1
                continue
            
            # Get additional information (read file once and parse all data)
            detail, buttons, doc_type, tag = get_all_file_data(full_path)
            
            # Extract class name from URL
            # The class_name is already the name from analyze_classes, so no need to re-extract
            
            # Create button JSON
            button_data = []
            for button in buttons[:3]:  # Limit to 3 buttons
                if len(button) >= 2:
                    button_data.append({
                        "first": button[0],
                        "second": button[1]
                    })
            
            button_json = json.dumps(button_data)
            
            # Generate tooltipTag
            tooltip_tag = generate_tooltip_tag(class_name, class_url)
            
            # Add to batch data
            batch_data.append((doc_type, tooltip_tag, description, detail, button_json))
            
            # Create AndroidHtmlPage and add to Content table
            if doc_db:
                try:
                    android_page = AndroidHtmlPage(full_path)
                    html_content = android_page.get_html_page()
                    
                    if html_content and not html_content.strip().startswith("<html><head><title>Class Documentation</title></head><body><p>File not found"):
                        # Create a path for the content table (relative to the documentation root)
                        content_path = os.path.relpath(full_path, directory_path)
                        # Add the cleaned HTML content to the Content table
                        doc_db.add_file(content_path, html_content.encode('utf-8'), 'en-US')
                except Exception as e:
                    logging.warning(f"Failed to add HTML content for {class_name}: {str(e)}")
            
            # Process batch when it reaches the batch size
            if len(batch_data) >= batch_size:
                with sqlite3.connect(db_path) as conn:
                    cursor = conn.cursor()
                    cursor.executemany("""
                        INSERT OR REPLACE INTO ide_tooltip_table 
                        (tooltipCategory, tooltipTag, tooltipSummary, tooltipDetail, tooltipButtons) 
                        VALUES (?, ?, ?, ?, ?)
                    """, batch_data)
                    conn.commit()
                processed_count += len(batch_data)
                batch_data = []
                logging.debug(f"Processed batch of {batch_size} items")
            
        except Exception as e:
            logging.error(f"Error processing {class_name}: {str(e)}")
            error_count += 1
            continue
    
    # Process any remaining batch data
    if batch_data:
        with sqlite3.connect(db_path) as conn:
            cursor = conn.cursor()
            cursor.executemany("""
                INSERT OR REPLACE INTO ide_tooltip_table 
                (tooltipCategory, tooltipTag, tooltipSummary, tooltipDetail, tooltipButtons) 
                VALUES (?, ?, ?, ?, ?)
            """, batch_data)
            conn.commit()
        processed_count += len(batch_data)
        logging.debug(f"Processed final batch of {len(batch_data)} items")
    
    print(f"Database commit completed. Processed: {processed_count}, Errors: {error_count}")
    
    # Verify the records were actually inserted
    with sqlite3.connect(db_path) as conn:
        cursor = conn.cursor()
        cursor.execute("SELECT COUNT(*) FROM ide_tooltip_table")
        total_records = cursor.fetchone()[0]
        print(f"Total records in database: {total_records}")

def main():
    """
    Main function to process Android documentation and create tooltips.
    """
    parser = argparse.ArgumentParser(description='Process Android documentation for tooltips')
    parser.add_argument('--input', required=True, help='Path to the directory containing Android classes.html (e.g., SourceDocs/AndroidDocs/API/Classes/developer.android.com/reference)')
    parser.add_argument('--output', required=True, help='Path to the output SQLite database file to create or update (e.g., tooltips_android.db)')
    parser.add_argument('--debug', action='store_true', help='Debug mode: only process first 10 items')
    
    args = parser.parse_args()
    
    if not os.path.exists(args.input):
        print(f"Error: Input directory {args.input} does not exist")
        return 1
    
    print(f"Processing Android documentation from {args.input}")
    print(f"Output database: {args.output}")
    if args.debug:
        print("DEBUG MODE ENABLED: Processing only first 10 items")
    
    traverse(args.input, args.output, debug_mode=args.debug)
    
    print("Android tooltip processing completed!")
    return 0

if __name__ == '__main__':
    exit(main())
