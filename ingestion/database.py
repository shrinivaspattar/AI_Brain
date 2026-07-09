import sqlite3
from pathlib import Path

DB_PATH = Path(__file__).resolve().parent.parent / "database" / "knowledge.db"


class Database:
    def __init__(self):
        self.conn = sqlite3.connect(DB_PATH)
        self.cursor = self.conn.cursor()

    def get_file(self, path):
        self.cursor.execute(
            "SELECT modified FROM files WHERE path = ?",
            (path,),
        )
        return self.cursor.fetchone()

    def get_all_paths(self):
        self.cursor.execute("SELECT path FROM files")
        return [row[0] for row in self.cursor.fetchall()]

    def delete_file(self, path):
        self.conn.execute(
            "DELETE FROM files WHERE path = ?",
            (path,),
        )
        self.conn.commit()

    def insert(self, info):
        self.conn.execute(
            """
            INSERT OR REPLACE INTO files (
                path,
                filename,
                extension,
                size,
                modified,
                sha256,
                mime,
                content
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                info["path"],
                info["name"],
                info["extension"],
                info["size"],
                info["modified"],
                info["sha256"],
                info["mime"],
                info["content"],
            ),
        )
        self.conn.commit()

    def close(self):
        self.conn.close()