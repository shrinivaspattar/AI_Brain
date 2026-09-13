"""T7 Provenance-Aware Duplicate Analysis (Phase D2) - read-only,
exactly as authorized: analyzing D1's duplicate-analysis output, path
text, directory structure, and filesystem metadata to understand WHY
duplicate groups exist. This module NEVER deletes, moves, renames,
extracts, quarantines, mutates anything, invokes
`DedupFilesystemExecutor`, creates an authorization, or automatically
designates any file as "the one to keep" or "the one to delete" - it
only observes and reports interpretations.

**Pipeline deliberately split into two independent stages**, so the
expensive, T7-touching part never needs to be redone just because the
classification logic changes:

1. `collect_path_signals(d1_report_path) -> RawProvenanceData` - the
   ONLY stage that touches the filesystem. Reads D1's already-
   established `duplicate_analysis.json` and performs a targeted,
   read-only `os.lstat` on exactly the paths D1 already named (never a
   fresh directory walk, never content hashing).
2. `classify(raw_data) -> ProvenanceAnalysis` - a PURE function over
   already-collected data. Reclassifying after a heuristic change, a
   naming correction, or a schema change requires only this stage -
   never a second pass over the T7. `analyze_provenance()` below is a
   convenience wrapper that runs both stages back to back for a fresh
   D1 report; `load_raw_from_previous_report()` + `classify()`
   re-derives a corrected analysis from an ALREADY-COLLECTED D2
   report's own raw per-path data, with zero filesystem access.

**Classification codes are evidence-pattern labels, not verified
conclusions** - a naming correction made during review (see
`AI_Brain_Architecture.md`'s D2 section): earlier code used names like
`current_plus_historical`, which reads as an established fact about
WHICH copy is current. It is not - "one copy's path lacks a dating
convention" does not prove that copy is in active use; it may simply
never have been placed in a dated folder. Every `inference_code` below
is named to describe the EVIDENCE PATTERN that fired, and the
human-readable prose explaining what that pattern MIGHT mean is
generated separately (via `INFERENCE_PROSE`), so the compact,
machine-readable per-group record never itself asserts more than the
pattern it observed. The required distinction - OBSERVED FACT ->
INFERENCE -> CONFIDENCE - is preserved as three separate, explicit
fields (`structured_facts`, `inference_code` + rendered prose,
`confidence`), never collapsed into one label.
"""

from __future__ import annotations

import json
import os
import re
from dataclasses import asdict, dataclass, field
from datetime import UTC, date, datetime
from pathlib import Path

from app.discovery.safety import reject_destination_inside_root

HISTORICAL_KEYWORDS = frozenset(
    {
        "archive", "backup", "as a copy of", "copy of", "old",
        "_duplicates_quarantine", ".trash", "sync-conflict", "takeout-",
    }
)
CURRENT_KEYWORDS = frozenset({"current", "latest", "working"})

_DATE_8DIGIT_RE = re.compile(r"(?<!\d)(\d{8})(?!\d)")
_DATE_DASHED_RE = re.compile(r"(?<!\d)(\d{2})-(\d{2})-(\d{4})(?!\d)")
_YEAR_RANGE = range(2000, 2036)

# --- Inference codes: neutral names for the EVIDENCE PATTERN that
# fired, never asserting the interpretation as settled. Human-readable
# prose is rendered separately from INFERENCE_PROSE, never stored
# per-group (see module docstring and the report-size fix this
# addresses).
INFERENCE_DIVERGENT_DATE_SIGNAL = "DIVERGENT_DATE_SIGNAL"
INFERENCE_PARTIAL_DATE_OR_KEYWORD_SIGNAL = "PARTIAL_DATE_OR_KEYWORD_SIGNAL"
INFERENCE_UNIFORM_KEYWORD_NO_FURTHER_SIGNAL = "UNIFORM_KEYWORD_NO_FURTHER_SIGNAL"
INFERENCE_NO_PROVENANCE_SIGNAL = "NO_PROVENANCE_SIGNAL"
INFERENCE_STALE_REFERENCE_UNVERIFIABLE = "STALE_REFERENCE_UNVERIFIABLE"

