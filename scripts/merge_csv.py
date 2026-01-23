import csv
import argparse
import sys

def merge_csv(primary_file, secondary_file, output_file, key_fields):
    try:
        # 1. Read the primary file to identify existing records
        existing_keys = set()
        with open(primary_file, mode='r', newline='', encoding='utf-8-sig') as f:
            reader = csv.DictReader(f)
            fieldnames = reader.fieldnames
            for row in reader:
                # Create a unique identifier based on the specified fields
                key = tuple(row[field] for field in key_fields)
                existing_keys.add(key)

        # 2. Open output file (writing the original data first)
        # We use 'w' to create a new file, or 'a' if you prefer appending in-place
        with open(output_file, mode='w', newline='', encoding='utf-8-sig') as out_f:
            writer = csv.DictWriter(out_f, fieldnames=fieldnames)
            writer.writeheader()
            
            # Re-read primary to copy to output
            with open(primary_file, mode='r', newline='', encoding='utf-8-sig') as f:
                primary_reader = csv.DictReader(f)
                for row in primary_reader:
                    writer.writerow(row)

            # 3. Process secondary file and append non-duplicates
            with open(secondary_file, mode='r', newline='', encoding='utf-8-sig') as f2:
                secondary_reader = csv.DictReader(f2)
                
                # Basic check to ensure columns match
                if not set(key_fields).issubset(set(secondary_reader.fieldnames)):
                    print(f"Error: Key fields {key_fields} not found in secondary file.")
                    return

                for row in secondary_reader:
                    key = tuple(row[field] for field in key_fields)
                    if key not in existing_keys:
                        writer.writerow(row)
                        existing_keys.add(key) # Prevent duplicates within the second file too

        print(f"Successfully merged into {output_file}")

    except FileNotFoundError as e:
        print(f"File error: {e}")
    except KeyError as e:
        print(f"Field name error: Could not find column {e}")

if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Merge two CSV files based on unique fields.")
    parser.add_argument("--primary", required=True, help="The first (base) CSV file")
    parser.add_argument("--secondary", required=True, help="The second CSV file to pull from")
    parser.add_argument("--output", required=True, help="The resulting merged CSV file")
    parser.add_argument("--keys", required=True, nargs='+', help="The field(s) to check for uniqueness")

    args = parser.parse_args()

    merge_csv(args.primary, args.secondary, args.output, args.keys)