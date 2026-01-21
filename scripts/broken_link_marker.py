import os
import argparse
from urllib.parse import urlparse, unquote
from bs4 import BeautifulSoup

def is_external(url):
    """Check if a URL is external (has a scheme or starts with //)."""
    parsed = urlparse(url)
    return bool(parsed.scheme) or url.startswith('//')

def resolve_local_path(current_file_web_path, link_url, web_root_disk):
    """Resolves a link URL to a physical path on disk."""
    if link_url.startswith('/'):
        # Absolute web path from server root
        relative_path = link_url.lstrip('/')
        return os.path.join(web_root_disk, relative_path)
    else:
        # Relative web path from current directory
        current_dir = os.path.dirname(current_file_web_path)
        return os.path.normpath(os.path.join(current_dir, link_url))

def process_html(input_path, output_path, web_root_disk, web_server_path, color, debug=False):
    with open(input_path, 'r', encoding='utf-8', errors='ignore') as f:
        # Explicitly using the 'lxml' parser for speed and consistency
        soup = BeautifulSoup(f, 'lxml')

    CLASS_NAME = "missing-or-external-link"
    
    # Inject CSS class styling into the <head>
    style_tag = soup.new_tag("style")
    style_tag.string = f".{CLASS_NAME} {{ color: {color} !important; }}"
    if soup.head:
        soup.head.append(style_tag)
    else:
        soup.insert(0, style_tag)

    links = soup.find_all('a', href=True)
    marked_count = 0
    unchanged_count = 0
    debug_log = []

    for link in links:
        # Clean URL of fragments and queries for file-system check
        href = link['href'].split('#')[0].split('?')[0]
        if not href:
            unchanged_count += 1
            continue

        should_mark = False
        if is_external(href):
            should_mark = True
            if debug: debug_log.append(f"  [EXT] {href}")
        else:
            decoded_href = unquote(href)
            target_disk_path = resolve_local_path(input_path, decoded_href, web_root_disk)
            
            if not os.path.exists(target_disk_path):
                should_mark = True
                if debug: debug_log.append(f"  [404] {href} -> Checked: {target_disk_path}")
            else:
                if debug: debug_log.append(f"  [OK ] {href}")

        if should_mark:
            classes = link.get('class', [])
            if CLASS_NAME not in classes:
                classes.append(CLASS_NAME)
            link['class'] = classes
            marked_count += 1
        else:
            unchanged_count += 1

    # Save modified file to the output mirror directory
    os.makedirs(os.path.dirname(output_path), exist_ok=True)
    with open(output_path, 'w', encoding='utf-8') as f:
        # BeautifulSoup's str() or encode() will use the lxml-parsed tree
        f.write(str(soup))

    # Total links within THIS file specifically
    file_total_links = marked_count + unchanged_count

    # Output line: Server-perspective path and per-file counts
    print(f"File: {web_server_path} | Marked: {marked_count} | Unchanged: {unchanged_count} | Total Links Processed: {file_total_links}")

    if debug and debug_log:
        for log in debug_log:
            print(log)

def main():
    parser = argparse.ArgumentParser(description="Mark dead/external links using BeautifulSoup with lxml.")
    parser.add_argument("--input-dir", required=True)
    parser.add_argument("--output-dir", required=True)
    parser.add_argument("--missing-link-color", required=True)
    parser.add_argument("--web-root", required=True)
    parser.add_argument("--debug", action="store_true")
    parser.add_argument("--limit-n", type=int)

    args = parser.parse_args()

    input_root = os.path.abspath(args.input_dir)
    web_root_disk = os.path.abspath(args.web_root)
    
    files_processed = 0

    for root, dirs, files in os.walk(input_root):
        for file in files:
            if file.lower().endswith(('.html', '.htm')):
                if args.limit_n is not None and files_processed >= args.limit_n:
                    return

                input_file_path = os.path.join(root, file)
                
                # Full web server path (e.g., /a/android/animation/AnimatorInflater.html)
                rel_to_web_root = os.path.relpath(input_file_path, web_root_disk)
                web_server_path = "/" + rel_to_web_root.replace(os.sep, '/')

                # Path for the mirrored output file
                rel_to_input_dir = os.path.relpath(input_file_path, input_root)
                output_file_path = os.path.join(args.output_dir, rel_to_input_dir)

                process_html(
                    input_file_path, 
                    output_file_path, 
                    web_root_disk, 
                    web_server_path,
                    args.missing_link_color, 
                    args.debug
                )
                
                files_processed += 1

if __name__ == "__main__":
    main()