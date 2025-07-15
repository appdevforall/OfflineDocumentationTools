import argparse
import pickle
import sqlite3
import os
import sys
from DocumentationDatabase import DocumentationDatabase

def load_android_data(pickle_path, database_path, category):
    """
    Load Android tooltip data from pickle file into the ide_tooltip_table.
    
    Args:
        pickle_path (str): Path to the pickle file containing tooltip data
        database_path (str): Path to the SQLite database
        category (str): Category for the tooltips ('java' or 'kotlin')
    """
    # Validate category
    if category not in ['java', 'kotlin']:
        raise ValueError("Category must be either 'java' or 'kotlin'")
    
    # Check if pickle file exists
    if not os.path.exists(pickle_path):
        raise FileNotFoundError(f"Pickle file not found: {pickle_path}")
    
    # Check if database exists
    if not os.path.exists(database_path):
        raise FileNotFoundError(f"Database file not found: {database_path}")
    
    # Load pickle data
    print(f"Loading tooltip data from: {pickle_path}")
    with open(pickle_path, 'rb') as f:
        tooltip_data = pickle.load(f)
    
    print(f"Found {len(tooltip_data)} tooltip records")
    
    # Connect to database
    conn = sqlite3.connect(database_path)
    cursor = conn.cursor()
    
    # Prepare insert statement
    insert_sql = """
    INSERT OR REPLACE INTO ide_tooltip_table 
    (tooltipCategory, tooltipTag, tooltipSummary, tooltipDetail, tooltipButtons)
    VALUES (?, ?, ?, ?, ?)
    """
    
    # Insert data
    inserted_count = 0
    for tooltip_tag, tooltip_info in tooltip_data.items():
        try:
            cursor.execute(insert_sql, (
                category,
                tooltip_tag,
                tooltip_info['tooltipSummary'],
                tooltip_info['tooltipDetail'],
                tooltip_info['tooltipButtons']
            ))
            inserted_count += 1
        except Exception as e:
            print(f"Error inserting tooltip for {tooltip_tag}: {str(e)}")
            continue
    
    # Commit changes
    conn.commit()
    conn.close()
    
    print(f"Successfully inserted {inserted_count} tooltip records with category '{category}'")

def process_directory(db, directory_path, directory_name):
    """
    Process a directory and insert all HTML files into the database.
    
    Args:
        db: DocumentationDatabase instance
        directory_path (str): Path to the directory to process
        directory_name (str): Name of the directory for logging purposes
        
    Returns:
        int: Number of files inserted
    """
    if not os.path.exists(directory_path):
        print(f"{directory_name} directory not found: {directory_path}")
        return 0
    
    print(f"Processing {directory_name} content directory: {directory_path}")
    inserted_count = 0
    
    for root, dirs, files in os.walk(directory_path):
        for file in files:
            if file.endswith('.html'):
                file_path = os.path.join(root, file)
                try:
                    # Read file content
                    with open(file_path, 'rb') as f:
                        content = f.read()
                    
                    # Normalize the path to start with "AndroidDocs/android" or "AndroidDocs/androidx"
                    rel_path = os.path.relpath(file_path, directory_path)
                    if directory_name == "Android":
                        normalized_path = f"AndroidDocs/android/{rel_path}"
                    else:  # AndroidX
                        normalized_path = f"AndroidDocs/androidx/{rel_path}"
                    
                    # Use DocumentationDatabase.add_file() method with normalized path
                    if db.add_file(normalized_path, content, 'en-US'):
                        inserted_count += 1
                    
                except Exception as e:
                    print(f"Error processing {directory_name} file {file_path}: {str(e)}")
                    continue
    
    print(f"Inserted {inserted_count} {directory_name} HTML files")
    return inserted_count

def load_content_files(database_path, android_dir=None, androidx_dir=None):
    """
    Load HTML content files from Android and AndroidX directories into the Content table.
    
    Args:
        database_path (str): Path to the SQLite database
        android_dir (str): Path to Android HTML directory (optional)
        androidx_dir (str): Path to AndroidX HTML directory (optional)
    """
    if not android_dir and not androidx_dir:
        print("No content directories specified, skipping content loading")
        return
    
    # Create DocumentationDatabase instance
    db = DocumentationDatabase(database_path)
    
    total_inserted = 0
    
    # Process Android directory
    if android_dir:
        total_inserted += process_directory(db, android_dir, "Android")
    
    # Process AndroidX directory
    if androidx_dir:
        total_inserted += process_directory(db, androidx_dir, "AndroidX")
    
    print(f"Successfully inserted {total_inserted} total HTML files into Content table")

def main():
    """
    Main function to load Android tooltip data and content files into database.
    """
    parser = argparse.ArgumentParser(description='Load Android tooltip data and content files into database')
    parser.add_argument('--pickle', '-p', 
                       help='Path to the pickle file (e.g., ProcessDocs/AndroidDocs/android-tooltips.pkl)')
    parser.add_argument('--database', '-d', required=True, 
                       help='Path to the SQLite database')
    parser.add_argument('--category', '-c', choices=['java', 'kotlin'],
                       help='Category for the tooltips (java or kotlin)')
    parser.add_argument('--android', help='Path to Android HTML directory (optional)')
    parser.add_argument('--androidx', help='Path to AndroidX HTML directory (optional)')
    
    args = parser.parse_args()
    
    try:
        # Load tooltip data if pickle file is provided
        if args.pickle:
            load_android_data(args.pickle, args.database, args.category)
        else:
            print("No pickle file provided, skipping tooltip loading")
        
        # Load content files
        load_content_files(args.database, args.android, args.androidx)
        
        print("Android data loading completed!")
        return 0
    except Exception as e:
        print(f"Error: {str(e)}")
        return 1

if __name__ == '__main__':
    exit(main())
