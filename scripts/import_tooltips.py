#!/usr/bin/env python3

import sys
import os
import argparse
from tooltips import TooltipDatabase
import openpyxl

def main():
    parser = argparse.ArgumentParser(
        description='Import tooltips from Excel file to SQLite database',
        formatter_class=argparse.ArgumentDefaultsHelpFormatter
    )
    
    parser.add_argument('-i', '--import',
                      dest='xlsx_file',
                      required=True,
                      help='Path to the Excel file containing tooltips')
    
    parser.add_argument('-d', '--database',
                      dest='sqlite_db',
                      required=True,
                      help='Path to the SQLite database file to create/update')
    
    args = parser.parse_args()
    
    # Validate input files
    if not os.path.exists(args.xlsx_file):
        parser.error(f"Excel file '{args.xlsx_file}' does not exist")
    
    if not args.xlsx_file.endswith('.xlsx'):
        parser.error(f"'{args.xlsx_file}' is not an Excel file")
    
    # Create database directory if it doesn't exist
    db_dir = os.path.dirname(args.sqlite_db)
    if db_dir and not os.path.exists(db_dir):
        os.makedirs(db_dir)
    
    try:
        # Create database and import data
        db = TooltipDatabase(args.sqlite_db, args.xlsx_file)
        result = db.read_xlsx()
        
        # Print summary
        print(f"\nImport Summary:")
        print(f"Total rows in Excel: {result['total_rows']}")
        print(f"Successfully processed: {result['processed_rows']}")
        print(f"Skipped rows: {len(result['skipped_rows'])}")
        
        if result['skipped_rows']:
            print("\nSkipped rows details:")
            for row_num, error in result['skipped_rows']:
                print(f"Row {row_num}: {error}")
        
        print(f"\nSuccessfully imported tooltips from '{args.xlsx_file}' to '{args.sqlite_db}'")
        
    except Exception as e:
        parser.error(f"Error importing tooltips: {str(e)}")

if __name__ == '__main__':
    main()
