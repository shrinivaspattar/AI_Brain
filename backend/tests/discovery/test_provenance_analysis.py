import json
from pathlib import Path

import pytest

from app.discovery.provenance_analysis import (
    CURRENT_KEYWORDS,
    HISTORICAL_KEYWORDS,
    INFERENCE_CONFIDENCE,
    INFERENCE_DIVERGENT_DATE_SIGNAL,
    INFERENCE_STALE_REFERENCE_UNVERIFIABLE,
    INFERENCE_NO_PROVENANCE_SIGNAL,
    INFERENCE_PARTIAL_DATE_OR_KEYWORD_SIGNAL,
    INFERENCE_PROSE,
    INFERENCE_UNIFORM_KEYWORD_NO_FURTHER_SIGNAL,
    _extract_date_tokens,
    _find_keywords,
    analyze_provenance,
    classify,
    collect_path_signals,
    load_raw_from_previous_report,
    write_provenance_report,
)


def _write_d1_report(
    tmp_path: Path,
    *,
    exact_duplicate_groups=None,
    cryptomator_chunk_duplicate_groups=None,
    directory_duplicate_groups=None,
    root: Path | None = None,
) -> Path:
    report = {
        "root": str(root or tmp_path),
        "analyzed_at": "2026-09-13T00:00:00+00:00",
        "exact_duplicate_groups": exact_duplicate_groups or [],
        "cryptomator_chunk_duplicate_groups": cryptomator_chunk_duplicate_groups or [],
        "directory_duplicate_groups": directory_duplicate_groups or [],
    }
    d1_path = tmp_path / "d1.json"
    d1_path.write_text(json.dumps(report))
    return d1_path


def _group(paths, content_hash="hash1", size=10, copies=None):
    return {
        "size_bytes": size,
        "content_hash": content_hash,
        "paths": [str(p) for p in paths],
        "copies": copies or len(paths),
        "reclaimable_bytes": size * ((copies or len(paths)) - 1),
        "category": "exact_duplicate",
    }


# --- date token extraction (unchanged from prior review pass) ---------


def test_extract_date_tokens_recognizes_ddmmyyyy() -> None:
    tokens = _extract_date_tokens("/corpus/27082026/file.txt")
    assert len(tokens) == 1
    assert tokens[0].parsed_date == "2026-08-27"


def test_extract_date_tokens_recognizes_dashed_ddmmyyyy() -> None:
    tokens = _extract_date_tokens("/corpus/backup-19-02-2026/file.txt")
    assert tokens[0].parsed_date == "2026-02-19"


def test_extract_date_tokens_rejects_implausible_dates() -> None:
    assert _extract_date_tokens("/corpus/99999999/file.txt") == []


def test_extract_date_tokens_rejects_out_of_range_year() -> None:
    assert _extract_date_tokens("/corpus/01011826/file.txt") == []


def test_extract_date_tokens_falls_back_to_yyyymmdd() -> None:
    tokens = _extract_date_tokens("/corpus/takeout-20260604T054024Z/file.txt")
    assert any(t.parsed_date == "2026-06-04" for t in tokens)


def test_extract_date_tokens_finds_none_when_absent() -> None:
    assert _extract_date_tokens("/corpus/current/file.txt") == []


def test_find_keywords_is_case_insensitive() -> None:
    assert "archive" in _find_keywords("/corpus/ARCHIVE/file.txt", HISTORICAL_KEYWORDS)


def test_find_keywords_returns_empty_when_absent() -> None:
    assert _find_keywords("/corpus/current/file.txt", HISTORICAL_KEYWORDS) == []
    assert _find_keywords("/corpus/backup/file.txt", CURRENT_KEYWORDS) == []


# --- inference codes describe evidence patterns, not conclusions ------