INFERENCE_PROSE: dict[str, str] = {
    INFERENCE_DIVERGENT_DATE_SIGNAL: (
        "Different copies' paths contain different date-like tokens (OBSERVED "
        "FACT). This is CONSISTENT WITH the copies originating from different "
        "backup/export events taken on different dates, but the tool cannot "
        "verify that these tokens actually represent backup dates rather than "
        "coincidental numbers - treat as a signal to investigate, not a "
        "verified timeline."
    ),
    INFERENCE_PARTIAL_DATE_OR_KEYWORD_SIGNAL: (
        "At least one copy's path carries a date-like token and/or a backup-"
        "sounding keyword, while at least one other copy's path carries "
        "neither (OBSERVED FACT). This pattern is CONSISTENT WITH one copy "
        "being a dated/archived snapshot and another being an undated "
        "working copy - but the absence of a date or keyword in a path is "
        "NOT proof that a file is currently in active use; it may simply "
        "never have been moved into a dated or archival location, or the "
        "naming convention may be inconsistent. This does not establish "
        "which copy, if any, is 'current' - it flags an asymmetry worth a "
        "human looking at."
    ),
    INFERENCE_UNIFORM_KEYWORD_NO_FURTHER_SIGNAL: (
        "A backup-sounding keyword appears in this group's paths (OBSERVED "
        "FACT), but nothing else (date tokens, path depth) distinguishes the "
        "copies further. This MAY mean the copies are multiple artifacts of "
        "effectively the same backup sweep or export, but the tool has no "
        "independent way to confirm they were captured together."
    ),
    INFERENCE_NO_PROVENANCE_SIGNAL: (
        "No date token and no backup/current keyword was found anywhere in "
        "this group's paths (OBSERVED FACT). This MAY mean the duplicate is "
        "coincidental (e.g. a shared template, installer, or boilerplate "
        "file that legitimately exists independently in multiple places) "
        "rather than a backup relationship - but absence of a signal is weak "
        "evidence at best, not a safety conclusion of any kind."
    ),
    INFERENCE_STALE_REFERENCE_UNVERIFIABLE: (
        "Fewer than two of this group's D1-named paths still existed when "
        "D2 checked them (OBSERVED FACT). D1's duplicate finding cannot be "
        "corroborated against the corpus's current state, so no provenance "
        "interpretation is offered."
    ),
}

# Confidence deliberately reflects how many independent signals agree,
# not certainty about the underlying interpretation - see review notes
# in AI_Brain_Architecture.md's D2 section for why
# PARTIAL_DATE_OR_KEYWORD_SIGNAL was downgraded from its original
# "medium" to "low" (an asymmetry in dating/keyword conventions is a
# single, weak signal, not the stronger two-concrete-dates evidence
# DIVERGENT_DATE_SIGNAL has).
INFERENCE_CONFIDENCE: dict[str, str] = {
    INFERENCE_DIVERGENT_DATE_SIGNAL: "medium",
    INFERENCE_PARTIAL_DATE_OR_KEYWORD_SIGNAL: "low",
    INFERENCE_UNIFORM_KEYWORD_NO_FURTHER_SIGNAL: "low",
    INFERENCE_NO_PROVENANCE_SIGNAL: "low",
    INFERENCE_STALE_REFERENCE_UNVERIFIABLE: "low",
}
# Every inference code requires human review before any future
# disposition decision except the one case with comparatively the
# strongest available signal (two independently-plausible, differing
# calendar dates) - even that one is "requires_human_review=False" only
# in the sense of "not flagged as LOW-signal," never as "safe to act on
# without review": see the module- and report-level disclaimers.
_REQUIRES_REVIEW: dict[str, bool] = {
    INFERENCE_DIVERGENT_DATE_SIGNAL: False,
    INFERENCE_PARTIAL_DATE_OR_KEYWORD_SIGNAL: True,
    INFERENCE_UNIFORM_KEYWORD_NO_FURTHER_SIGNAL: True,
    INFERENCE_NO_PROVENANCE_SIGNAL: True,
    INFERENCE_STALE_REFERENCE_UNVERIFIABLE: True,
}


