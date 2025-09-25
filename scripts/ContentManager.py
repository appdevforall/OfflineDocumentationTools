import argparse
import brotli
import hashlib
import os
import sqlite3
import shutil
import sys
import logging
import re

# Module-level logger
content_manager_logger = logging.getLogger(__name__)


def setup_logging():
    """Configures logging for the ContentManager application."""
    logging.basicConfig(
        level=logging.INFO,
        format='%(asctime)s - %(levelname)s - %(message)s',
        handlers=[
            logging.FileHandler("content_manager.log"),
            logging.StreamHandler(sys.stdout)
        ]
    )


class ContentManager:
    """
    Manages the extraction and updating of content in the Code on the Go documentation database.
    """

    # Map file extensions to (contentTypeID, compression) tuples based on the provided schema.
    # The default is text/plain, which is compressible.
    EXT_TO_CONTENT_TYPE = {
        'css': (1, 'brotli'),
        'svg': (2, 'brotli'),
        'png': (3, 'none'),
        'md': (4, 'brotli'),
        'txt': (5, 'brotli'),
        'ftl': (5, 'brotli'),
        'js': (6, 'brotli'),
        'mjs': (6, 'brotli'),
        'jpeg': (7, 'none'),
        'jpg': (7, 'none'),
        'json': (8, 'brotli'),
        'xml': (10, 'brotli'),
        'gif': (11, 'none'),
        'html': (12, 'brotli'),
        'text': (13, 'none'),
        'pdf': (14, 'brotli'),
        'otf': (15, 'brotli'),
        'woff2': (16, 'none'),
        'woff': (17, 'none'),
        'pfa': (18, 'none'),
        'pfb': (18, 'none'),
        'wasm': (19, 'brotli'),
        'ttf': (20, 'brotli'),
        'ts': (22, 'brotli'),  # Using application/x-typescript for .ts
        'ico': (23, 'none'),
        'icc': (24, 'brotli'),
    }

    # Android cursor limit is 2MB, but let's use a safe threshold of 1MB for splitting.
    CHUNK_SIZE = 1 * 1024 * 1024

    def __init__(self, input_db_path=None, output_db_path=None, input_dir=None, output_dir=None, hashes_file_path=None,
                 name=None, updated_sets=None):
        """
        Initializes the ContentManager.

        Args:
            input_db_path (str, optional): The path to the SQLite documentation database for reading.
            output_db_path (str, optional): The path to the database to be modified.
            input_dir (str, optional): The path to the input directory for 'build' mode.
            output_dir (str, optional): The path to the output directory for 'dump' mode.
            hashes_file_path (str, optional): The path to the hashes file.
            name (str, optional): The name of the person modifying the database.
            updated_sets (list, optional): Comma-separated list of documentation sets to update.
        """
        self.input_db_path = input_db_path
        self.output_db_path = output_db_path
        self.input_dir = input_dir
        self.output_dir = output_dir
        self.hashes_file_path = hashes_file_path
        self.name = name
        self.updated_sets = updated_sets

    def dump_content(self):
        """
        Extracts all files from the Content table and saves them to the output directory,
        while also creating a file of their hashes. Handles multipart files by reassembly.
        """
        if not self.output_dir or not self.hashes_file_path:
            content_manager_logger.error("Output directory and hashes file must be specified for 'dump' mode.")
            return

        conn = None
        hashes_to_write = []
        try:
            conn = sqlite3.connect(self.input_db_path)
            cursor = conn.cursor()

            os.makedirs(self.output_dir, exist_ok=True)

            cursor.execute("""
                SELECT
                    Content.path,
                    Content.content,
                    ContentTypes.compression
                FROM
                    Content
                JOIN
                    ContentTypes ON Content.contentTypeID = ContentTypes.id
            """)

            # Group file parts by their base filename
            grouped_files = {}
            for path, content_blob, compression in cursor.fetchall():
                # Check for multipart filename
                match = re.search(r'^(.*)-(\d+)$', path)
                if match:
                    base_path = match.group(1)
                    part_num = int(match.group(2))
                    if base_path not in grouped_files:
                        grouped_files[base_path] = []
                    grouped_files[base_path].append(
                        {'part': part_num, 'content': content_blob, 'compression': compression})
                else:
                    # Treat single-part files as part 0
                    if path not in grouped_files:
                        grouped_files[path] = []
                    grouped_files[path].append({'part': 0, 'content': content_blob, 'compression': compression})

            for base_path, parts in grouped_files.items():
                parts.sort(key=lambda p: p['part'])

                # Combine content from all parts
                combined_content = b''.join([p['content'] for p in parts])
                decompressed = False
                compression = parts[0]['compression']  # Assume all parts have the same compression type
                content_to_write = combined_content

                if compression == 'brotli':
                    try:
                        content_to_write = brotli.decompress(combined_content)
                        decompressed = True
                    except brotli.error as e:
                        # Log a warning but continue with the original (compressed) content
                        content_manager_logger.warning("Decompression failed for %s: %s. Saving original file.",
                                                       base_path, e)

                # Write the (possibly compressed) content to the file
                full_path = os.path.join(self.output_dir, *base_path.split('/'))
                os.makedirs(os.path.dirname(full_path), exist_ok=True)

                with open(full_path, 'wb') as f:
                    f.write(content_to_write)

                # Compute SHA-256 hash of the content that was written to the file
                file_hash = hashlib.sha256(content_to_write).hexdigest()
                hashes_to_write.append(f"{base_path}\t{file_hash}\n")

                if len(parts) > 1:
                    logging_message = f"Extracted and reassembled multipart file: {base_path}"
                    if decompressed:
                        logging_message += " (decompressed)"
                    logging_message += " from %d parts." % len(parts)
                    content_manager_logger.info(logging_message)
                else:
                    if decompressed:
                        content_manager_logger.info("Extracted and decompressed: %s", base_path)
                    else:
                        content_manager_logger.info("Extracted: %s (saved in original compressed form)", base_path)

            # Write hashes to the file
            with open(self.hashes_file_path, 'w', encoding='utf-8') as f:
                f.writelines(hashes_to_write)
            content_manager_logger.info("Hashes written to: %s", self.hashes_file_path)

        except sqlite3.Error as e:
            content_manager_logger.error("Database error: %s", e)
        finally:
            if conn:
                conn.close()

    def _get_content_type(self, file_path):
        """
        Determines content type and compression based on file extension.
        Returns a tuple of (contentTypeID, compression_type).
        """
        _, ext = os.path.splitext(file_path)
        ext = ext.lstrip('.').lower()

        # Check for multi-part file path, e.g., "file.html-1"
        ext_match = re.search(r'\.(.*)-?\d*$', file_path)
        if ext_match:
            ext = ext_match.group(1).lower()

        # Default to text/plain (ID 5, brotli) if extension is unknown
        return self.EXT_TO_CONTENT_TYPE.get(ext, (5, 'brotli'))

    def build_database(self):
        """
        Updates the Content table in the output database based on changes in the input directory.
        """
        if not self.input_dir or not self.input_db_path or not self.output_db_path or not self.hashes_file_path or not self.name:
            content_manager_logger.error(
                "Input directory, input database, output database, hashes file, and name must be specified for 'build' mode.")
            return

        # 1. Make a copy of the input database to the output location
        content_manager_logger.info("Creating a copy of '%s' at '%s'...", self.input_db_path, self.output_db_path)
        try:
            shutil.copy2(self.input_db_path, self.output_db_path)
        except IOError as e:
            content_manager_logger.error("Error copying database file: %s", e)
            return

        # 2. Read old hashes from the hashes file
        old_hashes = {}
        if os.path.exists(self.hashes_file_path):
            with open(self.hashes_file_path, 'r', encoding='utf-8') as f:
                for line in f:
                    try:
                        path, file_hash = line.strip().split('\t')
                        old_hashes[path] = file_hash
                    except ValueError:
                        content_manager_logger.warning("Skipping malformed line in hashes file: %s", line.strip())

        # 3. Get current files and their hashes
        current_hashes = {}
        for root, _, files in os.walk(self.input_dir):
            for filename in files:
                full_path = os.path.join(root, filename)
                db_path = os.path.relpath(full_path, self.input_dir).replace(os.sep, '/')

                try:
                    with open(full_path, 'rb') as f:
                        file_content = f.read()
                        file_hash = hashlib.sha256(file_content).hexdigest()
                        current_hashes[db_path] = file_hash
                except IOError as e:
                    content_manager_logger.error("Error reading file %s: %s", full_path, e)
                    continue

        # 4. Identify new, modified, and deleted files
        new_files = [path for path in current_hashes if path not in old_hashes]
        modified_files = [path for path, h in current_hashes.items() if path in old_hashes and h != old_hashes[path]]
        deleted_files = [path for path in old_hashes if path not in current_hashes]

        content_manager_logger.info("Identified %d new files, %d modified files, and %d deleted files.", len(new_files),
                                    len(modified_files), len(deleted_files))

        conn = None
        try:
            conn = sqlite3.connect(self.output_db_path)
            cursor = conn.cursor()

            # 5. Process changes
            # Deleted files (single query)
            if deleted_files:
                placeholders = ','.join(['?'] * len(deleted_files))
                query = f"DELETE FROM Content WHERE path IN ({placeholders})"
                cursor.execute(query, deleted_files)
                content_manager_logger.info("Successfully deleted %d files.", len(deleted_files))

            # New and modified files (prepare data for single INSERT/UPDATE)
            inserts_to_execute = []
            updates_to_execute = []

            for path in new_files + modified_files:
                full_path = os.path.join(self.input_dir, *path.split('/'))
                try:
                    with open(full_path, 'rb') as f:
                        file_content = f.read()
                except IOError as e:
                    content_manager_logger.error("Error reading file to process %s: %s", full_path, e)
                    continue

                content_type_id, compression_type = self._get_content_type(full_path)

                content_to_store = file_content
                if compression_type == 'brotli':
                    try:
                        content_to_store = brotli.compress(file_content, quality=11)
                    except brotli.error as e:
                        content_manager_logger.error("Error compressing file %s: %s", path, e)
                        continue

                # Handle oversized files
                if len(content_to_store) > self.CHUNK_SIZE:
                    num_chunks = (len(content_to_store) + self.CHUNK_SIZE - 1) // self.CHUNK_SIZE
                    content_manager_logger.info("File %s is oversized. Splitting into %d chunks.", path, num_chunks)
                    for i in range(num_chunks):
                        chunk = content_to_store[i * self.CHUNK_SIZE: (i + 1) * self.CHUNK_SIZE]
                        chunk_path = f"{path}-{i + 1}" if i > 0 else path
                        if path in new_files:
                            inserts_to_execute.append((chunk_path, 1, chunk, content_type_id))
                        else:
                            updates_to_execute.append((chunk, content_type_id, chunk_path))
                else:
                    # Normal sized files
                    if path in new_files:
                        inserts_to_execute.append((path, 1, content_to_store, content_type_id))
                    else:
                        updates_to_execute.append((content_to_store, content_type_id, path))

            # Execute batch inserts
            if inserts_to_execute:
                placeholders = ','.join(['(?, ?, ?, ?)'] * len(inserts_to_execute))
                flat_data = [item for sublist in inserts_to_execute for item in sublist]
                query = f"INSERT INTO Content (path, languageID, content, contentTypeID) VALUES {placeholders}"
                content_manager_logger.info("Executing INSERT for %d files.", len(inserts_to_execute))
                cursor.execute(query, flat_data)
                content_manager_logger.info("Successfully inserted %d files.", len(inserts_to_execute))

            # Execute batch updates
            for update_data in updates_to_execute:
                query = "UPDATE Content SET content = ?, contentTypeID = ? WHERE path = ?"
                cursor.execute(query, update_data)
                content_manager_logger.info("Successfully updated modified file %s", update_data[2])

            # --- LastChange Table Update Logic ---

            # 2. Insert/Update the mandatory "wholedb" record
            cursor.execute("UPDATE LastChange SET changeTime=CURRENT_TIMESTAMP, who=? where documentationSet='wholedb';", (self.name,))

            # 3. Update all specified documentation subsets
            if self.updated_sets:
                sets_list = self.updated_sets.split(',')
                for doc_set in sets_list:
                    doc_set = doc_set.strip()
                    # The requirement is to UPDATE TABLE LastChange SET now=CURRENT_TIMESTAMP where documentationSet="{name}"
                    # Since the table is dropped/created on every build, we INSERT/REPLACE.
                    # To mimic the intent of UPDATE on an existing database, we can perform an INSERT OR REPLACE.
                    # But since the table is fresh, we'll just insert.

                    cursor.execute(
                        "UPDATE LastChange SET changeTime=CURRENT_TIMESTAMP , who=? where documentationSet=?;", (self.name, doc_set))
                    #cursor.execute("""
                    #    INSERT INTO LastChange (documentationSet, changeTime, who) VALUES (?, CURRENT_TIMESTAMP, ?)
                    #""", (doc_set, self.name))
                    content_manager_logger.info("Updated LastChange for documentation set: %s", doc_set)

            conn.commit()
            content_manager_logger.info("Database update complete.")

        except sqlite3.Error as e:
            if conn:
                conn.rollback()
            content_manager_logger.error("Database error during build: %s", e)
        finally:
            if conn:
                conn.close()

    def dump_one(self, file_path):
        """
        Extracts a single file from the Content table based on its path.
        """
        if not self.output_dir or not self.input_db_path:
            content_manager_logger.error("Output directory and input database must be specified for 'dump_one'.")
            return

        conn = None
        try:
            conn = sqlite3.connect(self.input_db_path)
            cursor = conn.cursor()

            # Find all parts of the specified file
            cursor.execute("""
                SELECT
                    Content.path,
                    Content.content,
                    ContentTypes.compression
                FROM
                    Content
                JOIN
                    ContentTypes ON Content.contentTypeID = ContentTypes.id
                WHERE
                    Content.path = ? OR Content.path LIKE ?
            """, (file_path, f"{file_path}-%"))

            parts = cursor.fetchall()
            if not parts:
                content_manager_logger.warning("File not found in database: %s", file_path)
                return

            # Reassemble and decompress
            parts_dict = {}
            for part_path, part_content, compression in parts:
                match = re.search(r'^(.*)-(\d+)$', part_path)
                if match:
                    parts_dict[int(match.group(2))] = {'content': part_content, 'compression': compression}
                else:
                    parts_dict[0] = {'content': part_content, 'compression': compression}

            combined_content = b''.join([parts_dict[i]['content'] for i in sorted(parts_dict.keys())])
            decompressed = False
            compression = parts_dict[0]['compression'] if 0 in parts_dict else parts_dict[1]['compression']
            content_to_write = combined_content

            if compression == 'brotli':
                try:
                    content_to_write = brotli.decompress(combined_content)
                    decompressed = True
                except brotli.error as e:
                    content_manager_logger.error("Error decompressing %s: %s", file_path, e)
                    return

            full_path = os.path.join(self.output_dir, *file_path.split('/'))
            os.makedirs(os.path.dirname(full_path), exist_ok=True)

            with open(full_path, 'wb') as f:
                f.write(content_to_write)

            if len(parts) > 1:
                logging_message = f"Extracted and reassembled multipart file: {file_path}"
                if decompressed:
                    logging_message += " (decompressed)"
                logging_message += " from %d parts." % len(parts)
                content_manager_logger.info(logging_message)
            else:
                if decompressed:
                    content_manager_logger.info("Successfully extracted and decompressed: %s", file_path)
                else:
                    content_manager_logger.info("Successfully extracted: %s", file_path)

        except sqlite3.Error as e:
            content_manager_logger.error("Database error: %s", e)
        finally:
            if conn:
                conn.close()


