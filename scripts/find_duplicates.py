import sqlite3
from pathlib import Path

DB_PATH = Path(__file__).resolve().parent.parent / "database" / "knowledge.db"


def main():
    conn = sqlite3.connect(DB_PATH)

    cursor = conn.execute("""
        SELECT
            sha256,
            COUNT(*) AS copies
        FROM files
        GROUP BY sha256
        HAVING COUNT(*) > 1
    """)

    duplicates = cursor.fetchall()

    if not duplicates:
        print("No duplicate files found.")
        conn.close()
        return

    print("=" * 60)
    print("Duplicate Files")
    print("=" * 60)

    for sha, copies in duplicates:
        print(f"\nSHA256 : {sha}")
        print(f"Copies : {copies}")

        rows = conn.execute(
            """
            SELECT filename, path
            FROM files
            WHERE sha256 = ?
            """,
            (sha,),
        ).fetchall()

        for filename, path in rows:
            print(f"  - {filename}")
            print(f"    {path}")

    conn.close()


if __name__ == "__main__":
    main()