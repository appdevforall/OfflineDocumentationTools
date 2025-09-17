import json
import sqlite3
import os


def insert_android_and_androidx(conn):
    tooltip_data_androidx = json.loads(open("androidx_tooltips.json", "r").read())
    tooltip_data_android = json.loads(open("android_tooltips.json", "r").read())
    tooltip_data = {**tooltip_data_androidx, **tooltip_data_android}


    cursor = conn.cursor()


    # Delete existing androidx tooltips
    cursor.execute("""DELETE FROM Tooltips
        WHERE id IN (
          SELECT tooltipId
          FROM TooltipButtons
          WHERE uri LIKE '%androidx/%'
        );""")
    # Delete existing androidx tooltips
    cursor.execute("""DELETE FROM Tooltips
        WHERE id IN (
          SELECT tooltipId
          FROM TooltipButtons
          WHERE uri LIKE '%android/%'
        );""")

    tooltip_count = len(tooltip_data)

    for idx, tag in enumerate(tooltip_data):
        if idx % 100 == 0:
            print(f"{idx} tooltips inserted out of {tooltip_count}")
        cursor.execute("""
            INSERT OR REPLACE INTO Tooltips
            (categoryId, tag, summary, detail)
            VALUES (?, ?, ?, ?)
            """, (3, tag, tooltip_data[tag]["tooltipSummary"], tooltip_data[tag]["tooltipDetail"]))

        cursor.execute("""
                INSERT OR REPLACE INTO Tooltips
                (categoryId, tag, summary, detail)
                VALUES (?, ?, ?, ?)
                """, (4, tag, tooltip_data[tag]["tooltipSummary"], tooltip_data[tag]["tooltipDetail"]))

        conn.commit()

def create_tooltip_buttons(conn):
    """
    Creates entries in TooltipButtons for every entry in Tooltips
    whose tag contains "androidx.".
    """
    log = "content_log.txt"
    f = open(log, "w")
    f.write("\n")
    f.close()

    f = open(log, "a")

    try:
        cursor = conn.cursor()

        # Delete existing button entries for Android/AndroidX tooltips
        cursor.execute("DELETE FROM TooltipButtons WHERE tooltipId IN (SELECT id FROM Tooltips WHERE tag LIKE ?)",
                       ('%androidx%',))
        cursor.execute("DELETE FROM TooltipButtons WHERE tooltipId IN (SELECT id FROM Tooltips WHERE tag LIKE ?)",
                       ('%android%',))

        # Select all tooltips with "androidx." or "android." in their tag
        # along with their original JSON data stored in a file.
        tooltip_data_androidx = json.loads(open("androidx_tooltips.json", "r").read())
        tooltip_data_android = json.loads(open("android_tooltips.json", "r").read())
        tooltip_data = {**tooltip_data_androidx, **tooltip_data_android}

        cursor.execute("SELECT id, tag FROM Tooltips WHERE tag LIKE ? OR tag LIKE ?", ('%androidx.%', '%android.%',))
        tooltips_to_process = cursor.fetchall()

        if not tooltips_to_process:
            print("No tooltips found with 'androidx.' or 'android.' in the tag. No entries will be created.")
            return

        f.write(f"Found {len(tooltips_to_process)} tooltips to process.")

        total = len(tooltips_to_process)

        for idx, (tooltip_id, tooltip_tag) in enumerate(tooltips_to_process):
            debug = False
            if tooltip_tag in ["androidx.media3.common.C.VideoOutputMode", "android.telephony.NetworkScan"]:
                debug = True
            # if idx % 100 == 0:
                # print(f'{idx} out of {total} tooltips buttons\' processed')

            # Retrieve button information from the original JSON data
            buttons_json_str = tooltip_data.get(tooltip_tag, {}).get("tooltipButtons", "[]")
            buttons = json.loads(buttons_json_str)

            for button in buttons:
                description = button.get("first")
                base_name = button.get("second")

                if not description or not base_name:
                    continue

                # Correctly construct the URI by replacing all dots in the full tag
                # with slashes, then replacing the last part with the base_name
                # from the JSON.

                # Split the base name to handle cases like "C.VideoOutputMode.html"
                base_name_without_ext = base_name.rsplit('.', 1)[0]

                # Create the directory path by replacing dots in the tag with slashes
                # up to the point of the base name.
                if '.' in base_name_without_ext:
                    # For nested classes like C.VideoOutputMode
                    # e.g., androidx.media3.common.C.VideoOutputMode
                    # Split the tag at the base name

                    # Split the full tag at the base name to get the path
                    path_parts = tooltip_tag.split('.')
                    base_name_parts = base_name_without_ext.split('.')

                    # Find where the base name starts in the tag
                    split_idx = len(path_parts) - len(base_name_parts)

                    # Construct the directory path from the tag's package name
                    uri_path_prefix = '/'.join(path_parts[:split_idx])

                    # The full path is the prefix + the base name with slashes
                    uri_path_suffix = '.'.join(base_name_parts)

                    uri_path = f"{uri_path_prefix}/{uri_path_suffix}"
                    if debug:
                        print("path_parts: " + str(path_parts))
                        print("base_name_parts: " + str(base_name_parts))
                        print("split_idx: " + str(split_idx))
                        print("uri_path_prefix: " + str(uri_path_prefix))
                        print("uri_path_suffix: " + str(uri_path_suffix))

                else:
                    # Simple class name, e.g., C
                    # The path is just the package name with the class name appended
                    uri_path = tooltip_tag.replace('.', '/')


                # The final URI is the base path "a/" + the constructed path + ".html"
                uri = f"a/{uri_path}.html"
                if debug:
                    print("uri: " + str(uri))

                # Insert the new entry into the TooltipButtons table
                button_number_id = 1  # Assuming a single button per tooltip
                cursor.execute("""
                    INSERT OR REPLACE INTO TooltipButtons (tooltipId, buttonNumberId, description, uri)
                    VALUES (?, ?, ?, ?)""", (tooltip_id, button_number_id, description, uri))

        conn.commit()
        print("Successfully created all TooltipButtons entries.")
    except sqlite3.Error as e:
        print(f"sqlite error updating content/buttons: {e}")
    finally:
        f.close()

