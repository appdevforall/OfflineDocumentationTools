## Processing individual doc sets

To create links between separate documentation subsets from developer.google.com, process each of them indvidually by changing the menu id and output directory variables at the top of proc_script.sh and running it.

## Combining process doc sets

Then "cp -r" all contents of all output directories into a single directory "combined_docs"

## Adjust links

To let these documentation subsets link to each other, change the html directory in adjust_links.py to "combined_docs" and run the script.