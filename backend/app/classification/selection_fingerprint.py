from __future__ import annotations

import hashlib
import json


def compute_selection_fingerprint(
    *,
    d0_report_sha256: str,
    selection_policy_version: str,
    ordering_version: str,
    max_source_instances: int,
    max_source_bytes: int,
    max_extracted_bytes: int | None,
    max_embeddings: int,
    max_runtime_seconds: int,
    selected_paths: list[tuple[str, str | None]],
) -> str:
    """SHA-256 hex digest of a canonical serialization of exactly the
    frozen fingerprint inputs (D0 report hash + selection_policy_version
    + ordering_version + envelope values + sorted selected source
    references) - durable proof of exactly what was authorized/executed
    (frozen implementation design, point 8).

    Deterministic across process runs, Python versions, database row
    order, and transaction timing: `selected_paths` is explicitly
    re-sorted here (never trusting caller order, which could reflect
    database row order), `json.dumps(..., sort_keys=True)` removes any
    dict-insertion-order dependency, and only path STRINGS are used -
    never a SourceInstance integer id, which is assigned by the
    database and would make the fingerprint depend on incidental
    row-creation order rather than the selection itself.
    """
    canonical = {
        "d0_report_sha256": d0_report_sha256,
        "selection_policy_version": selection_policy_version,
        "ordering_version": ordering_version,
        "envelope": {
            "max_source_instances": max_source_instances,
            "max_source_bytes": max_source_bytes,
            "max_extracted_bytes": max_extracted_bytes,
            "max_embeddings": max_embeddings,
            "max_runtime_seconds": max_runtime_seconds,
        },
        "selected_paths": sorted(
            (
                {"root_t7_path": root_t7_path, "member_path": member_path}
                for root_t7_path, member_path in selected_paths
            ),
            key=lambda entry: (entry["root_t7_path"], entry["member_path"] or ""),
        ),
    }
    canonical_json = json.dumps(canonical, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(canonical_json.encode("utf-8")).hexdigest()
