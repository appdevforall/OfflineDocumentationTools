#!/usr/bin/env python3
import os
import re
import argparse
from urllib.parse import urlparse, unquote

def write_log(output_file, lines):
    """Helper function to write results to a specified output file."""
    if output_file:
        try:
            with open(output_file, 'w', encoding='utf-8') as f:
                for line in lines:
                    f.write(line + '\n')
            print(f"📄 Results successfully logged to: {output_file}")
        except Exception as e:
            print(f"⚠️ Failed to write to output file {output_file}: {e}")

def check_list(file_list, pebble_dir, output_file):
    """
    Verifies that every file in a given list exists in the rendered JSON+Pebble docs.
    """
    print(f"--- Running List Check ---")
    print(f"File list: {file_list}")
    print(f"Pebble directory: {pebble_dir}")

    missing_files = []
    try:
        with open(file_list, 'r', encoding='utf-8') as f:
            for line in f:
                path = line.strip()
                if not path:
                    continue

                # Strip leading './' if present
                if path.startswith('./'):
                    path = path[2:]

                # If the list contains .json files, expect .html in the rendered output
                if path.endswith('.json'):
                    path = path[:-5] + '.html'

                full_path = os.path.join(pebble_dir, path)

                if not os.path.exists(full_path):
                    missing_files.append(path)

    except FileNotFoundError:
        print(f"Error: The file list '{file_list}' was not found.")
        return

    if missing_files:
        print(f"❌ FAILED: {len(missing_files)} files from the list are missing in the rendered output.")
        for missing in missing_files[:15]:
            print(f"   - {missing}")
        if len(missing_files) > 15:
            print(f"   ... and {len(missing_files) - 15} more.")

        # Write missing files to output file if requested
        write_log(output_file, missing_files)
    else:
        print("✅ SUCCESS: All files in the list exist in the rendered documentation.")
    print()

def compare_base(base_dir, pebble_dir, output_file):
    """
    Verifies that every generated HTML file in the base plugin's documentation
    has a corresponding HTML page produced by JSON+Pebble.
    """
    print(f"--- Running Base Comparison Check ---")
    print(f"Base docs directory: {base_dir}")
    print(f"Pebble directory: {pebble_dir}")

    if not os.path.isdir(base_dir):
        print(f"Error: Base directory '{base_dir}' not found.")
        return

    missing_files = []
    total_files = 0

    for root, _, files in os.walk(base_dir):
        for file in files:
            if file.endswith('.html'):
                total_files += 1
                base_path = os.path.join(root, file)
                rel_path = os.path.relpath(base_path, base_dir)
                pebble_path = os.path.join(pebble_dir, rel_path)

                if not os.path.exists(pebble_path):
                    missing_files.append(rel_path)

    if missing_files:
        print(f"❌ FAILED: {len(missing_files)} out of {total_files} HTML files from the base docs are missing in the Pebble docs.")
        for missing in missing_files[:15]:
            print(f"   - {missing}")
        if len(missing_files) > 15:
            print(f"   ... and {len(missing_files) - 15} more.")

        # Write missing files to output file if requested
        write_log(output_file, missing_files)
    else:
        print(f"✅ SUCCESS: All {total_files} HTML files from the base documentation exist in the Pebble documentation.")
    print()

def check_links(docs_dir, output_file):
    """
    Verifies all internal links to other pages, tracking status for every link.
    """
    print(f"--- Running Link Status Check ---")
    print(f"Documentation directory: {docs_dir}")

    if not os.path.isdir(docs_dir):
        print(f"Error: Documentation directory '{docs_dir}' not found.")
        return

    link_log_lines = []
    broken_count = 0
    total_links_checked = 0

    # Regex to find href attributes
    href_regex = re.compile(r'href\s*=\s*["\']([^"\']+)["\']', re.IGNORECASE)

    for root, _, files in os.walk(docs_dir):
        for file in files:
            if file.endswith('.html'):
                file_path = os.path.join(root, file)
                relative_src_file = os.path.relpath(file_path, docs_dir)

                with open(file_path, 'r', encoding='utf-8', errors='ignore') as f:
                    content = f.read()

                links = href_regex.findall(content)

                for link in links:
                    parsed = urlparse(link)

                    # Ignore external links, mailto, and javascript protocols
                    if parsed.scheme in ('http', 'https', 'mailto', 'javascript', 'data'):
                        continue

                    # Ignore empty paths (e.g., href="#anchor" on the same page)
                    if not parsed.path:
                        continue

                    total_links_checked += 1

                    # Resolve target path relative to the directory of the file containing the link
                    target_rel_path = unquote(parsed.path)
                    target_abs_path = os.path.normpath(os.path.join(root, target_rel_path))

                    # If pointing to a directory, assume it's looking for index.html
                    if os.path.isdir(target_abs_path):
                        target_abs_path = os.path.join(target_abs_path, 'index.html')

                    if not os.path.exists(target_abs_path):
                        broken_count += 1
                        link_log_lines.append(f"[BROKEN] File: {relative_src_file} -> Link: {link}")
                    else:
                        link_log_lines.append(f"[VALID]  File: {relative_src_file} -> Link: {link}")

    # Output a concise summary to console
    if broken_count > 0:
        print(f"❌ FAILED: Found {broken_count} broken internal links out of {total_links_checked} total links checked.")
    else:
        print(f"✅ SUCCESS: All {total_links_checked} internal links checked are completely valid!")

    # Write the complete mapping status (valid + broken) to output file if requested
    if output_file:
        write_log(output_file, link_log_lines)
    print()

if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Sanity check tools for JSON+Pebble Dokka output.")
    subparsers = parser.add_subparsers(dest="command", required=True, help="Sub-commands")

    # Create a parent parser for shared arguments across commands
    parent_parser = argparse.ArgumentParser(add_help=False)
    parent_parser.add_argument("--output-file", "-o", help="Path to a file where results/errors will be logged.", default=None)

    # Command 1: check-list
    parser_list = subparsers.add_parser("check-list", parents=[parent_parser], help="Check if files in a text list exist in the rendered docs.")
    parser_list.add_argument("file_list", help="Path to the text file containing the list of files (e.g. files_json.txt)")
    parser_list.add_argument("pebble_dir", help="Path to the generated JSON+Pebble documentation directory")

    # Command 2: compare-base
    parser_compare = subparsers.add_parser("compare-base", parents=[parent_parser], help="Compare standard HTML output with JSON+Pebble output.")
    parser_compare.add_argument("base_dir", help="Path to the standard Dokka HTML documentation directory")
    parser_compare.add_argument("pebble_dir", help="Path to the generated JSON+Pebble documentation directory")

    # Command 3: check-links
    parser_links = subparsers.add_parser("check-links", parents=[parent_parser], help="Find and map status of internal links in a documentation directory.")
    parser_links.add_argument("docs_dir", help="Path to the documentation directory to scan")

    args = parser.parse_args()

    if args.command == "check-list":
        check_list(args.file_list, args.pebble_dir, args.output_file)
    elif args.command == "compare-base":
        compare_base(args.base_dir, args.pebble_dir, args.output_file)
    elif args.command == "check-links":
        check_links(args.docs_dir, args.output_file)
