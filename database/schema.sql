CREATE TABLE files (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    path TEXT UNIQUE,
    filename TEXT,
    extension TEXT,
    size INTEGER,
    modified TEXT,
    sha256 TEXT,
    mime TEXT
);