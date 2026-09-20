"""The master-backup guard on DedupFilesystemExecutor's constructor
(decision 0002). Constructor-level only: no database rows, no filesystem
mutation, and no real drive paths - everything lives under tmp_path."""

from unittest.mock import MagicMock

import pytest

from app.core.config import settings
from app.dedup.executor import DedupFilesystemExecutor


@pytest.fixture(autouse=True)
def _no_ambient_protected_paths(monkeypatch):
    """Isolate every test from the developer's own MASTER_BACKUP_PATHS (which
    is set in a real .env); tests that need configured paths set them
    explicitly."""
    monkeypatch.setattr(settings, "MASTER_BACKUP_PATHS", "")


@pytest.fixture
def dirs(tmp_path):
    made = {}
    for name in ("work", "quarantine", "backup", "elsewhere"):
        d = tmp_path / name
        d.mkdir()
        made[name] = d
    return made


def make(allowed, quarantine, **kwargs):
    return DedupFilesystemExecutor(MagicMock(), allowed, quarantine, **kwargs)


def test_no_protection_configured_behaves_as_before(dirs, monkeypatch):
    monkeypatch.setattr(settings, "MASTER_BACKUP_PATHS", "")
    executor = make(dirs["work"], dirs["quarantine"])
    assert executor.protected_roots == ()


def test_refuses_allowed_root_inside_protected_path(dirs):
    inner = dirs["backup"] / "notes"
    inner.mkdir()
    with pytest.raises(ValueError, match="inside protected master-backup"):
        make(inner, dirs["quarantine"], protected_roots=[dirs["backup"]])


def test_refuses_allowed_root_equal_to_protected_path(dirs):
    with pytest.raises(ValueError, match="inside protected master-backup"):
        make(dirs["backup"], dirs["quarantine"], protected_roots=[dirs["backup"]])


def test_refuses_allowed_root_that_contains_protected_path(tmp_path):
    parent = tmp_path / "parent"
    backup = parent / "backup"
    quarantine = tmp_path / "quarantine"
    backup.mkdir(parents=True)
    quarantine.mkdir()
    with pytest.raises(ValueError, match="contains protected master-backup"):
        make(parent, quarantine, protected_roots=[backup])


def test_refuses_quarantine_root_inside_protected_path(dirs):
    inside = dirs["backup"] / "quarantine_here"
    inside.mkdir()
    with pytest.raises(ValueError, match="quarantine_root .* inside protected"):
        make(dirs["work"], inside, protected_roots=[dirs["backup"]])


def test_unrelated_protected_path_is_allowed_and_recorded(dirs):
    executor = make(dirs["work"], dirs["quarantine"], protected_roots=[dirs["backup"]])
    assert executor.protected_roots == (dirs["backup"].resolve(),)


def test_configured_paths_apply_without_any_argument(dirs, monkeypatch):
    monkeypatch.setattr(settings, "MASTER_BACKUP_PATHS", str(dirs["backup"]))
    inner = dirs["backup"] / "sub"
    inner.mkdir()
    with pytest.raises(ValueError, match="inside protected master-backup"):
        make(inner, dirs["quarantine"])


def test_caller_can_add_to_but_not_remove_configured_paths(dirs, monkeypatch):
    monkeypatch.setattr(settings, "MASTER_BACKUP_PATHS", str(dirs["backup"]))
    with pytest.raises(ValueError):
        make(dirs["backup"], dirs["quarantine"], protected_roots=[dirs["elsewhere"]])


def test_configured_paths_parse_commas_and_blanks(monkeypatch):
    monkeypatch.setattr(settings, "MASTER_BACKUP_PATHS", " /a/one , ,/b/two,")
    assert [str(p) for p in settings.master_backup_paths()] == ["/a/one", "/b/two"]


def test_unmounted_protected_path_still_protects(tmp_path):
    # A protected drive that is not currently mounted does not exist on disk,
    # and the guard must not need it to. A parent folder that WOULD contain it
    # is still refused.
    parent = tmp_path / "media"
    ghost = parent / "not_mounted_drive"
    quarantine = tmp_path / "quarantine"
    parent.mkdir()
    quarantine.mkdir()
    assert not ghost.exists()

    with pytest.raises(ValueError, match="contains protected master-backup"):
        make(parent, quarantine, protected_roots=[ghost])