@dataclass(frozen=True, slots=True)
class DateToken:
    raw_text: str
    parsed_date: str


@dataclass(frozen=True, slots=True)
class PathSignals:
    """OBSERVED FACTS about one path - nothing here is an inference.
    Deliberately does NOT include mtime/ctime: collected internally by
    `_collect_path_signals` but not part of this module's actual
    classification logic (Syncthing's rewriting of timestamps on sync
    makes cross-copy mtime comparison unreliable on this corpus - see
    AI_Brain_Architecture.md's D2 section) and excluded here to keep
    the compact report's per-path records small; timestamps remain
    available on-disk via a fresh `stat` on any specific path a human
    wants to inspect further."""

    path: str
    exists: bool
    depth: int
    date_tokens: list[DateToken] = field(default_factory=list)
    historical_keywords_found: list[str] = field(default_factory=list)
    current_keywords_found: list[str] = field(default_factory=list)


@dataclass(frozen=True, slots=True)
class StructuredFacts:
    """Group-level OBSERVED FACTS, aggregated from each path's
    `PathSignals` - the compact alternative to repeating natural-
    language sentences per group."""

    paths_existing: int
    paths_missing: int
    distinct_date_tokens: list[str]
    paths_with_date_tokens: int
    paths_without_date_tokens: int
    paths_with_historical_keyword: int
    paths_with_current_keyword: int
    depth_min: int | None
    depth_max: int | None


@dataclass(frozen=True, slots=True)
class RawGroup:
    """One D1 group plus its collected `PathSignals` - the output of
    stage 1 (`collect_path_signals`), and the sole input stage 2
    (`classify`) needs. Never itself touches the filesystem again."""

    group_kind: str  # "exact_duplicate" | "cryptomator_chunk_duplicate" | "directory_duplicate"
    group_key: str
    copies: int
    reclaimable_bytes: int
    paths: list[PathSignals]


@dataclass(frozen=True, slots=True)
class RawProvenanceData:
    d1_root: str
    d1_analyzed_at: str
    collected_at: str
    groups: list[RawGroup]
    directory_groups_by_dir: dict[str, str]  # directory path -> D1 signature, for overlap detection


@dataclass(frozen=True, slots=True)
class GroupProvenance:
    group_kind: str
    group_key: str
    copies: int
    reclaimable_bytes: int
    paths: list[str]
    structured_facts: StructuredFacts
    evidence_codes: list[str]
    inference_code: str
    confidence: str
    requires_human_review: bool
    overlaps_directory_group: str | None = None


