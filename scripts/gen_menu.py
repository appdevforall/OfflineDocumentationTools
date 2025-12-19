import os
import argparse
import html


def generate_menu():
    parser = argparse.ArgumentParser(description="Generate a hierarchical HTML menu from a directory structure.")
    parser.add_argument("directory", help="The root directory to process")
    parser.add_argument("--url-prefix", help="Optional prefix for the generated links")
    parser.add_argument("--title", required=True, help="Title to display at the top of the page")
    parser.add_argument("--output-file", required=True, help="Path to the output HTML file")
    args = parser.parse_args()

    # Normalize the root path
    root_dir = os.path.normpath(args.directory)
    root_name = os.path.basename(root_dir)
    # Reference point for the dot-notation labels (includes the root folder name)
    base_parent = os.path.dirname(root_dir)

    with open(args.output_file, 'w', encoding='utf-8') as f:
        f.write("<!DOCTYPE html>\n")
        f.write("<html>\n")
        f.write("<head>\n")
        f.write(f"    <title>{html.escape(args.title)}</title>\n")
        f.write("    <style>\n")
        f.write("        body { font-family: sans-serif; line-height: 1.5; padding: 20px; }\n")
        f.write("        ul { list-style-type: none; margin: 0; padding-left: 20px; border-left: 1px solid #eee; }\n")
        f.write("        .folder-name { font-weight: bold; margin-top: 8px; color: #444; }\n")
        f.write("        a { text-decoration: none; color: #0066cc; font-size: 0.95em; }\n")
        f.write("        a:hover { text-decoration: underline; }\n")
        f.write("        li { margin: 2px 0; }\n")
        f.write("    </style>\n")
        f.write("</head>\n")
        f.write("<body>\n")
        f.write(f"    <h1>{html.escape(args.title)}</h1>\n")

        def walk_dir(current_path, depth, out_file):
            # dot_prefix is for the labels (e.g., android.accounts)
            rel_path_for_label = os.path.relpath(current_path, base_parent)
            dot_prefix = rel_path_for_label.replace(os.sep, ".")

            try:
                items = sorted(os.listdir(current_path))
            except PermissionError:
                return

            out_file.write(f"{'  ' * depth}<ul>\n")

            for item in items:
                full_path = os.path.join(current_path, item)

                if os.path.isdir(full_path):
                    # Folder label includes full parent hierarchy in dot-notation
                    folder_rel_path = os.path.relpath(full_path, base_parent)
                    folder_dot_path = folder_rel_path.replace(os.sep, ".")

                    out_file.write(f"{'  ' * (depth + 1)}<li>\n")
                    out_file.write(
                        f"{'  ' * (depth + 2)}<div class='folder-name'>{html.escape(folder_dot_path)}</div>\n")
                    walk_dir(full_path, depth + 2, out_file)
                    out_file.write(f"{'  ' * (depth + 1)}</li>\n")

                elif item.endswith(".html"):
                    name_no_ext = os.path.splitext(item)[0]
                    display_name = f"{dot_prefix}.{name_no_ext}"

                    # LINK LOGIC: Calculate relative to root_dir to exclude the root folder name
                    # e.g., if full_path is android/accounts/Account.html and root_dir is android,
                    # web_rel_path becomes accounts/Account.html
                    web_rel_path = os.path.relpath(full_path, root_dir).replace(os.sep, '/')

                    if args.url_prefix:
                        link_path = f"{args.url_prefix.rstrip('/')}/{web_rel_path}"
                    else:
                        link_path = web_rel_path

                    out_file.write(f"{'  ' * (depth + 1)}<li>\n")
                    out_file.write(f"{'  ' * (depth + 2)}<a href='{link_path}'>{html.escape(display_name)}</a>\n")
                    out_file.write(f"{'  ' * (depth + 1)}</li>\n")

            out_file.write(f"{'  ' * depth}</ul>\n")

        # Start recursion
        f.write(f"    <div class='folder-name'>{html.escape(root_name)}</div>\n")
        walk_dir(root_dir, 2, f)

        f.write("</body>\n")
        f.write("</html>\n")


if __name__ == "__main__":
    generate_menu()