def test_inference_codes_do_not_assert_current_or_historical_in_their_name() -> None:
    """The specific naming defect this review pass corrected: no
    inference code should itself claim which copy is 'current' or
    'historical' - that interpretation belongs only in the separately-
    rendered prose, explicitly hedged."""
    for code in INFERENCE_CONFIDENCE:
        assert "current" not in code.lower()
        assert "historical" not in code.lower()
        assert "backup" not in code.lower() or "signal" in code.lower()


def test_every_inference_code_has_rendered_prose() -> None:
    for code in INFERENCE_CONFIDENCE:
        assert code in INFERENCE_PROSE
        assert len(INFERENCE_PROSE[code]) > 0


def test_partial_signal_prose_explicitly_denies_proving_currency(tmp_path: Path) -> None:
    """The exact epistemic correction requested: verify the rendered
    prose for the weakest "asymmetric naming" pattern explicitly denies
    that it proves which copy is current."""
    prose = INFERENCE_PROSE[INFERENCE_PARTIAL_DATE_OR_KEYWORD_SIGNAL]
    assert "NOT proof" in prose
    assert "does not establish which copy" in prose.lower()


# --- classification: divergent dates (strongest available signal) -----


def test_classifies_differing_dates_as_divergent_date_signal(tmp_path: Path) -> None:
    root = tmp_path / "corpus"
    (root / "27082026").mkdir(parents=True)
    (root / "21082026").mkdir(parents=True)
    f1 = root / "27082026" / "a.txt"
    f1.write_bytes(b"x")
    f2 = root / "21082026" / "a.txt"
    f2.write_bytes(b"x")

    analysis = analyze_provenance(
        _write_d1_report(tmp_path, root=root, exact_duplicate_groups=[_group([f1, f2])])
    )
    group = analysis.exact_duplicate_provenance[0]

    assert group.inference_code == INFERENCE_DIVERGENT_DATE_SIGNAL
    assert group.confidence == "medium"
    assert not group.requires_human_review
    assert group.structured_facts.distinct_date_tokens == ["2026-08-21", "2026-08-27"]


# --- classification: partial date/keyword signal, now LOW confidence --


def test_classifies_one_dated_one_undated_as_partial_signal_low_confidence(
    tmp_path: Path,
) -> None:
    """The specific case this review targeted: one dated copy + one
    undated copy must NOT be presented as a confident current-vs-
    historical conclusion. Confidence must be low and review required."""
    root = tmp_path / "corpus"
    (root / "27082026").mkdir(parents=True)
    (root / "current").mkdir(parents=True)
    f1 = root / "27082026" / "a.txt"
    f1.write_bytes(b"x")
    f2 = root / "current" / "a.txt"
    f2.write_bytes(b"x")

    analysis = analyze_provenance(
        _write_d1_report(tmp_path, root=root, exact_duplicate_groups=[_group([f1, f2])])
    )
    group = analysis.exact_duplicate_provenance[0]

    assert group.inference_code == INFERENCE_PARTIAL_DATE_OR_KEYWORD_SIGNAL
    assert group.confidence == "low"
    assert group.requires_human_review is True


def test_classifies_keyword_plus_depth_as_partial_signal(tmp_path: Path) -> None:
    root = tmp_path / "corpus"
    (root / "Archive").mkdir(parents=True)
    f1 = root / "Archive" / "b.txt"
    f1.write_bytes(b"x")
    f2 = root / "b.txt"
    f2.write_bytes(b"x")

    analysis = analyze_provenance(
        _write_d1_report(tmp_path, root=root, exact_duplicate_groups=[_group([f1, f2])])
    )
    group = analysis.exact_duplicate_provenance[0]

    assert group.inference_code == INFERENCE_PARTIAL_DATE_OR_KEYWORD_SIGNAL
    assert group.confidence == "low"
    assert group.requires_human_review is True


# --- classification: uniform keyword, no further distinguishing signal


