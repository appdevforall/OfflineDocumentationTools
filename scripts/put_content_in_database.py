#! /usr/bin/python

import os
import os.path as path
import mimetypes
import sqlite3
import subprocess
import sys


LIST_OF_FILES           = "/tmp/files"
LIST_OF_DIRECTORIES     = "/tmp/directories"
DATABASE_FILENAME       = "/home/david/Documentation.db"
CLONE_INTO_DIR          = "../cloneContentDir"
COMPRESSION_PARALLELISM = os.cpu_count() - 1 if os.cpu_count() else 1

SKIP_EXTS = {"",
             ".version",
            }

OVERRIDE_EXTS = {"tar" : "gzip",
                }

OVERRIDE_MIMETYPES = {"image/svg+xml" : "brotli",
                     }

mimetypes.types_map[".md"]           = "text/plain"
mimetypes.types_map[".log"]          = "text/plain"
mimetypes.types_map[".dtd"]          = "text/plain"
mimetypes.types_map[".Debian"]       = "text/plain"
mimetypes.types_map[".alternatives"] = "text/plain"

INSERT_SQL = """INSERT INTO Content (path, languageID, content, contentTypeID) VALUES (?, 1, '', ?);"""
UPDATE_SQL = """UPDATE Content SET content=? WHERE path=? AND languageID=1;"""


# ------------------------------------------------------------------------------
def clone_directories(clone_into_dir):
  with open(LIST_OF_DIRECTORIES) as fd:
    dirnames = [i.strip()[2:] for i in fd][1:] # Skip the first, top directory.

  os.mkdir(clone_into_dir)
  [os.mkdir(path.join(clone_into_dir, d)) for d in dirnames]



# ------------------------------------------------------------------------------
def get_tables(cursor):
  rows = cursor.execute("""SELECT value, id, compression FROM ContentTypes ORDER BY id;""").fetchall()

  content_type_table = {row[0] : int(row[1]) for row in rows}
  compression_table  = {row[0] :     row[2]  for row in rows}

  return content_type_table, compression_table



# ------------------------------------------------------------------------------
def get_compression_command(input_filename, output_filename, compressor):
    if compressor == "brotli":
      compression_command = f"""brotli --best -o '{output_filename}' '{input_filename}'"""

    elif compressor == "None":
      compression_command = f"""cp '{input_filename}' '{output_filename}'"""

    else:
      print(f"""Bad compressor, '{compressor}' for file '{input_filename}'.\n\n""")
      sys.exit(1)

    return compression_command



# ------------------------------------------------------------------------------
def compress_noexts(compression_table, clone_into_dir, pathnames):
  noexts = [pathname for pathname in pathnames if len(path.splitext(pathname)[-1]) == 0]

  compressor = compression_table["text/plain"]

  return [get_compression_command(pathname, path.join(clone_into_dir, pathname), compressor)
          for pathname in noexts]



# ------------------------------------------------------------------------------
def compress_exts(compression_table, clone_into_dir, pathnames):
  compression_commands = [ ]

  for input_pathname in pathnames:
    if path.splitext(input_pathname)[-1] in SKIP_EXTS:
      continue

    mimetype = mimetypes.types_map[path.splitext(input_pathname)[-1]]
    output_pathname = path.join(clone_into_dir, input_pathname)

    compression_commands.append(get_compression_command(input_pathname, output_pathname, compression_table[mimetype]))

  return compression_commands



# ------------------------------------------------------------------------------
def compress(commands):
  number_of_commands = len(commands)
  jobs = []

  interval = 1 + int(1.0 * len(commands) / COMPRESSION_PARALLELISM)

  for i in range(COMPRESSION_PARALLELISM):
    items = commands[0:interval]
    commands = commands[interval:]

    jobs.append(subprocess.Popen(args=";".join(items), shell=True))

  print(f"There are {number_of_commands} compression commands and {len(jobs)} parallel jobs. This will take a few minutes.")

  [job.wait() for job in jobs]



# ------------------------------------------------------------------------------
def insert_noexts(content_type_table, clone_into_dir, pathnames):
  noexts = [pathname for pathname in pathnames if len(path.splitext(pathname)[-1]) == 0]

  content_type_id = content_type_table["text/plain"]

  return [(pathname, content_type_id, path.join(clone_into_dir, pathname))
          for pathname in noexts]



# ------------------------------------------------------------------------------
def insert_exts(content_type_table, clone_into_dir, pathnames):
  inserts = []

  for input_pathname in pathnames:
    if path.splitext(input_pathname)[-1] in SKIP_EXTS:
      continue

    mimetype = mimetypes.types_map[path.splitext(input_pathname)[-1]]
    output_pathname = path.join(clone_into_dir, input_pathname)

    inserts.append((input_pathname, content_type_table[mimetype], output_pathname))

  return inserts



# ------------------------------------------------------------------------------
def write_to_database(cursor, sql):
  for pathname, content_type_id, compressed_path in sql:
    cursor.execute(INSERT_SQL, (pathname, content_type_id))

    with open(compressed_path, "rb") as fd:
      data = fd.read()

    cursor.execute(UPDATE_SQL, (sqlite3.Binary(data), pathname))

  cursor.execute(INSERT_SQL, ('x.html', 3))
  cursor.execute(UPDATE_SQL, (sqlite3.Binary(b'Hello, Jim'), 'x.html'))



# ------------------------------------------------------------------------------
def main(cursor):
  with open(LIST_OF_FILES) as fd:
    pathnames = [i.strip()[2:] for i in fd]

  clone_directories(CLONE_INTO_DIR)
  print(f"Directories cloned into '{CLONE_INTO_DIR}'.")

  content_type_table, compression_table = get_tables(cursor)

  compression_commands = []
  compression_commands.extend(compress_exts(  compression_table, CLONE_INTO_DIR, pathnames))
  compression_commands.extend(compress_noexts(compression_table, CLONE_INTO_DIR, pathnames))

  compress(compression_commands)

  sql = []
  sql.extend(insert_exts(  content_type_table, CLONE_INTO_DIR, pathnames))
  sql.extend(insert_noexts(content_type_table, CLONE_INTO_DIR, pathnames))

  cursor.execute("PRAGMA foreign_keys = ON;") # Enable referential integrity enforcement;
  cursor.execute("DELETE FROM Content;")      # Delete old content.

  print("Starting database inserts.")
  write_to_database(cursor, sql)



# ------------------------------------------------------------------------------
# ------------------------------------------------------------------------------
if __name__ == "__main__":
  with sqlite3.connect(DATABASE_FILENAME) as connection:
    main(connection.cursor())
