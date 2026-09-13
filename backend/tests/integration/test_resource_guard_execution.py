"""Tests for Implementation Milestone 3's `BatchResourceGuard` (Scaled
Real-T7 Ingestion). See "Scaled Real-T7 Ingestion - Implementation
Design Pass" (`2fab4b3`), section "### 6. `BatchResourceGuard`", and
the numeric pass's "### 9. Resource-guard thresholds" table for the
frozen behavior under test.

No T7 access, no real disk-usage assumptions, no real Ollama process:
`disk_usage_fn` and `ollama_client` are fully injected fakes throughout,
per the guard's own "pluggable" design (Milestone 3 authorization,
point 3). No database is touched by this module at all - `IngestionBatch`
is only ever constructed in memory (transient, never `db.add()`-ed),
since `check_before_claim`/`check_before_expensive_operation` accept the
object but do not currently read any of its fields (see the guard's own
docstring for why).
"""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
from typing import NamedTuple

import pytest

from app.classification.resource_guard import (
    OLLAMA_HARD_STOP_CONSECUTIVE_FAILURES,
    POSTGRES_HARD_STOP_FREE_BYTES,
    POSTGRES_SOFT_STOP_FREE_BYTES,
    WORKSPACE_HARD_STOP_FREE_BYTES,
    WORKSPACE_SOFT_STOP_FREE_BYTES,
    BatchResourceGuard,
    ExpensiveOperationKind,
    GuardTier,
)
from app.models.ingestion_batch import BatchStopReason, IngestionBatch


class _DiskUsage(NamedTuple):
    total: int
    used: int
    free: int


def _disk_usage_fn(free_by_path: dict[Path, int]):
    def _fn(path: Path) -> _DiskUsage:
        return _DiskUsage(total=1, used=1, free=free_by_path[path])

    return _fn


class _FakeOllamaClient:
    """Records whether `list()` (never `embed()`) is what gets called,
    and can be scripted to raise on demand to simulate unreachability."""

    def __init__(self, *, raises: bool = False):
        self.raises = raises
        self.list_calls = 0
        self.embed_calls = 0

    def list(self):
        self.list_calls += 1
        if self.raises:
            raise ConnectionError("simulated ollama unreachable")
        return {"models": []}

    def embed(self, *args, **kwargs):  # pragma: no cover - must never be called
        self.embed_calls += 1
        raise AssertionError("BatchResourceGuard must never call embed()")


def _batch() -> IngestionBatch:
    """A transient, never-persisted IngestionBatch - sufficient because
    the guard does not currently read any of its fields (see the
    guard's own docstring)."""
    return IngestionBatch(
        classification_run_id=1,
        max_source_instances=1,
        max_source_bytes=1,
        max_embeddings=1,
        max_runtime_seconds=1,
        eligible_source_count=0,
        policy_filtered_count=0,
        selectable_count=0,
        source_instances_selected=0,
        source_bytes_selected=0,
        selection_fingerprint="0" * 64,
        selection_policy_version="test-v1",
        ordering_version="test-ordering-v1",
    )


_WORKSPACE_PATH = Path("/fake/workspace")
_POSTGRES_PATH = Path("/fake/postgres")


def _guard(
    *,
    workspace_free: int = WORKSPACE_SOFT_STOP_FREE_BYTES + 1,
    postgres_free: int = POSTGRES_SOFT_STOP_FREE_BYTES + 1,
    ollama_client: _FakeOllamaClient | None = None,
) -> BatchResourceGuard:
    return BatchResourceGuard(
        workspace_path=_WORKSPACE_PATH,
        postgres_path=_POSTGRES_PATH,
        disk_usage_fn=_disk_usage_fn({_WORKSPACE_PATH: workspace_free, _POSTGRES_PATH: postgres_free}),
        ollama_client=ollama_client or _FakeOllamaClient(),
    )


# -- check_before_claim: disk tiers ---------------------------------------


def test_normal_when_both_disks_well_above_soft_stop() -> None:
    guard = _guard(
        workspace_free=WORKSPACE_SOFT_STOP_FREE_BYTES + 1,
        postgres_free=POSTGRES_SOFT_STOP_FREE_BYTES + 1,
    )
    result = guard.check_before_claim(_batch())
    assert result.tier is GuardTier.NORMAL
    assert result.stop_reason is None
    assert result.review_required is False


def test_workspace_soft_stop_between_hard_and_soft_threshold() -> None:
    guard = _guard(workspace_free=WORKSPACE_HARD_STOP_FREE_BYTES + 1)
    result = guard.check_before_claim(_batch())
    assert result.tier is GuardTier.SOFT_STOP
    assert result.stop_reason is BatchStopReason.WORKSPACE_SOFT_STOP


