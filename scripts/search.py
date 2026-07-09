import sqlite3

conn = sqlite3.connect("database/knowledge.db")
cursor = conn.cursor()
query = input("Search: ").strip()
cursor.execute("""
SELECT filename, path, content
FROM files
WHERE content LIKE ?
""", (f"%{query}%",))
rows = cursor.fetchall()

if not rows:
    print("No matches.")
else:
    for name, path, content in rows:
        print(name)
        print(path)

        if content:
            preview = content[:200].replace("\n", " ")
            print(preview)

        print("-" * 50)
conn.close()