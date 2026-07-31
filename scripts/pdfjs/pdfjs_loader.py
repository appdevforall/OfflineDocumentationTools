#! /usr/bin/python
import csv
import json
import os
import sqlite3
import sys


DEBUG                   = "DEBUG" in sys.argv
if DEBUG: print("DEBUG enabled")
if DEBUG: from pprint import pprint, pformat

TOP_EXCLUDE_DIRS        = set(("docs", "examples", "extensions", "test"))
NODE_MODULES_DIR        = "node_modules"
NODE_FLUENT_DIR         = os.path.join(NODE_MODULES_DIR, "@fluent")
NODE_CI_DIR             = os.path.join(NODE_MODULES_DIR, "cached-iterable")
BCMAPS_EXCLUDE          = "external/bcmaps"
ICCS_EXCLUDE            = "external/iccs"

DATABASE_DIRECTORY      = "/home/david"
DATABASE_PATHNAME       = os.path.join(DATABASE_DIRECTORY, "documentation.db")
if DEBUG: print(f"DATABASE path info: '{DATABASE_DIRECTORY}', '{DATABASE_PATHNAME}'.")
CONTENT_TABLE_NAME      = "Content"
CONTENT_TYPE_TABLE_NAME = "ContentTypes"
PDFJS_PREFIX            = "p"
GUNZIP                  = "gunzip"
BROTLI_COMPRESSION      = "brotli"
BROTLI_PROGRAM          = "brotli"
BROTLI_FLAGS            = "-Z"
LANGUAGE_ID             = 1 # We only support en-US for now; row 1 in the Languages database table.
TEST_HTML_DATABASE_PATH = "x.html"
TEST_HTML_CONTENT       = b"Hello, App Dev for All"
#TEST_PDF_LOCAL_PATH     = "test/pdfs/freeculture.pdf"
TEST_PDF_LOCAL_PATH     = "examples/learning/helloworld.pdf"
TEST_PDF_DATABASE_PATH  = "x.pdf"
FILETYPE_MAP = {
  ".html"	: "text/html",
  ".ftl"	: "text/html",
  ".css"	: "text/css",
  ".js"		: "text/javascript",
  ".ts"		: "application/x-typescript",
  ".mjs"	: "text/javascript",
  ".mts"	: "text/javascript",
  ".pdf"	: "application/pdf",
  ".png"	: "image/png",
  ".ico"	: "image/png",
  ".jpg"	: "image/jpeg",
  ".jpeg"	: "image/jpeg",
  ".gif"	: "image/gif",
  ".svg"	: "image/svg+xml",
  ".json"	: "application/json",
  ".woff"	: "font/woff",
  ".woff2"	: "font/woff2",
  ".ttf"	: "font/ttf",
  ".otf"	: "font/otf",
  ".pfb"	: "application/x-font-type1",
  ".wasm"	: "application/wasm",
  ".md"		: "text/text",
  ".icc"	: "application/vnd-iccprofile",
  ".bcmap"	: "application/octet-stream",
  ""		: "text/text",
}


# ------------------------------------------------------------------------------
def getCompressionByMimeType(cursor):
  cursor.execute(f"SELECT id, value, compression FROM {CONTENT_TYPE_TABLE_NAME};")

  return {row[1] : (row[2], row[0]) for row in cursor.fetchall()}


# ------------------------------------------------------------------------------
def processDirectory(dirpath, _, filenames):
  if DEBUG: print(f"In processDirectory(), dirpath='{dirpath}', filenames={filenames}.")

  result = [ ]

  dirpath = dirpath[2:] # Skip the "./" in the front.

  if dirpath == "": # No top-level files go into the database.
    return result

  # Skip certain top-level directories.
  for excludeDir in TOP_EXCLUDE_DIRS:
    if dirpath.startswith(excludeDir):
      return result

  if dirpath.startswith(NODE_MODULES_DIR) and \
     not dirpath.startswith(NODE_FLUENT_DIR) and \
     not dirpath.startswith(NODE_CI_DIR):
    #print(f"XXX: '{dirpath}'")
    return result

  # Skip any path containing a dot directory anywhere.
  for item in dirpath.split(os.sep): # There's probably a better way to do this.
    if item.startswith("."):
      return result

  # We only support en-US for now.
  if dirpath.startswith("l10n") and not dirpath.endswith("en-US"):
    return result

  # These are for use cases that don't apply to our documents, I think. --DS, 19-Jul-2025
  if BCMAPS_EXCLUDE in dirpath or ICCS_EXCLUDE in dirpath:
    return result

  for filename in filenames:
    if DEBUG: print(f"In filenames loop, filename='{filename}'.")

    # Skip dot files.
    if filename.startswith("."):
      continue

    if filename.endswith(".md"):
      continue

    result.append(os.path.join(dirpath, filename))

  return result


