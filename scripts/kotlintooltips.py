import json
import csv
import argparse
import os
from bs4 import BeautifulSoup

def truncate(text, limit):
    """Truncates text to a specific limit and adds an ellipsis if needed."""
    if not text:
        return ""
    if len(text) <= limit:
        return text
    return text[:limit].rstrip() + "..."

def extract_tier_2(html_path, debug=False):
    """Parses the Dokka HTML to extract paragraph text from the content div."""
    if not os.path.exists(html_path):
        if debug:
            print(f"Warning: File not found: {html_path}")
        return ""

    try:
        with open(html_path, 'r', encoding='utf-8') as f:
            soup = BeautifulSoup(f, 'html.parser')

        # Target the specific Dokka container
        content_div = soup.find('div', class_='content sourceset-dependent-content')
        
        if not content_div:
            if debug:
                print(f"No content div found in {html_path}")
            return ""

        # Extract text from <p> elements
        paragraphs = content_div.find_all('p', recursive=True)
        text_content = " ".join(p.get_text(strip=True) for p in paragraphs)
        
        return text_content
    except Exception as e:
        if debug:
            print(f"Error processing {html_path}: {e}")
        return ""

def main():
    parser = argparse.ArgumentParser(description="Generate Kotlin Tooltip CSV")
    parser.add_argument("--pages-json", required=True, help="Path to pages.json")
    parser.add_argument("--kotlin-doc-location", required=True, help="Root directory of HTML docs")
    parser.add_argument("--out", required=True, help="Output CSV path")
    parser.add_argument("--tier-1-limit", type=int, default=100)
    parser.add_argument("--tier-2-limit", type=int, default=500)
    parser.add_argument("--debug", action="store_true")

    args = parser.parse_args()

    # Load JSON data
    with open(args.pages_json, 'r', encoding='utf-8') as f:
        data = json.load(f)

    # Handle case where JSON might be a single object or a list
    entries = [data] if isinstance(data, dict) else data

    headers = [
        "categoryId", "tag", "summary", "detail", 
        "description1", "uri1", "description2", "uri2", "description3", "uri3"
    ]

    processed_rows = []
    seen_tags = set()  # Track unique tags to prevent duplicates

    i = 0
    for entry in entries:
        i+=1
        if i % 100 == 0:
            print(i)
        tag = entry.get("description", "")
        
        # Deduplication check
        if tag in seen_tags:
            if args.debug:
                print(f"Skipping duplicate tag: {tag}")
            continue
            
        location = entry.get("location", "")
        full_html_path = os.path.join(args.kotlin_doc_location, location)
        
        raw_content = extract_tier_2(full_html_path, args.debug)
        detail = truncate(raw_content, args.tier_2_limit)
        summary = truncate(raw_content, args.tier_1_limit)

        row = {
            "categoryId": 4,
            "tag": tag,
            "summary": summary,
            "detail": detail,
            "description1": "View full documentation",
            "uri1": "k/" + location,
            "description2": "",
            "uri2": "",
            "description3": "",
            "uri3": ""
        }
        
        processed_rows.append(row)
        seen_tags.add(tag)  # Mark this tag as processed

    # Write results to CSV
    with open(args.out, 'w', newline='', encoding='utf-8') as f:
        writer = csv.DictWriter(f, fieldnames=headers)
        writer.writeheader()
        writer.writerows(processed_rows)

    print(f"Successfully generated {len(processed_rows)} unique tooltips at {args.out}")

if __name__ == "__main__":
    main()