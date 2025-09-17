from bs4 import BeautifulSoup
import os


class AndroidHtmlPage:
    """
    A class to represent an Android HTML documentation page.

    Args:
        file_path (str): Path to the HTML file
    """

    def __init__(self, file_path):
        self.file_path = file_path

    def get_article_html(self):
        """
        Extracts the relevant article HTML content from the page.

        Returns:
            str: The HTML content of the article, or empty string if not found
        """
        if not os.path.exists(self.file_path):
            return ""

        try:
            with open(self.file_path, 'r', encoding='utf-8') as f:
                soup = BeautifulSoup(f, 'html.parser')

                # Check for the AndroidX structure first
                header_block = soup.find(id='header-block')
                hats_survey = soup.find('devsite-hats-survey')

                if header_block and hats_survey:
                    # Found AndroidX structure, extract content between them
                    content = ""
                    current_node = header_block
                    while current_node and current_node != hats_survey:
                        content += str(current_node)
                        current_node = current_node.next_sibling
                    return content
                else:
                    # Fallback to the classic Android structure
                    start_comment = soup.find(string=lambda text: "START OF CLASS DATA" in text)
                    end_comment = soup.find(string=lambda text: "END OF CLASS DATA" in text)

                    if start_comment and end_comment:
                        content = ""
                        current_node = start_comment.next_sibling
                        while current_node and current_node != end_comment:
                            content += str(current_node)
                            current_node = current_node.next_sibling
                        return content

        except Exception:
            # Return empty string on any error
            return ""

    def get_html_page(self):
        """
        Wraps the extracted article HTML in <html><head><title>Class Documentation</title></head><body>...</body></html>
        """
        body = self.get_article_html()
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