# ------------------------------------------------------------------------------
def addBlobToDatabase(cursor, path, languageId, contentTypeId, blobData):
  # To put a binary blob in the database, add the row with empty content,
  # then update it with the real content.

  if DEBUG: print(f"In addBlobToDatabase, cursor={cursor}, path='{path}', languageId={languageId}, contentTypeId={contentTypeId}, len(blobData)={len(blobData)}.")
  else:
    print(f"Adding {path}.")

  cursor.execute(f"""
INSERT INTO {CONTENT_TABLE_NAME}
  (path, languageId, content, contentTypeId)
VALUES (?, ?, ?, ?)""",
             (path, languageId, "", contentTypeId));

  cursor.execute(f"""
UPDATE {CONTENT_TABLE_NAME}
SET content=?
WHERE PATH=?
  AND languageId={languageId}""",
                 (sqlite3.Binary(blobData), path))


# ------------------------------------------------------------------------------
def compressWithBrotli(pathname):
  command = f"{BROTLI_PROGRAM} {BROTLI_FLAGS} {pathname}"

  if DEBUG: print(f"Compressing with '{command}'.")

  os.system(command)

  brPathname = f"{pathname}.br"

  with open(brPathname, "rb") as fd:
    data = fd.read()
  os.remove(brPathname)

  return data


# ------------------------------------------------------------------------------
def addTestContentToDatabase(cursor, compressionByMimeType):
  addBlobToDatabase(cursor,
                    TEST_HTML_DATABASE_PATH,
                    LANGUAGE_ID,
                    compressionByMimeType["text"][1],
                    TEST_HTML_CONTENT)

  addBlobToDatabase(cursor,
                    TEST_PDF_DATABASE_PATH,
                    LANGUAGE_ID,
                    compressionByMimeType["application/pdf"][1],
                    compressWithBrotli(TEST_PDF_LOCAL_PATH))
  

# ------------------------------------------------------------------------------
def uncompressFile(pathname):
  if DEBUG: print(f"In uncompressFile, pathname='{pathname}'.")

  os.system(f"{GUNZIP} {pathname}")

  return os.path.splitext(pathname)[0]


# ------------------------------------------------------------------------------
def addFileToDatabase(cursor, compressionByMimeType, pathname):
  if DEBUG: print(f"Adding '{pathname}' to database.")

  # Gunzip the four gzipped TrueType font files. We'll use Brotli.
  if pathname.endswith(".gz"):
    pathname = uncompressFile(pathname)

  extension = os.path.splitext(pathname)[-1]
  if DEBUG: print(f"Extension for pathname '{pathname}' is '{extension}'.")

  mimeType = FILETYPE_MAP[extension]
  if DEBUG: print(f"Mime type for extension '{extension}' is '{mimeType}'.")

  compression, contentTypeId = compressionByMimeType[mimeType]
  if DEBUG: print(f"Compression and contentTypeId for Mime type '{mimeType}' are '{compression}' and {contentTypeId}.")

  if compression == BROTLI_COMPRESSION:
    data = compressWithBrotli(pathname)
  
  else:
    with open(pathname, "rb") as fd:
      data = fd.read()

  # It's a little funky, using os.path.join() for a database thing.
  databasePath = os.path.join(PDFJS_PREFIX, pathname)

  addBlobToDatabase(cursor, databasePath, LANGUAGE_ID, contentTypeId, data)


# ------------------------------------------------------------------------------
def updateDatabase(pathname):
  if DEBUG: print(f"In updateDatabase(), pathname='{pathname}'.")

  filesToAdd = [ ]
  for dirpath, _, filenames in os.walk(".", topdown=True):
    filesToAdd.extend(processDirectory(dirpath, _, filenames))

  if DEBUG: print(f"Files to add: {filesToAdd}.")

  connection = sqlite3.connect(database=DATABASE_PATHNAME)

  try:
    with connection:
      cursor = connection.cursor()

      compressionByMimeType = getCompressionByMimeType(cursor)
      if DEBUG: pprint(compressionByMimeType)

      addTestContentToDatabase(cursor, compressionByMimeType)

      [addFileToDatabase(cursor, compressionByMimeType, filename) for filename in filesToAdd]

  finally:
    connection.close()


# ------------------------------------------------------------------------------
def runProgram():
  if DEBUG: print("In runProgram().")
  updateDatabase(".")
  

# ------------------------------------------------------------------------------
# ------------------------------------------------------------------------------
if __name__ == "__main__":
  try:
    sys.exit(runProgram())

  except KeyboardInterrupt:
    print("Received keyboard interrupt. Exiting.")
    sys.exit(0)
