import sqlite3

conn = sqlite3.connect("database/knowledge.db")
cursor = conn.cursor()

query = input("Search: ").strip()

cursor.execute("""
SELECT filename, path, extension, content
FROM files
WHERE filename LIKE ?
   OR content LIKE ?
""", (f"%{query}%", f"%{query}%"))

rows = cursor.fetchall()

if not rows:
    print("No matches found.")
else:
    for name, path, extension, content in rows:
        print("=" * 60)
        print(f"File      : {name}")
        print(f"Path      : {path}")
        print(f"Extension : {extension}")

        if content:
            preview = content[:200].strip().replace("\n", " ")
            print("\nPreview")
            print("-------")
            print(preview)

        print()

conn.close()