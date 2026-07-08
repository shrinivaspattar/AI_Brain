import sqlite3
from pathlib import Path


DB_PATH = Path("database/knowledge.db")


def connect():
    return sqlite3.connect(DB_PATH)


def initialize():

    conn = connect()

    with open("database/schema.sql") as f:
        conn.executescript(f.read())

    conn.commit()
    conn.close()