from pathlib import Path
from .mime import detect_mime
from .metadata import get_metadata
from .database import Database
from .hash import sha256_file
from .archive import is_archive
from .archive_reader import list_zip_contents

class FileScanner:
    def __init__(self, root):
        self.root = Path(root)

    def scan(self):

        if not self.root.exists():
            print("Folder not found.")
            return

        db = Database()

        count = 0

        for path in self.root.rglob("*"):

            if path.is_file():

                info = get_metadata(path)
                

                # Calculate SHA-256
                info["sha256"] = sha256_file(path)
                info["mime"] = detect_mime(path)
                info["archive"] = is_archive(path)

                # Save to SQLite
                db.insert(info)

                print("-" * 60)
                print(f"Name      : {info['name']}")
                print(f"Extension : {info['extension']}")
                print(f"Size      : {info['size']} bytes")
                print(f"Modified  : {info['modified']}")
                print(f"SHA256    : {info['sha256']}")
                print(f"Path      : {info['path']}")
                print(f"MIME      : {info['mime']}")
                print(f"Archive   : {info['archive']}")

                if info["archive"] and path.suffix.lower() == ".zip":
                    print("Contents:")

                    for item in list_zip_contents(path):
                        print(f"  - {item}")

                count += 1

        db.close()

        print(f"\nFinished. Files found: {count}")

if __name__ == "__main__":
    folder = input("Folder to scan: ").strip()

    scanner = FileScanner(folder)

    scanner.scan()