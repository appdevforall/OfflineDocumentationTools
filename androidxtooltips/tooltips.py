import json
import sqlite3
import os

def main():
    tooltip_data = json.loads(open("androidx_tooltips.json", "r").read())
    print(tooltip_data["androidx.media3.muxer.AacMuxer"])


    os.system("./clean.sh")

    conn = sqlite3.connect("documentation.db")
    cursor = conn.cursor()

    # Delete existing androidx tooltips
    cursor.execute("""DELETE FROM Tooltips
WHERE id IN (
  SELECT tooltipId
  FROM TooltipButtons
  WHERE uri LIKE '%androidx/%'
);""")

    tooltip_id = 100000

    for tag in tooltip_data:
        cursor.execute("""
            INSERT OR REPLACE INTO Tooltips
            (id, categoryId, tag, summary, detail)
            VALUES (?, ?, ?, ?, ?)
            """, (tooltip_id, 3, tag, tooltip_data[tag]["tooltipSummary"], tooltip_data[tag]["tooltipDetail"]))


        cursor.execute("""
                INSERT OR REPLACE INTO Tooltips
                (id, categoryId, tag, summary, detail)
                VALUES (?, ?, ?, ?, ?)
                """, (tooltip_id + 1, 4, tag, tooltip_data[tag]["tooltipSummary"], tooltip_data[tag]["tooltipDetail"]))

        conn.commit()

        tooltip_id += 2
    conn.close()

if __name__ == '__main__':
    main()