def test_classifies_shared_keyword_no_depth_difference_as_uniform_signal(
    tmp_path: Path,
) -> None:
    root = tmp_path / "corpus"
    (root / "Backup1").mkdir(parents=True)
    (root / "Backup2").mkdir(parents=True)
    f1 = root / "Backup1" / "a.txt"
    f1.write_bytes(b"x")
    f2 = root / "Backup2" / "a.txt"
    f2.write_bytes(b"x")

    analysis = analyze_provenance(
        _write_d1_report(tmp_path, root=root, exact_duplicate_groups=[_group([f1, f2])])
    )
    group = analysis.exact_duplicate_provenance[0]

    assert group.inference_code == INFERENCE_UNIFORM_KEYWORD_NO_FURTHER_SIGNAL
    assert group.confidence == "low"
    assert group.requires_human_review


# --- classification: no signal at all -----------------------------------


def test_classifies_no_signal_as_no_provenance_signal(tmp_path: Path) -> None:
    root = tmp_path / "corpus"
    (root / "project_a").mkdir(parents=True)
    (root / "project_b").mkdir(parents=True)
    f1 = root / "project_a" / "template.yaml"
    f1.write_bytes(b"shared")
    f2 = root / "project_b" / "template.yaml"
    f2.write_bytes(b"shared")

    analysis = analyze_provenance(
        _write_d1_report(tmp_path, root=root, exact_duplicate_groups=[_group([f1, f2])])
    )
    group = analysis.exact_duplicate_provenance[0]

    assert group.inference_code == INFERENCE_NO_PROVENANCE_SIGNAL
    assert group.confidence == "low"
    assert group.requires_human_review


def test_no_provenance_signal_prose_does_not_imply_safe_to_remove() -> None:
    prose = INFERENCE_PROSE[INFERENCE_NO_PROVENANCE_SIGNAL]
    assert "safe to remove" not in prose.lower()
    assert "safe to delete" not in prose.lower()
    assert "not a safety conclusion" in prose.lower()


# --- ambiguous / insufficient evidence: live-corpus divergence --------


def test_handles_path_that_no_longer_exists(tmp_path: Path) -> None:
    root = tmp_path / "corpus"
    root.mkdir()
    f1 = root / "still_here.txt"
    f1.write_bytes(b"x")
    vanished = root / "vanished.txt"

    analysis = analyze_provenance(
        _write_d1_report(
            tmp_path, root=root, exact_duplicate_groups=[_group([f1, vanished])]
        )
    )
    group = analysis.exact_duplicate_provenance[0]

    assert group.inference_code == INFERENCE_STALE_REFERENCE_UNVERIFIABLE
    assert group.requires_human_review
    assert analysis.paths_no_longer_existing == 1
    assert group.structured_facts.paths_missing == 1


def test_handles_all_paths_vanished(tmp_path: Path) -> None:
    root = tmp_path / "corpus"
    root.mkdir()
    analysis = analyze_provenance(
        _write_d1_report(
            tmp_path,
            root=root,
            exact_duplicate_groups=[_group([root / "gone1.txt", root / "gone2.txt"])],
        )
    )
    group = analysis.exact_duplicate_provenance[0]
    assert group.inference_code == INFERENCE_STALE_REFERENCE_UNVERIFIABLE
    assert analysis.paths_no_longer_existing == 2


# --- category preservation: exact / cryptomator / directory kept apart


def test_cryptomator_groups_kept_separate_from_exact_duplicates(tmp_path: Path) -> None:
    root = tmp_path / "corpus"
    root.mkdir()
    f1 = root / "chunk1.c9r"
    f1.write_bytes(b"x")
    f2 = root / "chunk2.c9r"
    f2.write_bytes(b"x")

    analysis = analyze_provenance(
        _write_d1_report(
            tmp_path,
            root=root,
            cryptomator_chunk_duplicate_groups=[_group([f1, f2], content_hash="chash")],
        )
    )

    assert analysis.exact_duplicate_provenance == []
    assert len(analysis.cryptomator_chunk_provenance) == 1
    assert analysis.cryptomator_chunk_provenance[0].group_kind == "cryptomator_chunk_duplicate"