@dataclass(slots=True)
class ProvenanceAnalysis:
    d1_root: str
    d1_analyzed_at: str
    d2_analyzed_at: str
    exact_duplicate_provenance: list[GroupProvenance] = field(default_factory=list)
    cryptomator_chunk_provenance: list[GroupProvenance] = field(default_factory=list)
    directory_duplicate_provenance: list[GroupProvenance] = field(default_factory=list)
    inference_counts: dict[str, int] = field(default_factory=dict)
    confidence_counts: dict[str, int] = field(default_factory=dict)
    paths_no_longer_existing: int = 0

    def to_json_dict(self) -> dict:
        def group_dict(g: GroupProvenance) -> dict:
            return asdict(g)

        return {
            "_read_this_first": (
                "D2 is read-only forensic INTERPRETATION, not a deletion "
                "recommendation. `inference_code` names an EVIDENCE PATTERN, "
                "not a verified conclusion - render human-readable prose via "
                "app.discovery.provenance_analysis.INFERENCE_PROSE, or read "
                "the accompanying summary text file. `confidence` reflects "
                "how many independent signals agree, not certainty. "
                "'All D1-referenced paths were observable during D2' "
                "(paths_no_longer_existing=0 in this run) does NOT prove the "
                "corpus was unchanged between D1 and D2 - D2's own reads "
                "happened in a separate pass, roughly two hours after D1, "
                "while Syncthing was independently, continuously active; a "
                "file's CONTENT could have changed without its mere "
                "existence changing. Groups with confidence='low' or "
                "requires_human_review=true require independent human "
                "provenance review before any future decision - they are "
                "NOT 'likely safe to remove' candidates, and nothing in this "
                "report should be read as a disposal recommendation for any "
                "group at any confidence level."
            ),
            "d1_root": self.d1_root,
            "d1_analyzed_at": self.d1_analyzed_at,
            "d2_analyzed_at": self.d2_analyzed_at,
            "inference_counts": self.inference_counts,
            "confidence_counts": self.confidence_counts,
            "paths_no_longer_existing": self.paths_no_longer_existing,
            "exact_duplicate_provenance_count": len(self.exact_duplicate_provenance),
            "exact_duplicate_provenance": [group_dict(g) for g in self.exact_duplicate_provenance],
            "cryptomator_chunk_provenance_count": len(self.cryptomator_chunk_provenance),
            "cryptomator_chunk_provenance_note": (
                "Cryptomator .c9r groups are ENCRYPTED CHUNK matches, not "
                "plaintext document matches - a shared inference_code here "
                "means the CIPHERTEXT chunks are identical, which does NOT "
                "carry the same meaning as a plaintext exact-duplicate match "
                "(e.g. two identical .jpg files). Never merge these with "
                "exact_duplicate_provenance when reasoning about findings."
            ),
            "cryptomator_chunk_provenance": [group_dict(g) for g in self.cryptomator_chunk_provenance],
            "directory_duplicate_provenance_count": len(self.directory_duplicate_provenance),
            "directory_duplicate_provenance": [group_dict(g) for g in self.directory_duplicate_provenance],
        }


def _extract_date_tokens(path: str) -> list[DateToken]:
    tokens: list[DateToken] = []
    for match in _DATE_8DIGIT_RE.finditer(path):
        raw = match.group(1)
        parsed = _try_parse_ddmmyyyy(raw) or _try_parse_yyyymmdd(raw)
        if parsed is not None:
            tokens.append(DateToken(raw_text=raw, parsed_date=parsed.isoformat()))
    for match in _DATE_DASHED_RE.finditer(path):
        day, month, year = match.groups()
        parsed = _valid_date(int(year), int(month), int(day))
        if parsed is not None:
            tokens.append(DateToken(raw_text=match.group(0), parsed_date=parsed.isoformat()))
    return tokens


def _try_parse_ddmmyyyy(raw: str) -> date | None:
    return _valid_date(int(raw[4:8]), int(raw[2:4]), int(raw[0:2]))


def _try_parse_yyyymmdd(raw: str) -> date | None:
    return _valid_date(int(raw[0:4]), int(raw[4:6]), int(raw[6:8]))


def _valid_date(year: int, month: int, day: int) -> date | None:
    if year not in _YEAR_RANGE:
        return None
    try:
        return date(year, month, day)
    except ValueError:
        return None


def _find_keywords(path: str, keywords: frozenset[str]) -> list[str]:
    lowered = path.lower()
    return sorted(k for k in keywords if k in lowered)


def _collect_path_signals(path: str, root: str) -> PathSignals:
    path_obj = Path(path)
    try:
        depth = len(path_obj.relative_to(root).parts)
    except ValueError:
        depth = len(path_obj.parts)

    try:
        os.lstat(path)
        exists = True
    except OSError:
        exists = False

    return PathSignals(
        path=path,
        exists=exists,
        depth=depth,
        date_tokens=_extract_date_tokens(path),
        historical_keywords_found=_find_keywords(path, HISTORICAL_KEYWORDS),
        current_keywords_found=_find_keywords(path, CURRENT_KEYWORDS),
    )


