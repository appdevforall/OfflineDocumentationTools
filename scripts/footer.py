import os
import glob
from bs4 import BeautifulSoup, Tag

# --- Configuration ---

# Define the HTML footer to be appended.
# NOTE: Using BeautifulSoup's Tag functionality to create this will ensure
# it is correctly formatted and handled upon insertion.
FOOTER_HTML = """
<footer role="contentinfo">
<hr>
<p class="legal-copy"><small>Java is a trademark or registered trademark of Oracle and/or its affiliates in the US and other countries.<br> Copyright &copy; 1993, 2024 </small></p>
</footer>
"""

def append_footer_to_javadoc(directory=".", file_pattern="**/*.html"):
    """
    Recursively finds all specified HTML files in a directory and appends
    a standard footer immediately after the </main> tag.

    Args:
        directory (str): The starting directory for the recursive search.
        file_pattern (str): The pattern to match HTML files.
    """
    print(f"Starting search for HTML files in directory: {os.path.abspath(directory)}")
    
    # Use glob.glob with recursive=True to find all matching files in subdirectories
    # We use os.path.join to ensure the path is correctly constructed for the OS.
    file_count = 0
    
    # glob.iglob returns an iterator, which is memory efficient for large directories
    for filepath in glob.iglob(os.path.join(directory, file_pattern), recursive=True):
        
        # Skip directories if the glob pattern happened to match one
        if os.path.isdir(filepath):
            continue

        file_count += 1
        print(f"Processing: {filepath}")

        try:
            # 1. Read the file content
            with open(filepath, 'r', encoding='utf-8') as f:
                content = f.read()

            # 2. Parse the HTML content
            soup = BeautifulSoup(content, 'html.parser')

            # 3. Find the target tag (</main>)
            main_tag = soup.find('main')
            
            if main_tag:
                # 4. Create the new footer content as a BeautifulSoup object
                # This ensures the insertion process is safe and the tags are properly nested.
                footer_soup = BeautifulSoup(FOOTER_HTML, 'html.parser')
                footer_tag = footer_soup.find('footer')
                
                if footer_tag:
                    # 5. Insert the footer immediately after the </main> tag
                    main_tag.insert_after(footer_tag)
                    
                    # 6. Save the modified content back to the file
                    # We use soup.prettify() to ensure clean, readable HTML output.
                    with open(filepath, 'w', encoding='utf-8') as f:
                        f.write(str(soup))
                    print(f"  -> SUCCESS: Footer appended after </main>.")
                else:
                    print(f"  -> WARNING: Could not parse footer HTML.")

            else:
                print(f"  -> SKIPPED: </main> tag not found in file.")

        except Exception as e:
            print(f"  -> ERROR processing {filepath}: {e}")

    print("-" * 40)
    print(f"Finished processing {file_count} files.")
    print("Documentation files updated successfully.")


# Execute the main function
if __name__ == "__main__":
    # If your documentation is in a specific folder (e.g., 'docs'), change the 
    # directory argument: append_footer_to_javadoc(directory="./docs")
    append_footer_to_javadoc(directory="testout")
