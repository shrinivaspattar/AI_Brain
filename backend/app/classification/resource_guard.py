from __future__ import annotations

import shutil
from collections.abc import Callable
from dataclasses import dataclass
from enum import Enum
from pathlib import Path

import ollama

from app.core.config import settings
from app.models.ingestion_batch import BatchStopReason, IngestionBatch

# -- Frozen policy thresholds -------------------------------------------
#
# These are the RESERVE numbers and their derived soft-stop margins from
# "Scaled Real-T7 Ingestion - Numeric + Policy Definition Pass" (`3d37ec0`),
# section "### 9. Resource-guard thresholds" - genuinely frozen policy,
# not environment configuration. They are deliberately Python constants
# here, not `Settings` fields: unlike `INGESTION_DIR`/`OLLAMA_HOST` (which
# legitimately vary per machine), these reserve values are an approved
# policy decision that should require a new design gate to change, never
# a casual `.env` edit.
#
# The frozen doc's *currently-measured* free-space baselines (25GB
# workspace, 13GB Postgres, as of the numeric pass) are explicitly NOT
# reproduced as constants anywhere in this module - only the reserves
# below are frozen; live free space is always read fresh via
# `shutil.disk_usage` at check time, never assumed.
WORKSPACE_HARD_STOP_FREE_BYTES = 10 * 1024**3
WORKSPACE_SOFT_STOP_FREE_BYTES = 15 * 1024**3
POSTGRES_HARD_STOP_FREE_BYTES = 5 * 1024**3
POSTGRES_SOFT_STOP_FREE_BYTES = 8 * 1024**3
OLLAMA_HARD_STOP_CONSECUTIVE_FAILURES = 3

# Default paths for the two disk checks in the frozen thresholds table
# (workspace = `/home`, Postgres = `/`, the tighter constraint on this
# machine). Constructor parameters, not `Settings` fields, so tests can
# inject a `disk_usage_fn` without needing a real filesystem at these
# exact paths - see `BatchResourceGuard.__init__`.
DEFAULT_WORKSPACE_DISK_PATH = Path("/home")
DEFAULT_POSTGRES_DISK_PATH = Path("/")


class GuardTier(str, Enum):
    """Exactly three tiers - no fourth resource-depletion tier, per the
    Implementation Milestone 3 authorization. `review_required` is a
    separate, orthogonal flag on `GuardResult` (matching `IngestionBatch.
    review_required`'s own separateness from `status`), never a fourth
    tier value."""

    NORMAL = "normal"
    SOFT_STOP = "soft_stop"
    HARD_STOP = "hard_stop"


_TIER_SEVERITY = {GuardTier.NORMAL: 0, GuardTier.SOFT_STOP: 1, GuardTier.HARD_STOP: 2}


class ExpensiveOperationKind(str, Enum):
    """The two expensive-operation checkpoints named explicitly in the
    frozen design's `BatchResourceGuard` section: workspace free space
    immediately before archive extraction, Ollama reachability
    immediately before an embedding call. Neither check here performs
    the operation itself - extraction and embedding execution are later
    milestones."""

    ARCHIVE_EXTRACTION = "archive_extraction"
    EMBEDDING = "embedding"


@dataclass(frozen=True)
class GuardResult:
    """A structured decision, never a boolean, per the Milestone 3
    authorization. `stop_reason` is populated with the exact,
    already-frozen `BatchStopReason` value that applies (never a new or
    renamed value) whenever `tier` is not `NORMAL`; it is `None` for
    `NORMAL`.

    `review_required` is carried here for structural symmetry with the
    milestone's request, but this implementation never sets it `True`:
    every review-required trigger in the frozen thresholds table
    (workspace/Postgres *projected* consumption, an Ollama latency
    *spike*) requires either a DB-growth projection formula (explicitly
    forbidden by the frozen design - see point 6 below) or a calibrated
    latency ceiling (explicitly `UNRESOLVED` per the numeric pass, since
    no baseline embedding-call latency has ever been measured against
    this Ollama instance). Wiring an actual trigger for either is
    correctly out of scope for this milestone; a future milestone with
    real calibration evidence can set this field without changing its
    shape.
    """

    tier: GuardTier
    stop_reason: BatchStopReason | None
    detail: str
    review_required: bool = False


