import argparse
import brotli
import hashlib
import os
import sqlite3
import shutil
import sys
import logging

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

    def __init__(self, input_db_path=None, output_db_path=None, input_dir=None, output_dir=None, hashes_file_path=None,
                 name=None):
        """
        Initializes the ContentManager.

        Args:
            input_db_path (str, optional): The path to the SQLite documentation database for reading.
            output_db_path (str, optional): The path to the database to be modified.
            input_dir (str, optional): The path to the input directory for 'build' mode.
            output_dir (str, optional): The path to the output directory for 'dump' mode.
            hashes_file_path (str, optional): The path to the hashes file.
            name (str, optional): The name of the person modifying the database.
        """
        self.input_db_path = input_db_path
        self.output_db_path = output_db_path
        self.input_dir = input_dir
        self.output_dir = output_dir
        self.hashes_file_path = hashes_file_path
        self.name = name

        # A simple mapping to determine compression
        self.compressible_extensions = ['.html', '.css', '.js', '.json']

    def dump_content(self):
        """
        Extracts all files from the Content table and saves them to the output directory,
        while also creating a file of their hashes.
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

            for path, content_blob, compression in cursor.fetchall():
                full_path = os.path.join(self.output_dir, *path.split('/'))
                os.makedirs(os.path.dirname(full_path), exist_ok=True)

                decompressed = False
                content_to_write = content_blob  # Default to the original blob

                if compression == 'brotli':
                    try:
                        decompressed_content = brotli.decompress(content_blob)
                        content_to_write = decompressed_content
                        decompressed = True
                    except brotli.error as e:
                        # Log a warning but continue with the original (compressed) content
                        content_manager_logger.warning("Decompression failed for %s: %s. Saving original file.", path,
                                                       e)

                # Write the (possibly compressed) content to the file
                with open(full_path, 'wb') as f:
                    f.write(content_to_write)

                # Compute SHA-256 hash of the content that was written to the file
                file_hash = hashlib.sha256(content_to_write).hexdigest()
                hashes_to_write.append(f"{path}\t{file_hash}\n")

                if decompressed:
                    content_manager_logger.info("Extracted and decompressed: %s", path)
                else:
                    content_manager_logger.info("Extracted: %s (saved in original compressed form)", path)

            # Write hashes to the file
            with open(self.hashes_file_path, 'w', encoding='utf-8') as f:
                f.writelines(hashes_to_write)
            content_manager_logger.info("Hashes written to: %s", self.hashes_file_path)

        except sqlite3.Error as e:
            content_manager_logger.error("Database error: %s", e)
        finally:
            if conn:
                conn.close()

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

            cursor.execute("SELECT id FROM ContentTypes WHERE compression = 'brotli'")
            brotli_type_id = cursor.fetchone()[0]
            cursor.execute("SELECT id FROM ContentTypes WHERE compression = 'none'")
            none_type_id = cursor.fetchone()[0]

            # 5. Process changes
            # Deleted files (single query)
            if deleted_files:
                placeholders = ','.join(['?'] * len(deleted_files))
                query = f"DELETE FROM Content WHERE path IN ({placeholders})"
                cursor.execute(query, deleted_files)
                content_manager_logger.info("Successfully deleted %d files.", len(deleted_files))

            # New and modified files (prepare data)
            new_files_data = []

            for path in new_files:
                full_path = os.path.join(self.input_dir, *path.split('/'))
                try:
                    with open(full_path, 'rb') as f:
                        file_content = f.read()
                except IOError as e:
                    content_manager_logger.error("Error reading new file %s: %s", full_path, e)
                    continue

                _, ext = os.path.splitext(full_path)

                content_to_store = None
                content_type_id = None
                compressed_status = "uncompressed"

                if ext.lower() in self.compressible_extensions:
                    try:
                        content_to_store = brotli.compress(file_content, quality=11)
                        content_type_id = brotli_type_id
                        compressed_status = "compressed"
                    except brotli.error as e:
                        content_manager_logger.error("Error compressing file %s: %s", path, e)
                        continue
                else:
                    content_to_store = file_content
                    content_type_id = none_type_id

                new_files_data.append((path, content_to_store, content_type_id))
                content_manager_logger.info("Prepared to add new file %s (%s)", path, compressed_status)

            # Batch inserts (single query)
            if new_files_data:
                placeholders = ','.join(['(?, 1, ?, ?)'] * len(new_files_data))
                flat_data = [item for path, content, contentTypeID in new_files_data for item in
                             (path, content, contentTypeID)]
                query = f"INSERT INTO Content (path, languageID, content, contentTypeID) VALUES {placeholders}"
                content_manager_logger.info("Executing INSERT for %d files.", len(new_files_data))
                cursor.execute(query, flat_data)
                content_manager_logger.info("Successfully inserted %d new files.", len(new_files_data))

            # --- Reverting to single UPDATE queries ---
            for path in modified_files:
                full_path = os.path.join(self.input_dir, *path.split('/'))
                try:
                    with open(full_path, 'rb') as f:
                        file_content = f.read()
                except IOError as e:
                    content_manager_logger.error("Error reading modified file %s: %s", full_path, e)
                    continue

                _, ext = os.path.splitext(full_path)

                content_to_store = None
                content_type_id = None
                compressed_status = "uncompressed"

                if ext.lower() in self.compressible_extensions:
                    try:
                        content_to_store = brotli.compress(file_content, quality=11)
                        content_type_id = brotli_type_id
                        compressed_status = "compressed"
                    except brotli.error as e:
                        content_manager_logger.error("Error compressing file %s: %s", path, e)
                        continue
                else:
                    content_to_store = file_content
                    content_type_id = none_type_id

                # Execute single UPDATE query
                query = "UPDATE Content SET content = ?, contentTypeID = ? WHERE path = ?"
                cursor.execute(query, (content_to_store, content_type_id, path))
                content_manager_logger.info("Successfully updated modified file %s (%s)", path, compressed_status)

            # Update LastChange table
            cursor.execute("DROP TABLE IF EXISTS LastChange;")
            cursor.execute("""
                CREATE TABLE LastChange (
                    now TIMESTAMP,
                    who TEXT
                );
            """)
            cursor.execute("INSERT INTO LastChange VALUES (CURRENT_TIMESTAMP, ?);", (self.name,))

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

            cursor.execute("""
                SELECT
                    Content.content,
                    ContentTypes.compression
                FROM
                    Content
                JOIN
                    ContentTypes ON Content.contentTypeID = ContentTypes.id
                WHERE
                    Content.path = ?
            """, (file_path,))

            result = cursor.fetchone()
            if result is None:
                content_manager_logger.warning("File not found in database: %s", file_path)
                return

            content_blob, compression = result

            decompressed = False
            if compression == 'brotli':
                try:
                    content_to_write = brotli.decompress(content_blob)
                    decompressed = True
                except brotli.error as e:
                    content_manager_logger.error("Error decompressing %s: %s", file_path, e)
                    return
            else:
                content_to_write = content_blob

            full_path = os.path.join(self.output_dir, *file_path.split('/'))
            os.makedirs(os.path.dirname(full_path), exist_ok=True)

            with open(full_path, 'wb') as f:
                f.write(content_to_write)

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
                        required=False)  # This is a new required argument.

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
                                 output_db_path=args.output_db, hashes_file_path=args.hashes_file, name=args.name)
        manager.build_database()
    elif args.operation == 'dump_one':
        if not args.input_db or not args.output_dir or not args.single_file_path:
            parser.error("--input-database, --output-dir, and --file-path are required for 'dump_one' mode.")
        manager = ContentManager(input_db_path=args.input_db, output_dir=args.output_dir)
        manager.dump_one(args.single_file_path)


if __name__ == "__main__":
    setup_logging()
    main()