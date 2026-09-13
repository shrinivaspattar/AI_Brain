"""Shared safety primitive for every read-only discovery report writer
in this package (`corpus_inventory.py`, `duplicate_analysis.py`, and
any future addition) - a single, tested place for the "a report must
never be written back into the corpus it describes" invariant, rather
than each writer re-implementing (and potentially re-diverging on) the
same safety-critical check.
"""

from __future__ import annotations

from pathlib import Path


def reject_destination_inside_root(root: str | Path, destination: str | Path) -> None:
    """Raise `ValueError` if `destination` resolves to `root` itself or
    anywhere inside it - checked via `.resolve(strict=False)` on BOTH
    paths, never the raw, unresolved strings, so a `..` segment or a
    symlinked intermediate directory that resolves into the root is
    caught, not merely a literal path-string prefix match.
    `strict=False` is deliberate: `destination` (and possibly several
    of its trailing components) need not exist yet - whatever prefix
    already exists on disk is resolved through any symlinks, and the
    rest is appended literally, which is exactly the containment
    question that matters here. Raises nothing (returns normally) if
    `destination` is genuinely outside `root`.
    """
    resolved_root = Path(root).resolve(strict=False)
    resolved_destination = Path(destination).resolve(strict=False)

    if resolved_destination == resolved_root or resolved_destination.is_relative_to(
        resolved_root
    ):
        raise ValueError(
            f"Report destination {destination} resolves to "
            f"{resolved_destination}, which is the scanned root "
            f"{resolved_root} or a location inside it - a discovery "
            "report must never be written into the corpus it describes"
        )
