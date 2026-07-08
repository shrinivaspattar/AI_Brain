CREATE TABLE IF NOT EXISTS files (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    path TEXT UNIQUE,
    filename TEXT,
    extension TEXT,
    size INTEGER,
    modified TEXT
);