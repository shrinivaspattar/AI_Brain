from pathlib import Path


class FileScanner:
    def __init__(self, root):
        self.root = Path(root)

    def scan(self):
        if not self.root.exists():
            print(f"Folder not found: {self.root}")
            return

        count = 0

        for path in self.root.rglob("*"):
            if path.is_file():
                print(path)
                count += 1

        print()
        print(f"Finished.")
        print(f"Files found: {count}")


if __name__ == "__main__":
    root = input("Folder to scan: ").strip()

    scanner = FileScanner(root)

    scanner.scan()