def collect_path_signals(d1_report_path: Path) -> RawProvenanceData:
    """STAGE 1 - the only stage that touches the filesystem. Loads
    D1's `duplicate_analysis.json` and performs one targeted, read-only
    `os.lstat` per path D1 already named (existence only - never
    content, never a fresh directory walk)."""
    d1 = json.loads(Path(d1_report_path).read_text())
    root = d1["root"]

    directory_groups_by_dir: dict[str, str] = {
        path: group["signature"]
        for group in d1.get("directory_duplicate_groups", [])
        for path in group["paths"]
    }

    def collect_file_groups(groups: list[dict], kind: str) -> list[RawGroup]:
        return [
            RawGroup(
                group_kind=kind,
                group_key=group["content_hash"],
                copies=group["copies"],
                reclaimable_bytes=group["reclaimable_bytes"],
                paths=[_collect_path_signals(p, root) for p in group["paths"]],
            )
            for group in groups
        ]

    groups = collect_file_groups(d1.get("exact_duplicate_groups", []), "exact_duplicate")
    groups += collect_file_groups(
        d1.get("cryptomator_chunk_duplicate_groups", []), "cryptomator_chunk_duplicate"
    )
    groups += [
        RawGroup(
            group_kind="directory_duplicate",
            group_key=group["signature"],
            copies=len(group["paths"]),
            reclaimable_bytes=group["reclaimable_bytes"],
            paths=[_collect_path_signals(p, root) for p in group["paths"]],
        )
        for group in d1.get("directory_duplicate_groups", [])
    ]

    return RawProvenanceData(
        d1_root=root,
        d1_analyzed_at=d1["analyzed_at"],
        collected_at=datetime.now(UTC).isoformat(),
        groups=groups,
        directory_groups_by_dir=directory_groups_by_dir,
    )


def load_raw_from_previous_report(
    previous_report_path: Path, d1_report_path: Path
) -> RawProvenanceData:
    """Reconstruct `RawProvenanceData` from an ALREADY-COMPLETED D2
    report's own JSON, plus D1's report (needed only for the
    directory-duplicate-group lookup used in overlap detection - a
    plain read of an already-written report, not a filesystem access).
    Performs ZERO filesystem access of its own: every fact this
    function needs (path, exists, depth, date tokens, keywords) is
    already present in the previous report's own per-path records,
    whether that report used the current schema or the earlier one
    (`historical_keywords_found`/`current_keywords_found`/`date_
    tokens`/`depth`/`exists` are read the same way in both). Exists
    specifically so a classification-logic correction can be re-run
    against real, already-collected T7 data without a second read
    pass over the drive."""
    previous = json.loads(Path(previous_report_path).read_text())
    d1 = json.loads(Path(d1_report_path).read_text())

    directory_groups_by_dir: dict[str, str] = {
        path: group["signature"]
        for group in d1.get("directory_duplicate_groups", [])
        for path in group["paths"]
    }

    def reconstruct_path_signals(raw_path: dict) -> PathSignals:
        return PathSignals(
            path=raw_path["path"],
            exists=raw_path["exists"],
            depth=raw_path["depth"],
            date_tokens=[
                DateToken(raw_text=t["raw_text"], parsed_date=t["parsed_date"])
                for t in raw_path.get("date_tokens", [])
            ],
            historical_keywords_found=list(raw_path.get("historical_keywords_found", [])),
            current_keywords_found=list(raw_path.get("current_keywords_found", [])),
        )

    def reconstruct_groups(key: str) -> list[RawGroup]:
        groups = []
        for g in previous.get(key, []):
            # Earlier schema nested full PathSignals dicts under
            # "paths"; if this report already used the compact schema
            # (bare path strings), there is nothing left to reconstruct
            # from - the caller should collect fresh in that case.
            raw_paths = g["paths"]
            if raw_paths and isinstance(raw_paths[0], str):
                raise ValueError(
                    f"{previous_report_path} already uses the compact schema "
                    "(bare path strings, no per-path signal detail) - there "
                    "is nothing to reconstruct; use collect_path_signals() "
                    "against the D1 report directly instead"
                )
            groups.append(
                RawGroup(
                    group_kind=g["group_kind"],
                    group_key=g["group_key"],
                    copies=g["copies"],
                    reclaimable_bytes=g["reclaimable_bytes"],
                    paths=[reconstruct_path_signals(p) for p in raw_paths],
                )
            )
        return groups

    all_groups = (
        reconstruct_groups("exact_duplicate_provenance")
        + reconstruct_groups("cryptomator_chunk_provenance")
        + reconstruct_groups("directory_duplicate_provenance")
    )

    return RawProvenanceData(
        d1_root=previous["d1_root"],
        d1_analyzed_at=previous["d1_analyzed_at"],
        collected_at=previous["d2_analyzed_at"],
        groups=all_groups,
        directory_groups_by_dir=directory_groups_by_dir,
    )