def test_workspace_hard_stop_below_hard_threshold() -> None:
    guard = _guard(workspace_free=WORKSPACE_HARD_STOP_FREE_BYTES - 1)
    result = guard.check_before_claim(_batch())
    assert result.tier is GuardTier.HARD_STOP
    assert result.stop_reason is BatchStopReason.WORKSPACE_HARD_STOP


def test_postgres_soft_stop_between_hard_and_soft_threshold() -> None:
    guard = _guard(postgres_free=POSTGRES_HARD_STOP_FREE_BYTES + 1)
    result = guard.check_before_claim(_batch())
    assert result.tier is GuardTier.SOFT_STOP
    assert result.stop_reason is BatchStopReason.POSTGRES_SOFT_STOP


def test_postgres_hard_stop_below_hard_threshold() -> None:
    guard = _guard(postgres_free=POSTGRES_HARD_STOP_FREE_BYTES - 1)
    result = guard.check_before_claim(_batch())
    assert result.tier is GuardTier.HARD_STOP
    assert result.stop_reason is BatchStopReason.POSTGRES_HARD_STOP


def test_exactly_at_threshold_boundary_is_not_yet_a_stop() -> None:
    """The frozen thresholds are strict '<' comparisons - exactly at the
    boundary is still the better tier, never off-by-one into the worse
    one."""
    guard = _guard(
        workspace_free=WORKSPACE_SOFT_STOP_FREE_BYTES,
        postgres_free=POSTGRES_SOFT_STOP_FREE_BYTES,
    )
    result = guard.check_before_claim(_batch())
    assert result.tier is GuardTier.NORMAL


def test_tightest_budget_governs_when_both_fire_at_different_tiers() -> None:
    """Workspace SOFT_STOP + Postgres HARD_STOP must report the worse
    (HARD_STOP) overall tier - the frozen invariant that 'the tightest
    remaining budget among them governs continued execution.'"""
    guard = _guard(
        workspace_free=WORKSPACE_HARD_STOP_FREE_BYTES + 1,
        postgres_free=POSTGRES_HARD_STOP_FREE_BYTES - 1,
    )
    result = guard.check_before_claim(_batch())
    assert result.tier is GuardTier.HARD_STOP
    assert result.stop_reason is BatchStopReason.POSTGRES_HARD_STOP


def test_only_three_tiers_exist() -> None:
    assert {t.value for t in GuardTier} == {"normal", "soft_stop", "hard_stop"}


# -- check_before_expensive_operation: archive extraction -----------------


def test_archive_extraction_check_only_consults_workspace_disk() -> None:
    """Postgres being at hard-stop must NOT affect the archive-
    extraction check - only workspace disk is re-checked before that
    specific operation, per the frozen design."""
    guard = _guard(
        workspace_free=WORKSPACE_SOFT_STOP_FREE_BYTES + 1,
        postgres_free=POSTGRES_HARD_STOP_FREE_BYTES - 1,
    )
    result = guard.check_before_expensive_operation(_batch(), ExpensiveOperationKind.ARCHIVE_EXTRACTION)
    assert result.tier is GuardTier.NORMAL


def test_archive_extraction_check_hard_stops_on_low_workspace() -> None:
    guard = _guard(workspace_free=WORKSPACE_HARD_STOP_FREE_BYTES - 1)
    result = guard.check_before_expensive_operation(_batch(), ExpensiveOperationKind.ARCHIVE_EXTRACTION)
    assert result.tier is GuardTier.HARD_STOP
    assert result.stop_reason is BatchStopReason.WORKSPACE_HARD_STOP


# -- check_before_expensive_operation: embedding / ollama ------------------


def test_embedding_check_normal_when_ollama_reachable() -> None:
    client = _FakeOllamaClient(raises=False)
    guard = _guard(ollama_client=client)
    result = guard.check_before_expensive_operation(_batch(), ExpensiveOperationKind.EMBEDDING)
    assert result.tier is GuardTier.NORMAL
    assert client.list_calls == 1
    assert client.embed_calls == 0


def test_embedding_check_never_calls_embed() -> None:
    client = _FakeOllamaClient(raises=True)
    guard = _guard(ollama_client=client)
    guard.check_before_expensive_operation(_batch(), ExpensiveOperationKind.EMBEDDING)
    assert client.embed_calls == 0


def test_single_ollama_failure_is_not_yet_a_stop() -> None:
    client = _FakeOllamaClient(raises=True)
    guard = _guard(ollama_client=client)
    result = guard.check_before_expensive_operation(_batch(), ExpensiveOperationKind.EMBEDDING)
    assert result.tier is GuardTier.NORMAL
    assert result.stop_reason is None


