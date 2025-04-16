# Rewritten to use command line args by o4-mini

import argparse
import pickle
import os
import sys

def local_path_from_url(url):
    # Strip the base and drop the last segment (the resource ID)
    path = url.replace("https://developer.android.com/", "")
    return "/".join(path.split("/")[:-1])

def main():
    parser = argparse.ArgumentParser(
        description="Process a URL-to-file mapping and produce two pickled maps: URL->file and RID->file-key"
    )
    parser.add_argument(
        "--url-file-map-in",
        required=True,
        help="Path to the input URL-to-file map (tab-delimited: URL<TAB>RID per line)"
    )
    parser.add_argument(
        "--docs-path",
        required=True,
        help="Root path to the documentation files being processed"
    )
    parser.add_argument(
        "--url-file-map-out",
        required=True,
        help="Output path for the pickled URL->file map"
    )
    parser.add_argument(
        "--rid-file-map-out",
        required=True,
        help="Output path for the pickled RID->file-key map"
    )

    args = parser.parse_args()

    url_file_map = {}
    rid_file_map = {}

    # Read the input mapping
    with open(args.url_file_map_in, "r") as fin:
        for line in fin:
            line = line.strip()
            if not line:
                continue
            url, rid = line.split("\t")
            # Build the local file path for this resource
            local_rid_path = local_path_from_url(url)
            local_resource = os.path.join(args.docs_path, local_rid_path, rid)

            url_file_map[url] = local_resource
            # Create a filesystem‐friendly key for this RID
            rid_key = url.replace("https://developer.android.com/", "").replace("/", "_")
            rid_file_map[rid] = rid_key

    # Serialize the mappings
    with open(args.url_file_map_out, "wb") as fout_url:
        pickle.dump(url_file_map, fout_url)

    with open(args.rid_file_map_out, "wb") as fout_rid:
        pickle.dump(rid_file_map, fout_rid)

if __name__ == "__main__":
    main()