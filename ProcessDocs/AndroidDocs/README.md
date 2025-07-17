Scraping the official Android documentation is both very time consuming, and generates immense amounts of data. More than can be reasonably stored in a Git repo, even using Git LFS. Instead, the scrape is stored in a shared Google Drive: https://drive.google.com/drive/folders/1BTPYXSOptrZ8EmvroDRJ4dUykqTvudl0

The file within that directory is called "android_developer_site_final_scraped_raw.zip" and is here: https://drive.google.com/file/d/12n6kJ0F-7uda-fFMHko-cMZTD5_xsIMY/view?usp=sharing

That file is 9GB.

In the scripts/ directory, there are two relevant files: android_html_page.py and android_tooltips.py. They were run to product the derived files under ProcesssDocs/AndroidDocs for both "android" and "androidx".

In both cases, the starting point was the classes.html file which contains a table of all the functions and classes.