def test_ollama_hard_stops_on_exactly_the_frozen_consecutive_failure_count() -> None:
    client = _FakeOllamaClient(raises=True)
    guard = _guard(ollama_client=client)
    results = [
        guard.check_before_expensive_operation(_batch(), ExpensiveOperationKind.EMBEDDING)
        for _ in range(OLLAMA_HARD_STOP_CONSECUTIVE_FAILURES)
    ]
    assert [r.tier for r in results[:-1]] == [GuardTier.NORMAL] * (OLLAMA_HARD_STOP_CONSECUTIVE_FAILURES - 1)
    assert results[-1].tier is GuardTier.HARD_STOP
    assert results[-1].stop_reason is BatchStopReason.OLLAMA_PERSISTENTLY_UNREACHABLE


def test_ollama_consecutive_failure_count_resets_on_a_successful_check() -> None:
    client = _FakeOllamaClient(raises=True)
    guard = _guard(ollama_client=client)
    guard.check_before_expensive_operation(_batch(), ExpensiveOperationKind.EMBEDDING)
    guard.check_before_expensive_operation(_batch(), ExpensiveOperationKind.EMBEDDING)
    client.raises = False
    reset_result = guard.check_before_expensive_operation(_batch(), ExpensiveOperationKind.EMBEDDING)
    assert reset_result.tier is GuardTier.NORMAL
    client.raises = True
    # Two more failures after the reset must NOT yet hard-stop - proves
    # the counter actually reset rather than merely pausing.
    next_result = guard.check_before_expensive_operation(_batch(), ExpensiveOperationKind.EMBEDDING)
    assert next_result.tier is GuardTier.NORMAL


def test_consecutive_failure_count_does_not_survive_a_simulated_process_restart() -> None:
    """Documents the exact, explicit boundary from the Milestone 3
    final-correction pass: the consecutive-failure counter is
    process-local to one guard INSTANCE, never durable, never shared -
    a fresh instance (standing in for a fresh process after a restart)
    must start back at zero, never inherit a prior instance's count."""
    client = _FakeOllamaClient(raises=True)
    first_guard = _guard(ollama_client=client)
    # One failure short of the hard-stop threshold - not yet a stop.
    for _ in range(OLLAMA_HARD_STOP_CONSECUTIVE_FAILURES - 1):
        result = first_guard.check_before_expensive_operation(_batch(), ExpensiveOperationKind.EMBEDDING)
        assert result.tier is GuardTier.NORMAL

    # Simulate a process restart: a brand-new guard instance, same
    # (still-failing) Ollama client. If the count were durable/shared,
    # this next check would be the Nth consecutive failure and hard-stop;
    # since it is NOT, it must be treated as failure #1 again.
    restarted_guard = _guard(ollama_client=client)
    post_restart_result = restarted_guard.check_before_expensive_operation(
        _batch(), ExpensiveOperationKind.EMBEDDING
    )
    assert post_restart_result.tier is GuardTier.NORMAL
    assert post_restart_result.stop_reason is None


def test_check_ollama_reachable_returns_plain_bool() -> None:
    assert _guard(ollama_client=_FakeOllamaClient(raises=False)).check_ollama_reachable() is True
    assert _guard(ollama_client=_FakeOllamaClient(raises=True)).check_ollama_reachable() is False


def test_check_ollama_reachable_never_calls_embed() -> None:
    client = _FakeOllamaClient(raises=False)
    _guard(ollama_client=client).check_ollama_reachable()
    assert client.list_calls == 1
    assert client.embed_calls == 0


def test_unknown_expensive_operation_kind_raises() -> None:
    guard = _guard()
    with pytest.raises(ValueError):
        guard.check_before_expensive_operation(_batch(), SimpleNamespace())  # type: ignore[arg-type]


# -- review_required: honestly never set this milestone --------------------


def test_review_required_is_never_true_from_any_check_this_milestone() -> None:
    """Every review-required trigger in the frozen thresholds table
    needs either a DB-growth projection formula (forbidden) or a
    calibrated Ollama latency ceiling (unresolved) - neither exists in
    this milestone, so this field must stay honestly False throughout,
    never fabricated."""
    guard = _guard(
        workspace_free=WORKSPACE_HARD_STOP_FREE_BYTES - 1,
        postgres_free=POSTGRES_HARD_STOP_FREE_BYTES - 1,
        ollama_client=_FakeOllamaClient(raises=True),
    )
    disk_result = guard.check_before_claim(_batch())
    embedding_result = guard.check_before_expensive_operation(_batch(), ExpensiveOperationKind.EMBEDDING)
    extraction_result = guard.check_before_expensive_operation(_batch(), ExpensiveOperationKind.ARCHIVE_EXTRACTION)
    assert disk_result.review_required is False
    assert embedding_result.review_required is False
    assert extraction_result.review_required is False
