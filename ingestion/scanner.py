import os
from pathlib import Path
from .mime import detect_mime
from .metadata import get_metadata
from .database import Database
from .hash import sha256_file
from .archive import is_archive
from .text import extract_text
from config.ignore import IGNORE_DIRS, IGNORE_FILES
from .archive_reader import (
    open_zip,
    list_zip_contents_from_archive,
)

class FileScanner:
    def __init__(self, root):
        self.root = Path(root)

    def scan(self):

        if not self.root.exists():
            print("Folder not found.")
            return

        db = Database()

        scanned_paths = set()
        scanned = 0
        indexed = 0
        skipped = 0
        removed = 0

        scanned_paths = set()
        

        for root, dirs, files in os.walk(self.root):

            
            dirs[:] = [d for d in dirs if d not in IGNORE_DIRS]

            for filename in files:

                path = Path(root) / filename
                scanned += 1
                scanned_paths.add(str(path))

                if path.name in IGNORE_FILES:
                    continue

                info = get_metadata(path)
                existing = db.get_file(info["path"])
                

                if existing and existing[0] == str(info["modified"]):
                    skipped += 1
                    print(f"Skipping: {path.name}")
                    continue
                info["sha256"] = sha256_file(path)
                info["mime"] = detect_mime(path)
                info["archive"] = is_archive(path)

                text = extract_text(path)
                info["content"] = text

                db.insert(info)
                indexed += 1

                print("-" * 60)
                print(f"Name      : {info['name']}")
                print(f"Extension : {info['extension']}")
                print(f"Size      : {info['size']} bytes")
                print(f"Modified  : {info['modified']}")
                print(f"SHA256    : {info['sha256']}")
                print(f"Path      : {info['path']}")
                print(f"MIME      : {info['mime']}")
                print(f"Archive   : {info['archive']}")

                if text:
                    print("Preview:")
                    print(text[:100].strip())

                if info["archive"] and path.suffix.lower() == ".zip":
                    print("Contents:")

                    with open_zip(path) as archive:
                        items = list_zip_contents_from_archive(archive)

                        for item in items:
                            print(f"  - {item['name']}")
                            print(f"      Size            : {item['size']} bytes")
                            print(f"      Compressed Size : {item['compressed']} bytes")
                            print(f"      Archive         : {item['is_archive']}")

        
        db_paths = set(db.get_all_paths())

        deleted = db_paths - scanned_paths

        for path in deleted:
            print(f"Removing deleted file: {path}")
            db.delete_file(path)
            removed += 1
        db.close()
        print("\nScan Summary")
        print("-" * 40)
        print(f"Scanned : {scanned}")
        print(f"Indexed : {indexed}")
        print(f"Skipped : {skipped}")
        print(f"Removed : {removed}")
if __name__ == "__main__":
    folder = input("Folder to scan: ").strip()

    scanner = FileScanner(folder)

    scanner.scan()
    