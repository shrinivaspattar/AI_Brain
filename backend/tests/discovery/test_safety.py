from pathlib import Path

import pytest

from app.discovery.safety import reject_destination_inside_root


def test_rejects_destination_equal_to_root(tmp_path: Path) -> None:
    root = tmp_path / "corpus"
    root.mkdir()

    with pytest.raises(ValueError, match="scanned root"):
        reject_destination_inside_root(root, root)


def test_rejects_destination_inside_root(tmp_path: Path) -> None:
    root = tmp_path / "corpus"
    root.mkdir()

    with pytest.raises(ValueError, match="scanned root"):
        reject_destination_inside_root(root, root / "a" / "b" / "report.json")


def test_rejects_dotdot_resolving_inside_root(tmp_path: Path) -> None:
    root = tmp_path / "corpus"
    (root / "sub").mkdir(parents=True)

    with pytest.raises(ValueError, match="scanned root"):
        reject_destination_inside_root(root, root / "sub" / ".." / "report.json")


def test_accepts_legitimate_destination_outside_root(tmp_path: Path) -> None:
    root = tmp_path / "corpus"
    root.mkdir()

    # Must not raise.
    reject_destination_inside_root(root, tmp_path / "reports" / "report.json")


def test_accepts_sibling_directory_with_similar_name_prefix(tmp_path: Path) -> None:
    """A naive string-prefix check would wrongly reject this - "corpus2"
    starts with the literal characters of "corpus" but is a completely
    different, sibling directory once both are resolved as paths."""
    root = tmp_path / "corpus"
    root.mkdir()
    sibling = tmp_path / "corpus2"
    sibling.mkdir()

    reject_destination_inside_root(root, sibling / "report.json")