def test_report_json_labels_cryptomator_section_as_ciphertext_scoped(
    tmp_path: Path,
) -> None:
    """Ensure the report itself, not just prose elsewhere, states that
    .c9r matches are ciphertext-chunk matches, not plaintext document
    matches - required so a reader of the JSON alone (not just the
    architecture doc) sees this scoping."""
    root = tmp_path / "corpus"
    root.mkdir()
    analysis = analyze_provenance(_write_d1_report(tmp_path, root=root))
    data = analysis.to_json_dict()

    assert "cryptomator_chunk_provenance_note" in data
    note = data["cryptomator_chunk_provenance_note"].lower()
    assert "ciphertext" in note
    assert "never merge" in note or "not the same" in note or "does not" in note


def test_directory_duplicate_groups_get_own_provenance_classification(
    tmp_path: Path,
) -> None:
    root = tmp_path / "corpus"
    d1_dir = root / "27082026" / "phone"
    d2_dir = root / "phone"
    d1_dir.mkdir(parents=True)
    d2_dir.mkdir(parents=True)
    (d1_dir / "photo.jpg").write_bytes(b"x")
    (d2_dir / "photo.jpg").write_bytes(b"x")

    analysis = analyze_provenance(
        _write_d1_report(
            tmp_path,
            root=root,
            directory_duplicate_groups=[
                {
                    "signature": "sig1",
                    "paths": [str(d1_dir), str(d2_dir)],
                    "total_size_bytes": 1,
                    "file_count": 1,
                    "reclaimable_bytes": 1,
                }
            ],
        )
    )

    assert len(analysis.directory_duplicate_provenance) == 1
    group = analysis.directory_duplicate_provenance[0]
    assert group.group_kind == "directory_duplicate"
    assert group.inference_code == INFERENCE_PARTIAL_DATE_OR_KEYWORD_SIGNAL


# --- overlap detection --------------------------------------------------


def test_detects_overlap_between_file_and_directory_duplicate_groups(
    tmp_path: Path,
) -> None:
    root = tmp_path / "corpus"
    d1_dir = root / "27082026" / "phone"
    d2_dir = root / "phone"
    d1_dir.mkdir(parents=True)
    d2_dir.mkdir(parents=True)
    f1 = d1_dir / "photo.jpg"
    f2 = d2_dir / "photo.jpg"
    f1.write_bytes(b"x")
    f2.write_bytes(b"x")

    analysis = analyze_provenance(
        _write_d1_report(
            tmp_path,
            root=root,
            exact_duplicate_groups=[_group([f1, f2])],
            directory_duplicate_groups=[
                {
                    "signature": "sig1",
                    "paths": [str(d1_dir), str(d2_dir)],
                    "total_size_bytes": 1,
                    "file_count": 1,
                    "reclaimable_bytes": 1,
                }
            ],
        )
    )

    assert analysis.exact_duplicate_provenance[0].overlaps_directory_group == "sig1"


def test_no_overlap_when_file_group_not_inside_any_directory_group(
    tmp_path: Path,
) -> None:
    root = tmp_path / "corpus"
    root.mkdir()
    f1 = root / "a.txt"
    f2 = root / "b.txt"
    f1.write_bytes(b"x")
    f2.write_bytes(b"x")

    analysis = analyze_provenance(
        _write_d1_report(tmp_path, root=root, exact_duplicate_groups=[_group([f1, f2])])
    )
    assert analysis.exact_duplicate_provenance[0].overlaps_directory_group is None


# --- aggregate counts ----------------------------------------------------


def test_inference_and_confidence_counts_are_aggregated(tmp_path: Path) -> None:
    root = tmp_path / "corpus"
    root.mkdir()
    f1 = root / "a.txt"
    f2 = root / "b.txt"
    f1.write_bytes(b"x")
    f2.write_bytes(b"x")

    analysis = analyze_provenance(
        _write_d1_report(tmp_path, root=root, exact_duplicate_groups=[_group([f1, f2])])
    )
    assert sum(analysis.inference_counts.values()) == 1
    assert sum(analysis.confidence_counts.values()) == 1


