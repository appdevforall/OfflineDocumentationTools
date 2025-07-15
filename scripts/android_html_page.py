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
        Wraps the output of get_article_text() in <html><head><title>Class Documentation</title></head><body>...</body></html>
        """
        body = self.get_article_text()
        if not body:
            return ""
        return f"<html><head><title>Class Documentation</title></head><body>{body}</body></html>" 

    def get_androidx_article_text(self):
        """
        Extract plain text from AndroidX HTML content after <div id="header-block"> up to devsite-hats-survey, stripping HTML tags (no BeautifulSoup).
        Returns:
            str: The plain text content, or empty string if not found
        """
        import re
        if not os.path.exists(self.file_path):
            return ""
        try:
            with open(self.file_path, 'r', encoding='utf-8') as f:
                content = f.read()
                start_marker = '<div id="header-block">'
                end_marker = '<devsite-hats-survey'
                start_index = content.find(start_marker)
                if start_index == -1:
                    return ""
                # Don't skip past the header-block div, include it
                end_index = content.find(end_marker, start_index)
                if end_index == -1:
                    return ""
                content_between = content[start_index:end_index]
                # Remove all HTML tags
                text = re.sub(r'<[^>]+>', ' ', content_between)
                # Collapse whitespace
                text = re.sub(r'\s+', ' ', text).strip()
                return text
        except Exception:
            return ""
    
    def get_androidx_html_page(self):
        """
        Wraps the output of get_androidx_article_text() in <html><head><title>Class Documentation</title></head><body>...</body></html>
        """
        body = self.get_androidx_article_text()
        if not body:
            return ""
        return f"<html><head><title>Class Documentation</title></head><body>{body}</body></html>"

def main():
    import argparse
    parser = argparse.ArgumentParser(description="Extract and print a cleaned Android HTML documentation page.")
    parser.add_argument("file", help="Path to the Android HTML file")
    args = parser.parse_args()

    page = AndroidHtmlPage(args.file)
    print(page.get_html_page())

if __name__ == "__main__":
    main() 