#! /usr/bin/python

import os.path as path
import mimetypes
import http
from http.server import BaseHTTPRequestHandler, HTTPServer
import sqlite3
import sys
import time
import threading

DATABASE_FILENAME = "Documentation.db"
HOST_NAME         = "localhost"
SERVER_PORT       = 8080 #6174
QUERY_SQL         = """SELECT
  content,
  CT.value       as contentType,
  CT.compression as compression
FROM
  Content               as C
INNER JOIN Languages    as L  ON C.languageID    = L.ID
INNER JOIN ContentTypes as CT ON C.contentTypeID = CT.ID
WHERE
    C.path          = ?
AND L.value         = ?;"""

DEFAULT_LANGUAGE    = "en-US"
ACCEPT_LANGUAGES    = "Accept-Language" # Client's acceptable languages
ACCEPT_ENCODINGS    = "Accept-Encoding" # Client's acceptable compression types
ACCEPT              = "Accept"          # Client's acceptable content types
BROTLI_TAG          = "br"
BROTLI_COMPRESSION  = "brotli"

# Thread-local storage for database connections
thread_local = threading.local()

def get_db():
    if not hasattr(thread_local, "connection"):
        thread_local.connection = sqlite3.connect(DATABASE_FILENAME)
        thread_local.cursor = thread_local.connection.cursor()
    return thread_local.cursor

# ------------------------------------------------------------------------------
def get_header(headers, key, default_value):
  return [item.strip() for item in headers.get(key, default_value).split(",")]



# ------------------------------------------------------------------------------
def get_client_languages(headers):
  # TODO: Get the language with the highest q factor. --DS, 6-May-2025

  return get_header(headers, ACCEPT_LANGUAGES, DEFAULT_LANGUAGE)



# ------------------------------------------------------------------------------
def format_error(error, url_path, language):
  message = b"<html><head><title>Error %s %s</title></head><body>%s<br>%s<br>%s</body></html>" % \
              (bytes(str(error.value),                                      "utf-8"),
               bytes(error.phrase,                                          "utf-8"),
               bytes(f"""Path: <b>{url_path}</b>""",                        "utf-8"),
               bytes(f"""Language: <b>{language}</b>""",                    "utf-8"),
               bytes(f"""<button onClick='history.back()'>Back</button>""", "utf-8")
              )

  return error, (message, 'text/html', 'None')



# ------------------------------------------------------------------------------
def get_data(cursor, url_path, language):
  cursor.execute(QUERY_SQL, (url_path, language))
  rows = cursor.fetchall()

  print(f"""In get_data(), url_path='{url_path}', language='{language}' and there are {len(rows)} rows.""")

  if len(rows) == 1:
    result = http.HTTPStatus.OK, rows[0]

  elif len(rows) == 0:
    print(f"""DATABASE CONTENT ERROR: there is no match for url_path='{url_path}'.""")
    result = format_error(http.HTTPStatus.NOT_FOUND, url_path, language)

  else: # len(rows) > 1
    print(f"""DATABASE CONTENT ERROR: for url_path='{url_path}', there are {len(rows)}, not 1.""")
    result = format_error(http.HTTPStatus.INTERNAL_SERVER_ERROR, url_path, language)

  return result



# ------------------------------------------------------------------------------
def client_supports_brotli(headers):
  return BROTLI_TAG in get_header(headers, ACCEPT_ENCODINGS, "")



# ------------------------------------------------------------------------------
def uncompress_brotli(content):
  print("TODO: Replace this function with Brotli decompression. --DS, 6-May-2025")

  return b"""Brotli decompression is required but the server doesn't do it yet. Sorry.\n"""



# ------------------------------------------------------------------------------
class MyServer(BaseHTTPRequestHandler):
  def do_GET(self):
    cursor = get_db()
    status, (content, content_type, compression) = \
       get_data(cursor, self.path[1:], get_client_languages(self.headers)[0])

    # TODO: Test that the client will accept the content type we propose. Use ACCEPT for that. --DS, 6-May-2025

    from pprint import pformat; print(f"""Headers: {pformat(self.headers.items())}""")
    if compression == BROTLI_COMPRESSION and client_supports_brotli(self.headers) == False:
      content = uncompress_brotli(content)
      compression = "None"

    self.send_response(status.value)
    self.send_header("Content-type", content_type)
    self.send_header("Content-length", len(content))
    if compression == BROTLI_COMPRESSION:
      self.send_header("Content-encoding", BROTLI_TAG)
    self.end_headers()
    self.wfile.write(content)



# ------------------------------------------------------------------------------
def start_server():
    myServer = HTTPServer((HOST_NAME, SERVER_PORT), MyServer)
    print(f"Server started http://{HOST_NAME}:{SERVER_PORT}")

    try:
        myServer.serve_forever()
    except KeyboardInterrupt:
        pass

    myServer.server_close()
    print("Server stopped.")



# ------------------------------------------------------------------------------
# ------------------------------------------------------------------------------
if __name__ == "__main__":
    start_server()
