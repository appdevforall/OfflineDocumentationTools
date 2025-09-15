import sqlite3

try:
    with sqlite3.connect("empty.db") as conn:
        # The database file "my_empty_database.db" is created upon connecting.
        # You can perform database operations here if needed.
        pass
except sqlite3.OperationalError as e:
    print(f"Failed to create or open database: {e}")