# --- compact schema: no repeated prose stored per group ------------------


def test_group_records_store_codes_not_repeated_prose(tmp_path: Path) -> None:
    root = tmp_path / "corpus"
    root.mkdir()
    f1 = root / "a.txt"
    f2 = root / "b.txt"
    f1.write_bytes(b"x")
    f2.write_bytes(b"x")

    analysis = analyze_provenance(
        _write_d1_report(tmp_path, root=root, exact_duplicate_groups=[_group([f1, f2])])
    )
    data = analysis.to_json_dict()
    group_json = data["exact_duplicate_provenance"][0]

    # The compact per-group record carries a short code, not the full
    # prose paragraph - prose lives only in INFERENCE_PROSE, rendered
    # on demand by report generators, never duplicated per group.
    assert group_json["inference_code"] == INFERENCE_NO_PROVENANCE_SIGNAL
    assert "inference" not in group_json  # old key name must be gone
    assert len(group_json["inference_code"]) < 50


def test_path_signals_do_not_serialize_mtime_ctime(tmp_path: Path) -> None:
    """Report-size fix: mtime/ctime are not used by classification and
    were a major contributor to the original 498MB report size -
    confirm they are no longer part of the compact per-group record."""
    root = tmp_path / "corpus"
    root.mkdir()
    f1 = root / "a.txt"
    f2 = root / "b.txt"
    f1.write_bytes(b"x")
    f2.write_bytes(b"x")

    analysis = analyze_provenance(
        _write_d1_report(tmp_path, root=root, exact_duplicate_groups=[_group([f1, f2])])
    )
    data = analysis.to_json_dict()
    group_json = data["exact_duplicate_provenance"][0]

    assert "mtime" not in json.dumps(group_json)
    assert "ctime" not in json.dumps(group_json)
    # Paths are now bare strings, not nested per-path objects.
    assert all(isinstance(p, str) for p in group_json["paths"])


# --- two-stage pipeline: classify is reusable without re-touching fs ----


def test_classify_is_a_pure_function_reusable_without_filesystem_access(
    tmp_path: Path,
) -> None:
    root = tmp_path / "corpus"
    root.mkdir()
    f1 = root / "a.txt"
    f2 = root / "b.txt"
    f1.write_bytes(b"x")
    f2.write_bytes(b"x")

    raw = collect_path_signals(
        _write_d1_report(tmp_path, root=root, exact_duplicate_groups=[_group([f1, f2])])
    )
    # Delete the files - classify() must not need the filesystem again.
    f1.unlink()
    f2.unlink()

    analysis = classify(raw)
    assert len(analysis.exact_duplicate_provenance) == 1


def _write_old_style_report(tmp_path: Path, root: Path, path_records: list[dict]) -> Path:
    """Builds a report matching the PRE-review schema (full per-path
    signal objects nested under "paths"), exactly as the real,
    already-completed T7 run originally produced - this is the actual
    shape `load_raw_from_previous_report` needs to reconstruct from."""
    old_report = {
        "d1_root": str(root),
        "d1_analyzed_at": "2026-09-13T03:37:04+00:00",
        "d2_analyzed_at": "2026-09-13T05:27:16+00:00",
        "exact_duplicate_provenance": [
            {
                "group_kind": "exact_duplicate",
                "group_key": "hash1",
                "copies": len(path_records),
                "reclaimable_bytes": 1,
                "paths": path_records,
            }
        ],
        "cryptomator_chunk_provenance": [],
        "directory_duplicate_provenance": [],
    }
    report_path = tmp_path / "old_style.json"
    report_path.write_text(json.dumps(old_report))
    return report_path


