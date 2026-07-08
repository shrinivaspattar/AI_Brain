from pathlib import Path

from metadata import read_metadata


class FileScanner:

    def __init__(self, root):
        self.root = Path(root)

    def scan(self):

        if not self.root.exists():
            print("Folder not found.")
            return

        count = 0

        for path in self.root.rglob("*"):

            if path.is_file():

                file = read_metadata(path)

                print("=" * 60)
                print(f"Name      : {file.filename}")
                print(f"Extension : {file.extension}")
                print(f"Size      : {file.size:,} bytes")
                print(f"Modified  : {file.modified}")
                print(f"Path      : {file.path}")

                count += 1

        print("\nFinished")
        print(f"Files : {count}")


if __name__ == "__main__":

    folder = input("Folder to scan: ")

    FileScanner(folder).scan()