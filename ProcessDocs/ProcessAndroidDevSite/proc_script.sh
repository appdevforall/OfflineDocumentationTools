MENU_ID=111
OUTDIR="archdocs"

# Extract single menu HTML for app architecture section
python3 extract_single_menu.py --menu-cache /home/elissa/ADFA/devsite/dev_scraper/android_dev_scraper/scraper_project/outtest8/menu_cache.pkl --menu-id $MENU_ID --out-file menu_raw.html

# Create URL -> local file and RID -> local file basename maps.
python3 process_url_map.py --url-file-map-in /home/elissa/ADFA/devsite/dev_scraper/android_dev_scraper/scraper_project/outtest8/url_file_map.txt  --docs-path /home/elissa/ADFA/devsite/dev_scraper/android_dev_scraper/scraper_project/outtest8/docs/ --url-file-map-out url_file.pkl --rid-file-map-out rid_file_out.pkl

# Extract links from isolated menu HTML
python3 extract_links.py --input-file menu_raw.html --output-file links.txt

# Output:
# valid.txt -- list of links that we have actually scraped as they appear in the menu
# invalid.txt -- list of links in menu that have not been scraped (external, missed due to bug)
# file_locs.txt -- list of file locations that need to be processed into documentation

python3 process_targets.py --url-file-map url_file.pkl --links-file links.txt --output-files valid.txt,invalid.txt,file_locs.txt

# Debloat docs, place HTML into archdocs/ and media in archdocs/media/
python3 debloat_docs.py --input_list file_locs.txt --output_dir $OUTDIR --menu_file menu${MENU_ID}.html --url_map url_file.txt --media_subdir media --x_size 100x100, --rid_map rid_file_out.pkl --log_file log.txt

# Create menu
python3 debloat_menu.py --input menu_raw.html --output ${OUTDIR}/menu${MENU_ID}.html