def test_load_raw_from_previous_report_reconstructs_without_filesystem_access(
    tmp_path: Path,
) -> None:
    """The core capability this review pass needed: reclassifying an
    already-completed real report (in its ORIGINAL, pre-review schema
    with full per-path signal objects, exactly like the real T7 D2
    output) after a naming/schema correction, with zero new T7 reads."""
    root = tmp_path / "corpus"
    root.mkdir()
    old_style_path = _write_old_style_report(
        tmp_path,
        root,
        [
            {
                "path": str(root / "Archive" / "a.txt"),
                "exists": True,
                "depth": 2,
                "date_tokens": [],
                "historical_keywords_found": ["archive"],
                "current_keywords_found": [],
            },
            {
                "path": str(root / "a.txt"),
                "exists": True,
                "depth": 1,
                "date_tokens": [],
                "historical_keywords_found": [],
                "current_keywords_found": [],
            },
        ],
    )
    d1_path = _write_d1_report(tmp_path, root=root, exact_duplicate_groups=[])

    # No filesystem access happens here at all - the source files
    # referenced by the old report are never even created on disk.
    raw = load_raw_from_previous_report(old_style_path, d1_path)
    analysis = classify(raw)

    assert len(analysis.exact_duplicate_provenance) == 1
    assert (
        analysis.exact_duplicate_provenance[0].inference_code
        == INFERENCE_PARTIAL_DATE_OR_KEYWORD_SIGNAL
    )


def test_load_raw_from_previous_report_rejects_already_compact_schema(
    tmp_path: Path,
) -> None:
    """A report that already uses the compact (bare path string)
    schema has nothing left to reconstruct - must raise clearly rather
    than silently produce an empty/wrong analysis."""
    root = tmp_path / "corpus"
    root.mkdir()
    f1 = root / "a.txt"
    f2 = root / "b.txt"
    f1.write_bytes(b"x")
    f2.write_bytes(b"x")

    d1_path = _write_d1_report(tmp_path, root=root, exact_duplicate_groups=[_group([f1, f2])])
    analysis = analyze_provenance(d1_path)
    report_path = tmp_path / "compact.json"
    write_provenance_report(analysis, report_path)

    with pytest.raises(ValueError, match="compact schema"):
        load_raw_from_previous_report(report_path, d1_path)


# --- write_provenance_report safety ---------------------------------------


def test_write_provenance_report_rejects_destination_inside_root(
    tmp_path: Path,
) -> None:
    root = tmp_path / "corpus"
    root.mkdir()
    analysis = analyze_provenance(_write_d1_report(tmp_path, root=root))

    with pytest.raises(ValueError, match="scanned root"):
        write_provenance_report(analysis, root / "report.json")


def test_write_provenance_report_accepts_legitimate_destination(
    tmp_path: Path,
) -> None:
    root = tmp_path / "corpus"
    root.mkdir()
    analysis = analyze_provenance(_write_d1_report(tmp_path, root=root))

    destination = tmp_path / "reports" / "provenance.json"
    written = write_provenance_report(analysis, destination)

    assert written == destination
    data = json.loads(destination.read_text())
    assert "_read_this_first" in data
    assert "not a deletion recommendation" in data["_read_this_first"]


def test_report_disclaimer_states_live_corpus_caveat_precisely(tmp_path: Path) -> None:
    """Verify the exact wording requested: paths being observable
    during D2 must not be presented as proof the corpus was
    unchanged."""
    root = tmp_path / "corpus"
    root.mkdir()
    analysis = analyze_provenance(_write_d1_report(tmp_path, root=root))
    disclaimer = analysis.to_json_dict()["_read_this_first"]

    assert "does NOT prove the corpus was unchanged" in disclaimer
    assert "Syncthing" in disclaimer
    assert "two hours" in disclaimer or "separate pass" in disclaimer


def test_report_disclaimer_states_low_confidence_is_not_a_disposal_signal(
    tmp_path: Path,
) -> None:
    root = tmp_path / "corpus"
    root.mkdir()
    analysis = analyze_provenance(_write_d1_report(tmp_path, root=root))
    disclaimer = analysis.to_json_dict()["_read_this_first"]

    assert "NOT" in disclaimer
    assert "likely safe to remove" in disclaimer.lower() or "disposal" in disclaimer.lower()
