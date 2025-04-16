import pickle
import sys
import urllib.parse
import os

URL_FILE_MAP_PATH = "full_scraper_output/dev_scraper/android_dev_scraper/scraper_project/outtest8/url_file_map.txt"
DOCS_PATH =  "full_scraper_output/dev_scraper/android_dev_scraper/scraper_project/outtest8/docs/"

"""
https://developer.android.com/health-and-fitness	5508627005843485
https://developer.android.com/social-and-messaging	5508627191585972
https://developer.android.com/productivity	5508627349969719
"""

def local_path_from_url(url):
    path = url.replace("https://developer.android.com/", "")
    return "/".join(path.split("/")[:-1])

def main():
    if len(sys.argv) != 3:
        print("Usage: python3 process_url_map.py url_file_map.pkl rid_file_map.pkl")
        return

    url_file_map = {}
    rid_file_map = {}

    map_lines = open(URL_FILE_MAP_PATH, "r").readlines()
    for line in map_lines:
        url, rid = line.strip().split("\t")
        local_rid_path = local_path_from_url(url)
        local_resource = os.path.join(DOCS_PATH, local_rid_path, rid)

        url_file_map[url] = local_resource
        rid_file_map[rid] = url.replace("https://developer.android.com/", "").replace("/", "_")

    pickle.dump(url_file_map, open(sys.argv[1], "wb"))
    pickle.dump(rid_file_map, open(sys.argv[2], "wb"))

if __name__ == "__main__":
    main()