import sqlite3
from pathlib import Path

DB_PATH = Path(__file__).resolve().parent.parent / "database" / "knowledge.db"


def format_size(size):
    """Convert bytes into a human-readable format."""

    units = ["B", "KB", "MB", "GB", "TB"]

    value = float(size)

    for unit in units:
        if value < 1024 or unit == units[-1]:
            return f"{value:.2f} {unit}"
        value /= 1024


def main():

    conn = sqlite3.connect(DB_PATH)

    cursor = conn.execute(
        """
        SELECT
            sha256,
            COUNT(*) AS copies,
            MAX(size) AS size
        FROM files
        GROUP BY sha256
        HAVING COUNT(*) > 1
        """
    )

    duplicates = cursor.fetchall()

    if not duplicates:
        print("No duplicate files found.")
        conn.close()
        return

    print("=" * 70)
    print("Duplicate File Report")
    print("=" * 70)

    group = 1

    total_wasted = 0

    for sha, copies, size in duplicates:

        wasted = size * (copies - 1)

        total_wasted += wasted

        print(f"\nDuplicate Group #{group}")
        print("-" * 70)

        print(f"SHA256          : {sha}")
        print(f"Copies          : {copies}")
        print(f"Size per file   : {format_size(size)}")
        print(f"Wasted space    : {format_size(wasted)}")

        rows = conn.execute(
            """
            SELECT filename, path
            FROM files
            WHERE sha256 = ?
            ORDER BY filename
            """,
            (sha,),
        ).fetchall()

        print("\nFiles:")

        for i, (filename, path) in enumerate(rows, start=1):
            print(f"\n{i}. {filename}")
            print(f"   {path}")

        group += 1

    print("\n" + "=" * 70)
    print(f"Total Recoverable Space : {format_size(total_wasted)}")
    print("=" * 70)

    conn.close()


if __name__ == "__main__":
    main()