def fix_path_capitalization_executemany(conn) -> None:
    """
    Corrects capitalization by processing data in Python and using
    executemany() for an efficient batch update.

    Args:
        db_path (str): The file path to the SQLite database.
    """
    try:
        cursor = conn.cursor()
        logfile = "log.txt"
        handle = open(logfile, "w")
        handle.write("\n")
        handle.close()
        handle = open(logfile, "a")
        # Step 1: Fetch all case-insensitive URIs from TooltipButtons.
        # Store them in a dictionary for fast in-memory lookup.
        # This is a one-time operation.
        cursor.execute("SELECT uri FROM TooltipButtons;")
        tooltip_uris = {row[0].lower(): row[0] for row in cursor.fetchall()}

        # Step 2: Fetch the current paths from Content.
        # We'll use this list to find which paths need updating.
        cursor.execute("SELECT path FROM Content;")
        content_paths = [row[0] for row in cursor.fetchall()]

        # Step 3: Prepare the list of tuples for the batch update.
        updates = []
        for path in content_paths:
            # Look up the case-sensitive URI using the lowercase version.
            correct_uri = tooltip_uris.get(path.lower())

            # If a match is found and the capitalization differs, add it to our list.
            if correct_uri and correct_uri != path:
                handle.write("Path in Content: " + path + "\tPath in TooltipButtons: " + correct_uri + "\n")
                updates.append((correct_uri, path))

        # Step 4: Execute the batch update using executemany().
        # This is the most efficient way to perform multiple updates.
        if updates:
            print(f"Preparing to update {len(updates)} path entries...")
            update_query = "UPDATE Content SET path = ? WHERE path = ?;"
            cursor.executemany(update_query, updates)
            conn.commit()
        else:
            print("no updates")

    except sqlite3.Error as e:
        print(f"❌ Database error: {e}")

    finally:
        if conn:
            conn.close()
def verify_android_uris_in_content(conn):
    """
    Verifies that every uri in TooltipButtons has a corresponding record in Content.
    """
    print("\n--- Verifying URI paths ---")
    log_handle = open("log.txt", "w")

    try:
        cursor = conn.cursor()
        # Get all URIs from the TooltipButtons table
        cursor.execute("SELECT uri FROM TooltipButtons")
        all_uris = set([row[0] for row in cursor.fetchall()])

        if not all_uris:
            print("No URIs found in TooltipButtons to verify.")
            return

        for idx, uri in enumerate(all_uris[:300]):
            if idx % 100 == 0:
                print(idx)
            if uri[:2] != "a/":
                continue
            # Check if a matching path exists in the Content table
            cursor.execute("SELECT COUNT(*) FROM Content WHERE path = ?", (uri,))
            count = cursor.fetchone()[0]

            if count == 0:
                error_str = f"Warning: No matching content record found for URI: {uri}"
                log_handle.write(error_str + "\n")
                print(error_str)
            else:
                success_str = f"Found content record for URI: {uri}"
                log_handle.write(success_str + "\n")
    except sqlite3.Error as e:
        print(f"Error verifying URIs: {e}")

    log_handle.close()


def main():
    # os.system("./clean.sh")

    conn = sqlite3.connect("documentation.db")
    # insert_android_and_androidx(conn)
    create_tooltip_buttons(conn)
    # fix_path_capitalization_executemany(conn)
    conn.close()


if __name__ == '__main__':
    main()