class BatchResourceGuard:
    """Pluggable resource guard implementing the frozen "### 6.
    `BatchResourceGuard`" interface. Every dependency (disk-usage probe,
    Ollama client, the two disk paths) is constructor-injectable so
    tests never depend on this machine's actual free space or a real
    Ollama process - see the accompanying test module for real-Postgres-
    free but fully deterministic coverage.

    NOT DOING (explicitly, per the Milestone 3 authorization): no
    DB-growth projection formula (`BatchReportService` measures actual
    `pg_database_size()` delta post hoc, in a future milestone, never
    predicted here); no CPU/RAM check (deferred entirely, `psutil` not
    adopted); no embedding calls of any kind, ever - `check_ollama_
    reachable` calls only `ollama.Client.list()` (hits `/api/tags`),
    which is reachability, not performance calibration.

    OLLAMA CONSECUTIVE-FAILURE COUNT - EXACT BOUNDARY (per the
    Milestone 3 final-correction pass, stated explicitly rather than
    left implicit): `_consecutive_ollama_failures` is a **process-local,
    in-memory-only counter scoped to this one guard INSTANCE**. It is
    NOT a durable, batch-wide "three strikes" counter - it does not
    survive a process restart, is never read from or shared with any
    other `BatchResourceGuard` instance (even one checking the exact
    same batch), and has no corresponding column on `IngestionBatch`.
    A worker process that crashes and restarts with a fresh guard
    instance begins counting from zero again, exactly like the
    runtime-accounting session model above - this is a deliberate,
    honest limitation, never claimed to be stronger than it is. If a
    future worker-integration milestone needs the 3-consecutive-checks
    threshold to hold durably ACROSS process restarts (not just within
    one long-lived process), that is new scope for that milestone to
    design and implement - e.g. a durable counter column with its own
    concurrency-safe increment/reset semantics - not something this
    guard silently already provides.
    """

    def __init__(
        self,
        *,
        workspace_path: Path = DEFAULT_WORKSPACE_DISK_PATH,
        postgres_path: Path = DEFAULT_POSTGRES_DISK_PATH,
        disk_usage_fn: Callable[[Path], shutil._ntuple_diskusage] = shutil.disk_usage,
        ollama_client: ollama.Client | None = None,
    ) -> None:
        self.workspace_path = workspace_path
        self.postgres_path = postgres_path
        self._disk_usage_fn = disk_usage_fn
        self._ollama_client = ollama_client or ollama.Client(host=settings.OLLAMA_HOST)
        # Process-local, in-memory only, NOT durable, NOT shared across
        # instances or processes, does NOT survive a restart - see the
        # class docstring's "OLLAMA CONSECUTIVE-FAILURE COUNT" section
        # for the exact, explicit boundary.
        self._consecutive_ollama_failures = 0

    def check_before_claim(self, batch: IngestionBatch) -> GuardResult:
        """Workspace + Postgres free space - the "before claiming work"
        checkpoint, cheap and fast, per the frozen design. `batch` is
        accepted (not currently read) to match the frozen interface
        signature and leave room for a future per-batch-class threshold
        without changing every call site again."""
        del batch  # unused today - see docstring
        return self._check_disk()

    def check_before_expensive_operation(
        self, batch: IngestionBatch, kind: ExpensiveOperationKind
    ) -> GuardResult:
        """Re-checks the ONE resource that specific expensive operation
        actually stresses, per the frozen design - workspace free space
        immediately before archive extraction, Ollama reachability
        immediately before an embedding call. Never performs the
        operation itself."""
        del batch  # unused today - see check_before_claim's docstring
        if kind is ExpensiveOperationKind.ARCHIVE_EXTRACTION:
            return self._check_workspace_disk()
        if kind is ExpensiveOperationKind.EMBEDDING:
            return self._check_ollama_for_embedding()
        raise ValueError(f"unknown expensive-operation kind: {kind!r}")

    def check_ollama_reachable(self) -> bool:
        """A pure reachability probe - `ollama.Client.list()` hits
        `/api/tags`, never `/api/embed`. Explicitly NOT a performance
        calibration: a `True` result means Ollama answered, nothing
        about how fast it answered."""
        try:
            self._ollama_client.list()
            return True
        except Exception:
            return False

    # -- internals --------------------------------------------------

    def _check_disk(self) -> GuardResult:
        workspace_result = self._check_workspace_disk()
        postgres_result = self._check_postgres_disk()
        if _TIER_SEVERITY[postgres_result.tier] > _TIER_SEVERITY[workspace_result.tier]:
            return postgres_result
        return workspace_result

    def _check_workspace_disk(self) -> GuardResult:
        free_bytes = self._disk_usage_fn(self.workspace_path).free
        if free_bytes < WORKSPACE_HARD_STOP_FREE_BYTES:
            return GuardResult(
                tier=GuardTier.HARD_STOP,
                stop_reason=BatchStopReason.WORKSPACE_HARD_STOP,
                detail=(
                    f"workspace disk free bytes {free_bytes} < hard-stop threshold "
                    f"{WORKSPACE_HARD_STOP_FREE_BYTES} at {self.workspace_path}"
                ),
            )
        if free_bytes < WORKSPACE_SOFT_STOP_FREE_BYTES:
            return GuardResult(
                tier=GuardTier.SOFT_STOP,
                stop_reason=BatchStopReason.WORKSPACE_SOFT_STOP,
                detail=(
                    f"workspace disk free bytes {free_bytes} < soft-stop threshold "
                    f"{WORKSPACE_SOFT_STOP_FREE_BYTES} at {self.workspace_path}"
                ),
            )
        return GuardResult(
            tier=GuardTier.NORMAL,
            stop_reason=None,
            detail=f"workspace disk free bytes {free_bytes} at {self.workspace_path}",
        )

    def _check_postgres_disk(self) -> GuardResult:
        free_bytes = self._disk_usage_fn(self.postgres_path).free
        if free_bytes < POSTGRES_HARD_STOP_FREE_BYTES:
            return GuardResult(
                tier=GuardTier.HARD_STOP,
                stop_reason=BatchStopReason.POSTGRES_HARD_STOP,
                detail=(
                    f"postgres disk free bytes {free_bytes} < hard-stop threshold "
                    f"{POSTGRES_HARD_STOP_FREE_BYTES} at {self.postgres_path}"
                ),
            )
        if free_bytes < POSTGRES_SOFT_STOP_FREE_BYTES:
            return GuardResult(
                tier=GuardTier.SOFT_STOP,
                stop_reason=BatchStopReason.POSTGRES_SOFT_STOP,
                detail=(
                    f"postgres disk free bytes {free_bytes} < soft-stop threshold "
                    f"{POSTGRES_SOFT_STOP_FREE_BYTES} at {self.postgres_path}"
                ),
            )
        return GuardResult(
            tier=GuardTier.NORMAL,
            stop_reason=None,
            detail=f"postgres disk free bytes {free_bytes} at {self.postgres_path}",
        )

    def _check_ollama_for_embedding(self) -> GuardResult:
        if self.check_ollama_reachable():
            self._consecutive_ollama_failures = 0
            return GuardResult(
                tier=GuardTier.NORMAL,
                stop_reason=None,
                detail="ollama reachable",
            )
        self._consecutive_ollama_failures += 1
        if self._consecutive_ollama_failures >= OLLAMA_HARD_STOP_CONSECUTIVE_FAILURES:
            return GuardResult(
                tier=GuardTier.HARD_STOP,
                stop_reason=BatchStopReason.OLLAMA_PERSISTENTLY_UNREACHABLE,
                detail=(
                    f"ollama unreachable on {self._consecutive_ollama_failures} "
                    "consecutive checks (persistent, this process's own count)"
                ),
            )
        return GuardResult(
            tier=GuardTier.NORMAL,
            stop_reason=None,
            detail=(
                f"ollama unreachable on {self._consecutive_ollama_failures} consecutive "
                f"check(s), below the {OLLAMA_HARD_STOP_CONSECUTIVE_FAILURES}-check "
                "persistence threshold - not yet a stop condition"
            ),
        )
