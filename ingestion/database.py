import sqlite3
from pathlib import Path

# Location of the SQLite database
DB_PATH = Path(__file__).resolve().parent.parent / "database" / "knowledge.db"


def get_connection():
    return sqlite3.connect(DB_PATH)