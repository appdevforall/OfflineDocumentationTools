#!/bin/bash

# Note, please do not do this more than once per year. 

wget -r \
     --level=inf \
     -e robots=off \
     --verbose \
     --random-wait  \
     --no-clobber \
     --page-requisites \
     --html-extension \
     --convert-links \
     --restrict-file-names=windows \
     --domains runestone.academy \
     --no-parent \
     --user-agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36" \
     --referer="https://runestone.academy/" \
     https://runestone.academy/ns/books/published/javajavajava/root-1-2-3.html?mode=browsing