def main():
    """
    Parses command-line arguments and runs the ContentManager.
    """
    parser = argparse.ArgumentParser(description="Manage content for the Code on the Go documentation database.")
    parser.add_argument("--operation", required=True, choices=["dump", "build", "dump_one"],
                        help="The operation mode: 'dump' to extract all files, 'build' to add/update them, or 'dump_one' to extract a single file.")

    # Arguments for all modes
    parser.add_argument("--input-database", dest="input_db",
                        help="The path to the SQLite documentation database to read from (dump, dump_one) or copy from (build).")
    parser.add_argument("--hashes-file", dest="hashes_file",
                        help="[dump/build] Path to the hashes file.")

    # Arguments for dump/dump_one modes
    parser.add_argument("--output-dir", dest="output_dir",
                        help="[dump/dump_one] The output directory for extracted files.")

    # Arguments for build mode
    parser.add_argument("--input-directory", dest="input_dir",
                        help="[build] The directory with content used to update the database.")
    parser.add_argument("--output-database", dest="output_db",
                        help="[build] The file name of the updated database.")
    parser.add_argument("--name", dest="name",
                        help="[build] The name of the author of the update.",
                        required=False)
    parser.add_argument("--updated-sets", dest="updated_sets",
                        help="[build] Comma-separated list of documentation subsets that were updated (e.g., 'java,kotlin').",
                        required=False)

    # Arguments for dump_one mode
    parser.add_argument("--single-file-path", dest="single_file_path",
                        help="[dump_one] The path of the single file to extract.")

    args = parser.parse_args()

    # Add conditional logic for the required --name argument
    if args.operation == 'build' and not args.name:
        parser.error("--name is required for 'build' mode.")

    if args.operation == 'dump':
        if not args.input_db or not args.output_dir or not args.hashes_file:
            parser.error("--input-database, --output-dir, and --hashes-file are required for 'dump' mode.")
        manager = ContentManager(input_db_path=args.input_db, output_dir=args.output_dir,
                                 hashes_file_path=args.hashes_file)
        manager.dump_content()
    elif args.operation == 'build':
        if not args.input_db or not args.input_dir or not args.hashes_file or not args.output_db:
            parser.error(
                "--input-database, --input-directory, --hashes-file, and --output-database are all required for 'build' mode.")
        manager = ContentManager(input_db_path=args.input_db, input_dir=args.input_dir,
                                 output_db_path=args.output_db, hashes_file_path=args.hashes_file, name=args.name,
                                 updated_sets=args.updated_sets)
        manager.build_database()
    elif args.operation == 'dump_one':
        if not args.input_db or not args.output_dir or not args.single_file_path:
            parser.error("--input-database, --output-dir, and --file-path are required for 'dump_one' mode.")
        manager = ContentManager(input_db_path=args.input_db, output_dir=args.output_dir)
        manager.dump_one(args.single_file_path)


if __name__ == "__main__":
    setup_logging()
    main()