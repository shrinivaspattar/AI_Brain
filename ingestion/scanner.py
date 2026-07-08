from pathlib import Path

from metadata import get_metadata
from database import Database


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

                db.insert(info)

                print("-" * 60)
                print(f"Name      : {info['name']}")
                print(f"Extension : {info['extension']}")
                print(f"Size      : {info['size']} bytes")
                print(f"Modified  : {info['modified']}")
                print(f"Path      : {info['path']}")

                count += 1

        db.close()

        print(f"\nFinished. Files found: {count}")


if __name__ == "__main__":
    folder = input("Folder to scan: ").strip()

    scanner = FileScanner(folder)

    scanner.scan()