def _build_structured_facts(paths: list[PathSignals]) -> StructuredFacts:
    existing = [p for p in paths if p.exists]
    depths = [p.depth for p in existing]
    return StructuredFacts(
        paths_existing=len(existing),
        paths_missing=len(paths) - len(existing),
        distinct_date_tokens=sorted({t.parsed_date for p in existing for t in p.date_tokens}),
        paths_with_date_tokens=sum(1 for p in existing if p.date_tokens),
        paths_without_date_tokens=sum(1 for p in existing if not p.date_tokens),
        paths_with_historical_keyword=sum(1 for p in existing if p.historical_keywords_found),
        paths_with_current_keyword=sum(1 for p in existing if p.current_keywords_found),
        depth_min=min(depths) if depths else None,
        depth_max=max(depths) if depths else None,
    )


def _classify_group(paths: list[PathSignals]) -> tuple[str, list[str]]:
    """Returns (inference_code, evidence_codes). Confidence and
    requires_human_review are derived from inference_code via the
    module-level lookup tables, never computed ad hoc per group."""
    existing = [p for p in paths if p.exists]
    if len(existing) < 2:
        return INFERENCE_STALE_REFERENCE_UNVERIFIABLE, ["PATHS_MISSING_IN_D2"]

    distinct_dates = sorted({t.parsed_date for p in existing for t in p.date_tokens})
    paths_without_dates = [p for p in existing if not p.date_tokens]
    any_historical_keyword = any(p.historical_keywords_found for p in existing)
    depths = [p.depth for p in existing]
    depth_spread = max(depths) - min(depths) if depths else 0
    shallowest = min(existing, key=lambda p: p.depth)

    if len(distinct_dates) >= 2:
        return INFERENCE_DIVERGENT_DATE_SIGNAL, ["MULTIPLE_DISTINCT_DATE_TOKENS"]

    if len(distinct_dates) == 1 and paths_without_dates:
        return (
            INFERENCE_PARTIAL_DATE_OR_KEYWORD_SIGNAL,
            ["SINGLE_DATE_TOKEN_PARTIAL_COVERAGE"],
        )

    if any_historical_keyword:
        if depth_spread >= 1 and not shallowest.historical_keywords_found:
            return (
                INFERENCE_PARTIAL_DATE_OR_KEYWORD_SIGNAL,
                ["HISTORICAL_KEYWORD_PRESENT", "DEPTH_ASYMMETRY_WITH_KEYWORD"],
            )
        return INFERENCE_UNIFORM_KEYWORD_NO_FURTHER_SIGNAL, ["HISTORICAL_KEYWORD_PRESENT"]

    return INFERENCE_NO_PROVENANCE_SIGNAL, ["NO_DATE_OR_KEYWORD_SIGNAL"]


