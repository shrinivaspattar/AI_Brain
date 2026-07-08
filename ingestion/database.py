import sqlite3
from pathlib import Path

DB_PATH = Path(__file__).resolve().parent.parent / "database" / "knowledge.db"


class Database:
    def __init__(self):
        self.conn = sqlite3.connect(DB_PATH)

    def insert(self, info):
        self.conn.execute(
            """
            INSERT OR IGNORE INTO files
            (path, filename, extension, size, modified)
            VALUES (?, ?, ?, ?, ?)
            """,
            (
                info["path"],
                info["name"],
                info["extension"],
                info["size"],
                str(info["modified"]),
            ),
        )
        self.conn.commit()

    def close(self):
        self.conn.close()