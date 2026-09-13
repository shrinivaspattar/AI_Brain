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


def test_does_not_protect_a_second_root_it_was_never_told_about(
    tmp_path: Path,
) -> None:
    """Documents the EXACT scope of this safety check, tied to a real
    incident found during D2's final pre-commit verification: a test
    write directed at a real, second T7 path (not the one a given
    analysis had actually scanned) was blocked only by OS permissions,
    not by this helper - because this helper protects ONLY the
    specific `root` it is given, and has no concept of "other locations
    the caller separately considers sensitive." That is not a bug here;
    it is this helper's documented boundary - see "Protected-root
    policy" in AI_Brain_Architecture.md's D2 section for why a
    multi-root policy belongs in a separate, explicit layer rather than
    being folded into this single-root check. Uses two entirely
    synthetic directories under `tmp_path` - this test never
    references, opens, or writes anywhere near a real filesystem path,
    by design, given what triggered writing it."""
    active_root = tmp_path / "active_analysis_root"
    active_root.mkdir()
    a_different_sensitive_root = tmp_path / "a_different_sensitive_root"
    a_different_sensitive_root.mkdir()

    destination_inside_the_other_root = a_different_sensitive_root / "report.json"

    # Must NOT raise - proving the helper's protection is scoped to the
    # root it was actually given, never a general "protect everything
    # sensitive" guarantee it was never designed to provide.
    reject_destination_inside_root(active_root, destination_inside_the_other_root)


def test_caller_can_protect_multiple_roots_by_checking_each_explicitly(
    tmp_path: Path,
) -> None:
    """Until a dedicated protected-root policy layer exists (see
    AI_Brain_Architecture.md's D2 Safety Follow-up), a caller wanting
    MULTIPLE locations protected must call this helper once per
    protected root - there is no single call that checks a destination
    against a list. Demonstrates the correct compositional pattern,
    entirely with synthetic paths."""
    protected_roots = [tmp_path / "protected_a", tmp_path / "protected_b"]
    for root in protected_roots:
        root.mkdir()

    destination_inside_b = tmp_path / "protected_b" / "sneaky.json"

    with pytest.raises(ValueError, match="scanned root"):
        for root in protected_roots:
            reject_destination_inside_root(root, destination_inside_b)
