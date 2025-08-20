import os
import sqlite3
import shutil

import brotli


def get_extension(file):
    if "." not in file:
        return "NA"
    return file.split(".")[-1]



def main():
    DATABASE_NAME = "documentationaug.db"
    shutil.copy(DATABASE_NAME, "tmp" + DATABASE_NAME)
    DATABASE_NAME = "tmp" + DATABASE_NAME
    kotlin_stdlib_path = "SourceDocs/KotlinStdLibDocs"
    path_prefix = "ks/"

    all_files = []

    # os.walk() generates the file names in a directory tree by walking the
    # tree either top-down or bottom-up.
    # It yields a 3-tuple (dirpath, dirnames, filenames) for each directory.
    for dirpath, dirnames, filenames in os.walk(kotlin_stdlib_path):
        # We iterate through the filenames found in the current directory.
        for filename in filenames:
            # We join the directory path and the filename to get the full path.
            full_path = os.path.join(dirpath, filename)
            all_files.append(full_path)

    file_content_types = {
        "json": "application/json",
        "html": "text/html",
        "svg": "image/svg+xml",
        "woff2": "font/woff2",
        "NA": "text",
        "css": "text/css",
        "js": "application/javascript",
        "woff": "font/woff"
    }

    compression_types = {
        "json": "brotli",
        "html": "brotli",
        "svg": "none",
        "woff2": "none",
        "NA": "brotli",
        "css": "brotli",
        "js": "brotli",
        "woff": "none"
    }

    conn = sqlite3.connect(DATABASE_NAME)
    cursor = conn.cursor()

    language_id = "en-US"

    for file in all_files[:10]:
        path = file.replace(kotlin_stdlib_path, path_prefix)
        file_extension = get_extension(file)

        content = open(file, "rb").read()

        if compression_types[file_extension] == "brotli":
            compressed_content = brotli.compress(content)
        else:
            compressed_content = content

        content_type_id = file_content_types[file_extension]

        cursor.execute(
        "INSERT INTO Content (path, languageID, content, contentTypeID) VALUES (?, ?, ?, ?)",
        (path, language_id, compressed_content, content_type_id))

        conn.commit()
        print(file)
    conn.close()


    """
    conn = sqlite3.connect(DATABASE_NAME)
    cursor = conn.cursor()

    for file in all_files: file.replace(kotlin_stdlib_path, path_prefix). '')
        # cursor.execute(command)
        conn.commit()
        

    """


if __name__ == "__main__":
    main()