def _overlapping_directory_signature(
    file_paths: list[str], directory_groups_by_dir: dict[str, str]
) -> str | None:
    signatures = set()
    for path in file_paths:
        parent = os.path.dirname(path)
        while parent and parent != os.path.dirname(parent):
            if parent in directory_groups_by_dir:
                signatures.add(directory_groups_by_dir[parent])
                break
            parent = os.path.dirname(parent)
        else:
            return None
    return next(iter(signatures)) if len(signatures) == 1 else None


def classify(raw: RawProvenanceData) -> ProvenanceAnalysis:
    """STAGE 2 - a PURE function over already-collected data. Never
    touches the filesystem; safe to re-run as many times as needed
    after a classification-logic change, with zero additional T7
    access."""
    analysis = ProvenanceAnalysis(
        d1_root=raw.d1_root,
        d1_analyzed_at=raw.d1_analyzed_at,
        d2_analyzed_at=raw.collected_at,
    )

    by_kind: dict[str, list[GroupProvenance]] = {
        "exact_duplicate": [],
        "cryptomator_chunk_duplicate": [],
        "directory_duplicate": [],
    }

    for raw_group in raw.groups:
        inference_code, evidence_codes = _classify_group(raw_group.paths)
        missing = sum(1 for p in raw_group.paths if not p.exists)
        analysis.paths_no_longer_existing += missing
        if missing:
            evidence_codes = [*evidence_codes, "PATHS_MISSING_IN_D2"]

        overlap = None
        if raw_group.group_kind != "directory_duplicate":
            overlap = _overlapping_directory_signature(
                [p.path for p in raw_group.paths], raw.directory_groups_by_dir
            )

        group = GroupProvenance(
            group_kind=raw_group.group_kind,
            group_key=raw_group.group_key,
            copies=raw_group.copies,
            reclaimable_bytes=raw_group.reclaimable_bytes,
            paths=[p.path for p in raw_group.paths],
            structured_facts=_build_structured_facts(raw_group.paths),
            evidence_codes=evidence_codes,
            inference_code=inference_code,
            confidence=INFERENCE_CONFIDENCE[inference_code],
            requires_human_review=_REQUIRES_REVIEW[inference_code],
            overlaps_directory_group=overlap,
        )
        by_kind[raw_group.group_kind].append(group)

        analysis.inference_counts[inference_code] = (
            analysis.inference_counts.get(inference_code, 0) + 1
        )
        analysis.confidence_counts[group.confidence] = (
            analysis.confidence_counts.get(group.confidence, 0) + 1
        )

    analysis.exact_duplicate_provenance = by_kind["exact_duplicate"]
    analysis.cryptomator_chunk_provenance = by_kind["cryptomator_chunk_duplicate"]
    analysis.directory_duplicate_provenance = by_kind["directory_duplicate"]
    return analysis


def analyze_provenance(d1_report_path: Path) -> ProvenanceAnalysis:
    """Convenience wrapper: run both stages back to back against a
    fresh D1 report. Prefer calling `collect_path_signals` once and
    `classify` separately (and repeatedly) when iterating on
    classification logic - see module docstring."""
    return classify(collect_path_signals(d1_report_path))


def write_provenance_report(analysis: ProvenanceAnalysis, destination: Path) -> Path:
    """Write `analysis` as pretty-printed JSON to `destination`
    (creating parent directories as needed) and return the path
    written. Refuses (`ValueError`, nothing written) if `destination`
    resolves to `analysis.d1_root` or anywhere inside it - see
    `app.discovery.safety.reject_destination_inside_root`."""
    destination = Path(destination)
    reject_destination_inside_root(analysis.d1_root, destination)

    destination.parent.mkdir(parents=True, exist_ok=True)
    destination.write_text(json.dumps(analysis.to_json_dict(), indent=2, default=str))
    return destination
