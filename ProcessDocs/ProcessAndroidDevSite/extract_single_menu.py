import argparse
import pickle

MENU_CACHE_FILE = "full_scraper_output/dev_scraper/android_dev_scraper/scraper_project/outtest8/menu_cache.pkl"
MENU_TO_CHECK = 7 # id of layout menu

def main():
    parser = argparse.ArgumentParser(
        prog='single_menu_extract.py',
        description='Extract the HTML from a single Android developer site documentation section as a single HTML file.')

    parser.add_argument('--menu-cache', help='Disk location of serialized menu-to-unique-ID map.',
                        required=True)
    parser.add_argument('--menu-id', help='ID of single menu to extract.',
                        required=True)
    parser.add_argument('--out-file', help='Output file name.',
                        required=True)

    args = parser.parse_args()


    menu_cache = pickle.load(open(args.menu_cache, "rb"))
    menu_cache_rev = {v: k for k, v in menu_cache.items()}

    menu = menu_cache_rev[int(args.menu_id)]
    open(args.out_file, "w").write(menu)

if __name__ == "__main__":
    main()