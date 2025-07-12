from bs4 import BeautifulSoup
import os
import re
import sys

class AndroidHtmlPage:
    """
    A class to represent an Android HTML documentation page.
    
    Args:
        file_path (str): Path to the HTML file
    """
    
    def __init__(self, file_path):
        self.file_path = file_path
    
    def get_article_text(self):
        """
        Extract text from content between the specific HTML comment sentinels.
        
        Returns:
            str: The text content between the sentinels, or empty string if not found
        """
        if not os.path.exists(self.file_path):
            return ""
        
        try:
            with open(self.file_path, 'r', encoding='utf-8') as f:
                content = f.read()
                
                # Find content between the sentinel comments
                start_sentinel = "<!-- ======== START OF CLASS DATA ======== -->"
                end_sentinel = "<!-- ========= END OF CLASS DATA ========= -->"
                
                start_index = content.find(start_sentinel)
                end_index = content.find(end_sentinel)
                
                if start_index == -1 or end_index == -1:
                    return ""
                
                if start_index >= end_index:
                    return ""
                
                # Extract the content between sentinels
                start_content = start_index + len(start_sentinel)
                content_between = content[start_content:end_index]
                
                # Parse the HTML content between sentinels
                soup = BeautifulSoup(content_between, 'html.parser')
                
                # Extract text content, converting HTML to plain text
                return soup.get_text(separator=' ', strip=True)
                    
        except Exception as e:
            # Return empty string on any error
            return ""
    
    def get_html_page(self):
        """
        Return a valid HTML page with proper structure containing the article content.
        
        Returns:
            str: A complete HTML page with the article content between sentinels
        """
        if not os.path.exists(self.file_path):
            return f"""<html>
<head><title>Class Documentation</title></head>
<body>
<p>File not found: {self.file_path}</p>
</body>
</html>"""
        
        try:
            with open(self.file_path, 'r', encoding='utf-8') as f:
                content = f.read()
                
                # Find content between the sentinel comments
                start_sentinel = "<!-- ======== START OF CLASS DATA ======== -->"
                end_sentinel = "<!-- ========= END OF CLASS DATA ========= -->"
                
                start_index = content.find(start_sentinel)
                end_index = content.find(end_sentinel)
                
                if start_index == -1 or end_index == -1:
                    return f"""<html>
<head><title>Class Documentation</title></head>
<body>
<p>No class data found in {self.file_path}</p>
</body>
</html>"""
                
                if start_index >= end_index:
                    return f"""<html>
<head><title>Class Documentation</title></head>
<body>
<p>Invalid sentinel positions in {self.file_path}</p>
</body>
</html>"""
                
                # Extract the content between sentinels
                start_content = start_index + len(start_sentinel)
                content_between = content[start_content:end_index]
                
                # Extract the class name from the h1 tag
                soup = BeautifulSoup(content_between, 'html.parser')
                h1_tag = soup.find('h1')
                class_name = h1_tag.get_text(strip=True) if h1_tag else "Class Documentation"
                
                # Create the complete HTML page
                html_page = f"""<html>
<head><title>{class_name}</title></head>
<body>
{content_between}
</body>
</html>"""
                
                return html_page
                    
        except Exception as e:
            return f"""<html>
<head><title>Class Documentation</title></head>
<body>
<p>Error reading file {self.file_path}: {str(e)}</p>
</body>
</html>""" 

def main():
    import argparse
    parser = argparse.ArgumentParser(description="Extract and print a cleaned Android HTML documentation page.")
    parser.add_argument("file", help="Path to the Android HTML file")
    args = parser.parse_args()

    page = AndroidHtmlPage(args.file)
    print(page.get_html_page())

if __name__ == "__main__":
    main() 