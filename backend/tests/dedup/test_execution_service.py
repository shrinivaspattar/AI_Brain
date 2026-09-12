from datetime import UTC, datetime
from unittest.mock import MagicMock

import pytest

from app.dedup.execution_plan_service import ActionValidity, PlanValidity
from app.dedup.execution_service import DedupExecutionService
from app.models.dedup_authorization import DedupPlanAuthorization, DedupPlanAuthorizationStatus
from app.models.dedup_execution import (
    DedupExecution,
    DedupExecutionActionAudit,
    DedupExecutionActionResult,
    DedupExecutionStatus,
)
from app.models.dedup_execution_plan import DedupExecutionPlanAction, DedupPlanActionType


def _authorization(
    authorization_id: int = 1,
    plan_id: int = 1,
    status: DedupPlanAuthorizationStatus = DedupPlanAuthorizationStatus.AUTHORIZED,
) -> DedupPlanAuthorization:
    return DedupPlanAuthorization(
        id=authorization_id,
        plan_id=plan_id,
        status=status,
        validity_snapshot={"is_valid": True},
    )


def _validity(plan_id: int = 1, is_valid: bool = True) -> PlanValidity:
    return PlanValidity(
        plan_id=plan_id,
        canonical_document_exists=True,
        canonical_path_changed=False,
        canonical_exists_now=True,
        canonical_type_matches=True,
        canonical_hash_matches=is_valid,
        canonical_size_matches=True,
        canonical_valid=is_valid,
        actions=[
            ActionValidity(
                action_id=1,
                document_id="doc-dup",
                source_path="/documents/dup.txt",
                document_exists=True,
                path_changed=False,
                exists_now=True,
                type_matches=True,
                hash_matches=is_valid,
                size_matches=True,
                is_valid=is_valid,
            )
        ],
        is_valid=is_valid,
    )


def _execution(
    execution_id: int = 1,
    authorization_id: int = 1,
    plan_id: int = 1,
    status: DedupExecutionStatus = DedupExecutionStatus.RUNNING,
) -> DedupExecution:
    return DedupExecution(
        id=execution_id,
        authorization_id=authorization_id,
        plan_id=plan_id,
        status=status,
        started_at=datetime(2026, 9, 12, 10, 0, tzinfo=UTC),
    )


def _plan_action(
    action_id: int = 1,
    plan_id: int = 1,
    document_id: str = "doc-dup",
) -> DedupExecutionPlanAction:
    return DedupExecutionPlanAction(
        id=action_id,
        plan_id=plan_id,
        document_id=document_id,
        action=DedupPlanActionType.DELETE,
        source_path="/documents/dup.txt",
        target_document_id="doc-canonical",
        target_path="/documents/canonical.txt",
        observed_exists=True,
        observed_content_hash="hash-a",
        observed_file_size=11,
        reason="test",
    )


def _audit(
    audit_id: int = 1,
    execution_id: int = 1,
    plan_action_id: int = 1,
    result: DedupExecutionActionResult = DedupExecutionActionResult.SUCCESS,
) -> DedupExecutionActionAudit:
    return DedupExecutionActionAudit(
        id=audit_id,
        execution_id=execution_id,
        plan_action_id=plan_action_id,
        document_id="doc-dup",
        planned_action=DedupPlanActionType.DELETE,
        source_path="/documents/dup.txt",
        target_path="/documents/canonical.txt",
        expected_content_hash="hash-a",
        expected_file_size=11,
        result=result,
        filesystem_mutation_occurred=(
            None
            if result == DedupExecutionActionResult.UNKNOWN
            else result == DedupExecutionActionResult.SUCCESS
        ),
    )


def _service_for(
    db,
    authorization=None,
    validity=None,
    existing_execution=None,
) -> DedupExecutionService:
    service = DedupExecutionService(db)
    service.authorization_service.get_authorization = MagicMock(
        return_value=authorization
    )
    service.plan_service.check_plan_validity = MagicMock(return_value=validity)
    service._get_execution_for_authorization = MagicMock(
        return_value=existing_execution
    )
    return service


# --- start_execution: happy path -------------------------------------


def test_start_execution_succeeds_for_authorized_and_valid_plan() -> None:
    db = MagicMock()
    authorization = _authorization()
    validity = _validity(is_valid=True)
    service = _service_for(db, authorization=authorization, validity=validity)

    execution = service.start_execution(1, executor_identity="test-executor")

    assert execution.authorization_id == 1
    assert execution.plan_id == 1
    assert execution.status == DedupExecutionStatus.RUNNING
    assert execution.executor_identity == "test-executor"
    db.add.assert_called_once()
    db.commit.assert_called_once()


# --- start_execution: pre-check failures ------------------------------


def test_start_execution_raises_for_missing_authorization() -> None:
    db = MagicMock()
    service = _service_for(db, authorization=None)

    with pytest.raises(ValueError, match="not found"):
        service.start_execution(999)

    db.add.assert_not_called()


def test_start_execution_raises_for_revoked_authorization() -> None:
    db = MagicMock()
    authorization = _authorization(status=DedupPlanAuthorizationStatus.REVOKED)
    service = _service_for(db, authorization=authorization)

    with pytest.raises(ValueError, match="is not active"):
        service.start_execution(1)

    db.add.assert_not_called()


def test_start_execution_raises_for_existing_execution() -> None:
    db = MagicMock()
    authorization = _authorization()
    existing = _execution()
    service = _service_for(db, authorization=authorization, existing_execution=existing)

    with pytest.raises(ValueError, match="already has an execution"):
        service.start_execution(1)

    db.add.assert_not_called()


def test_start_execution_raises_for_stale_plan() -> None:
    db = MagicMock()
    authorization = _authorization()
    validity = _validity(is_valid=False)
    service = _service_for(db, authorization=authorization, validity=validity)

    with pytest.raises(ValueError, match="no longer valid"):
        service.start_execution(1)

    db.add.assert_not_called()


def test_start_execution_checks_activity_before_stale_plan() -> None:
    """Cheap DB-only checks (authorization active, no existing
    execution) must short-circuit before the filesystem-touching
    validity re-check."""
    db = MagicMock()
    authorization = _authorization(status=DedupPlanAuthorizationStatus.REVOKED)
    service = _service_for(db, authorization=authorization)

    with pytest.raises(ValueError, match="is not active"):
        service.start_execution(1)

    service.plan_service.check_plan_validity.assert_not_called()


# --- record_action_result: happy paths --------------------------------


def test_record_action_result_success_requires_mutation() -> None:
    db = MagicMock()
    execution = _execution()
    plan_action = _plan_action()

    service = DedupExecutionService(db)
    service.get_execution = MagicMock(return_value=execution)
    db.get.return_value = plan_action
    db.scalar.return_value = None  # no existing audit row

    audit = service.record_action_result(
        1,
        1,
        DedupExecutionActionResult.SUCCESS,
        observed_content_hash="hash-a",
        observed_file_size=11,
        filesystem_mutation_occurred=True,
    )

    assert audit.result == DedupExecutionActionResult.SUCCESS
    assert audit.filesystem_mutation_occurred is True
    # Planned fields copied straight from the plan action, not from caller input.
    assert audit.expected_content_hash == "hash-a"
    assert audit.expected_file_size == 11
    assert audit.planned_action == DedupPlanActionType.DELETE
    assert audit.source_path == "/documents/dup.txt"
    assert audit.target_path == "/documents/canonical.txt"
    assert audit.document_id == "doc-dup"
    db.commit.assert_called_once()


def test_record_action_result_precondition_failed_requires_no_mutation() -> None:
    db = MagicMock()
    execution = _execution()
    plan_action = _plan_action()

    service = DedupExecutionService(db)
    service.get_execution = MagicMock(return_value=execution)
    db.get.return_value = plan_action
    db.scalar.return_value = None

    audit = service.record_action_result(
        1,
        1,
        DedupExecutionActionResult.PRECONDITION_FAILED,
        observed_content_hash="different-hash",
        error_message="hash mismatch: expected hash-a, observed different-hash",
        filesystem_mutation_occurred=False,
    )

    assert audit.result == DedupExecutionActionResult.PRECONDITION_FAILED
    assert audit.filesystem_mutation_occurred is False
    assert "hash mismatch" in audit.error_message


def test_record_action_result_not_attempted() -> None:
    db = MagicMock()
    execution = _execution()
    plan_action = _plan_action()

    service = DedupExecutionService(db)
    service.get_execution = MagicMock(return_value=execution)
    db.get.return_value = plan_action
    db.scalar.return_value = None

    audit = service.record_action_result(
        1, 1, DedupExecutionActionResult.NOT_ATTEMPTED
    )

    assert audit.result == DedupExecutionActionResult.NOT_ATTEMPTED
    assert audit.filesystem_mutation_occurred is False
    assert audit.started_at is None
    assert audit.ended_at is None


def test_record_action_result_failed() -> None:
    db = MagicMock()
    execution = _execution()
    plan_action = _plan_action()

    service = DedupExecutionService(db)
    service.get_execution = MagicMock(return_value=execution)
    db.get.return_value = plan_action
    db.scalar.return_value = None

    audit = service.record_action_result(
        1,
        1,
        DedupExecutionActionResult.FAILED,
        error_message="Permission denied",
        filesystem_mutation_occurred=False,
    )

    assert audit.result == DedupExecutionActionResult.FAILED
    assert audit.error_message == "Permission denied"


# --- record_action_result: pre-check failures --------------------------


def test_record_action_result_raises_for_missing_execution() -> None:
    db = MagicMock()
    service = DedupExecutionService(db)
    service.get_execution = MagicMock(return_value=None)

    with pytest.raises(ValueError, match="not found"):
        service.record_action_result(999, 1, DedupExecutionActionResult.SUCCESS)

    db.add.assert_not_called()


def test_record_action_result_raises_for_non_running_execution() -> None:
    db = MagicMock()
    execution = _execution(status=DedupExecutionStatus.COMPLETED)
    service = DedupExecutionService(db)
    service.get_execution = MagicMock(return_value=execution)

    with pytest.raises(ValueError, match="is not RUNNING"):
        service.record_action_result(1, 1, DedupExecutionActionResult.SUCCESS)

    db.add.assert_not_called()


def test_record_action_result_raises_for_missing_plan_action() -> None:
    db = MagicMock()
    execution = _execution()
    service = DedupExecutionService(db)
    service.get_execution = MagicMock(return_value=execution)
    db.get.return_value = None

    with pytest.raises(ValueError, match="not found"):
        service.record_action_result(1, 999, DedupExecutionActionResult.SUCCESS)

    db.add.assert_not_called()


def test_record_action_result_raises_for_plan_action_from_different_plan() -> None:
    db = MagicMock()
    execution = _execution(plan_id=1)
    plan_action = _plan_action(plan_id=2)
    service = DedupExecutionService(db)
    service.get_execution = MagicMock(return_value=execution)
    db.get.return_value = plan_action

    with pytest.raises(ValueError, match="belongs to plan"):
        service.record_action_result(1, 1, DedupExecutionActionResult.SUCCESS)

    db.add.assert_not_called()


def test_record_action_result_raises_for_duplicate_recording() -> None:
    db = MagicMock()
    execution = _execution()
    plan_action = _plan_action()
    existing_audit = _audit()
    service = DedupExecutionService(db)
    service.get_execution = MagicMock(return_value=execution)
    db.get.return_value = plan_action
    db.scalar.return_value = existing_audit

    with pytest.raises(ValueError, match="already has a recorded result"):
        service.record_action_result(1, 1, DedupExecutionActionResult.SUCCESS)

    db.add.assert_not_called()


def test_record_action_result_raises_for_precondition_failed_with_mutation() -> None:
    db = MagicMock()
    execution = _execution()
    plan_action = _plan_action()
    service = DedupExecutionService(db)
    service.get_execution = MagicMock(return_value=execution)
    db.get.return_value = plan_action
    db.scalar.return_value = None

    with pytest.raises(ValueError, match="can never report"):
        service.record_action_result(
            1,
            1,
            DedupExecutionActionResult.PRECONDITION_FAILED,
            filesystem_mutation_occurred=True,
        )

    db.add.assert_not_called()


def test_record_action_result_raises_for_not_attempted_with_mutation() -> None:
    db = MagicMock()
    execution = _execution()
    plan_action = _plan_action()
    service = DedupExecutionService(db)
    service.get_execution = MagicMock(return_value=execution)
    db.get.return_value = plan_action
    db.scalar.return_value = None

    with pytest.raises(ValueError, match="can never report"):
        service.record_action_result(
            1,
            1,
            DedupExecutionActionResult.NOT_ATTEMPTED,
            filesystem_mutation_occurred=True,
        )

    db.add.assert_not_called()


def test_record_action_result_raises_for_success_without_mutation() -> None:
    db = MagicMock()
    execution = _execution()
    plan_action = _plan_action()
    service = DedupExecutionService(db)
    service.get_execution = MagicMock(return_value=execution)
    db.get.return_value = plan_action
    db.scalar.return_value = None

    with pytest.raises(ValueError, match="requires filesystem_mutation_occurred=True"):
        service.record_action_result(
            1,
            1,
            DedupExecutionActionResult.SUCCESS,
            filesystem_mutation_occurred=False,
        )

    db.add.assert_not_called()


# --- record_action_result: UNKNOWN (crash-recovery representation) -----


def test_record_action_result_unknown_requires_null_mutation_flag() -> None:
    db = MagicMock()
    execution = _execution()
    plan_action = _plan_action()
    service = DedupExecutionService(db)
    service.get_execution = MagicMock(return_value=execution)
    db.get.return_value = plan_action
    db.scalar.return_value = None

    audit = service.record_action_result(
        1,
        1,
        DedupExecutionActionResult.UNKNOWN,
        filesystem_mutation_occurred=None,
        error_message="executor process did not return; outcome could not be confirmed",
    )

    assert audit.result == DedupExecutionActionResult.UNKNOWN
    assert audit.filesystem_mutation_occurred is None
    assert audit.ended_at is None
    # expected_* still frozen from the plan action, same as every other result.
    assert audit.expected_content_hash == "hash-a"


def test_record_action_result_unknown_rejects_true_mutation_flag() -> None:
    db = MagicMock()
    execution = _execution()
    plan_action = _plan_action()
    service = DedupExecutionService(db)
    service.get_execution = MagicMock(return_value=execution)
    db.get.return_value = plan_action
    db.scalar.return_value = None

    with pytest.raises(ValueError, match="requires filesystem_mutation_occurred=None"):
        service.record_action_result(
            1,
            1,
            DedupExecutionActionResult.UNKNOWN,
            filesystem_mutation_occurred=True,
        )

    db.add.assert_not_called()


def test_record_action_result_unknown_rejects_false_mutation_flag() -> None:
    db = MagicMock()
    execution = _execution()
    plan_action = _plan_action()
    service = DedupExecutionService(db)
    service.get_execution = MagicMock(return_value=execution)
    db.get.return_value = plan_action
    db.scalar.return_value = None

    with pytest.raises(ValueError, match="requires filesystem_mutation_occurred=None"):
        service.record_action_result(
            1,
            1,
            DedupExecutionActionResult.UNKNOWN,
            filesystem_mutation_occurred=False,
        )

    db.add.assert_not_called()


def test_record_action_result_unknown_rejects_ended_at() -> None:
    db = MagicMock()
    execution = _execution()
    plan_action = _plan_action()
    service = DedupExecutionService(db)
    service.get_execution = MagicMock(return_value=execution)
    db.get.return_value = plan_action
    db.scalar.return_value = None

    with pytest.raises(ValueError, match="requires ended_at=None"):
        service.record_action_result(
            1,
            1,
            DedupExecutionActionResult.UNKNOWN,
            filesystem_mutation_occurred=None,
            ended_at=datetime(2026, 9, 12, 12, 0, tzinfo=UTC),
        )

    db.add.assert_not_called()


def test_record_action_result_unknown_allows_started_at() -> None:
    """started_at MAY be set for UNKNOWN if a recovery step has
    independent evidence of when the attempt began - only ended_at is
    forced null."""
    db = MagicMock()
    execution = _execution()
    plan_action = _plan_action()
    service = DedupExecutionService(db)
    service.get_execution = MagicMock(return_value=execution)
    db.get.return_value = plan_action
    db.scalar.return_value = None

    audit = service.record_action_result(
        1,
        1,
        DedupExecutionActionResult.UNKNOWN,
        filesystem_mutation_occurred=None,
        started_at=datetime(2026, 9, 12, 11, 0, tzinfo=UTC),
    )

    assert audit.started_at == datetime(2026, 9, 12, 11, 0, tzinfo=UTC)
    assert audit.ended_at is None


def test_record_action_result_failed_rejects_null_mutation_flag() -> None:
    """Only UNKNOWN may leave filesystem_mutation_occurred indeterminate
    - FAILED must still report a definite True or False."""
    db = MagicMock()
    execution = _execution()
    plan_action = _plan_action()
    service = DedupExecutionService(db)
    service.get_execution = MagicMock(return_value=execution)
    db.get.return_value = plan_action
    db.scalar.return_value = None

    with pytest.raises(ValueError, match="requires a definite filesystem_mutation_occurred"):
        service.record_action_result(
            1,
            1,
            DedupExecutionActionResult.FAILED,
            filesystem_mutation_occurred=None,
        )

    db.add.assert_not_called()


# --- complete_execution: derived overall status ------------------------


def test_complete_execution_all_success_is_completed() -> None:
    db = MagicMock()
    execution = _execution()
    audits = [
        _audit(1, result=DedupExecutionActionResult.SUCCESS),
        _audit(2, plan_action_id=2, result=DedupExecutionActionResult.SUCCESS),
    ]
    service = DedupExecutionService(db)
    service.get_execution = MagicMock(return_value=execution)
    db.scalars.side_effect = [[1, 2], audits]

    result = service.complete_execution(1)

    assert result.status == DedupExecutionStatus.COMPLETED
    assert result.failure_reason is None
    assert result.ended_at is not None
    db.commit.assert_called_once()


def test_complete_execution_zero_success_is_failed() -> None:
    db = MagicMock()
    execution = _execution()
    audits = [_audit(1, result=DedupExecutionActionResult.FAILED)]
    service = DedupExecutionService(db)
    service.get_execution = MagicMock(return_value=execution)
    db.scalars.side_effect = [[1], audits]

    result = service.complete_execution(1)

    assert result.status == DedupExecutionStatus.FAILED
    assert result.failure_reason is not None
    assert "failed" in result.failure_reason


def test_complete_execution_partial_success_is_partially_completed() -> None:
    """The worked example from the spec: some actions succeeded before
    execution stopped due to a failure - distinct from FAILED (zero
    successes) and COMPLETED (zero failures)."""
    db = MagicMock()
    execution = _execution()
    audits = [
        _audit(1, plan_action_id=1, result=DedupExecutionActionResult.SUCCESS),
        _audit(2, plan_action_id=2, result=DedupExecutionActionResult.SUCCESS),
        _audit(3, plan_action_id=3, result=DedupExecutionActionResult.FAILED),
        _audit(4, plan_action_id=4, result=DedupExecutionActionResult.NOT_ATTEMPTED),
        _audit(5, plan_action_id=5, result=DedupExecutionActionResult.NOT_ATTEMPTED),
    ]
    service = DedupExecutionService(db)
    service.get_execution = MagicMock(return_value=execution)
    db.scalars.side_effect = [[1, 2, 3, 4, 5], audits]

    result = service.complete_execution(1)

    assert result.status == DedupExecutionStatus.PARTIALLY_COMPLETED
    assert "plan action 3" in result.failure_reason


def test_complete_execution_any_unknown_result_is_needs_review() -> None:
    """NEEDS_REVIEW takes priority over every other classification,
    regardless of how many other actions cleanly succeeded - this
    system must never describe an execution as COMPLETED/FAILED/
    PARTIALLY_COMPLETED while part of its story is genuinely unknown."""
    db = MagicMock()
    execution = _execution()
    audits = [
        _audit(1, plan_action_id=1, result=DedupExecutionActionResult.SUCCESS),
        _audit(2, plan_action_id=2, result=DedupExecutionActionResult.SUCCESS),
        _audit(3, plan_action_id=3, result=DedupExecutionActionResult.UNKNOWN),
    ]
    service = DedupExecutionService(db)
    service.get_execution = MagicMock(return_value=execution)
    db.scalars.side_effect = [[1, 2, 3], audits]

    result = service.complete_execution(1)

    assert result.status == DedupExecutionStatus.NEEDS_REVIEW
    assert "plan action 3" in result.failure_reason
    assert "unknown" in result.failure_reason.lower()


def test_complete_execution_unknown_takes_priority_over_failed() -> None:
    """Even when zero actions succeeded, a single UNKNOWN result still
    yields NEEDS_REVIEW, never FAILED - FAILED specifically means "we
    know for certain nothing succeeded," which isn't true here."""
    db = MagicMock()
    execution = _execution()
    audits = [
        _audit(1, plan_action_id=1, result=DedupExecutionActionResult.UNKNOWN),
    ]
    service = DedupExecutionService(db)
    service.get_execution = MagicMock(return_value=execution)
    db.scalars.side_effect = [[1], audits]

    result = service.complete_execution(1)

    assert result.status == DedupExecutionStatus.NEEDS_REVIEW


def test_complete_execution_raises_for_missing_execution() -> None:
    db = MagicMock()
    service = DedupExecutionService(db)
    service.get_execution = MagicMock(return_value=None)

    with pytest.raises(ValueError, match="not found"):
        service.complete_execution(999)


def test_complete_execution_raises_for_already_finalized() -> None:
    db = MagicMock()
    execution = _execution(status=DedupExecutionStatus.COMPLETED)
    service = DedupExecutionService(db)
    service.get_execution = MagicMock(return_value=execution)

    with pytest.raises(ValueError, match="is not RUNNING"):
        service.complete_execution(1)


def test_complete_execution_raises_for_incomplete_audit_trail() -> None:
    db = MagicMock()
    execution = _execution()
    audits = [_audit(1, plan_action_id=1, result=DedupExecutionActionResult.SUCCESS)]
    service = DedupExecutionService(db)
    service.get_execution = MagicMock(return_value=execution)
    # Plan has 3 actions, only 1 has a recorded result.
    db.scalars.side_effect = [[1, 2, 3], audits]

    with pytest.raises(ValueError, match="cannot be finalized"):
        service.complete_execution(1)

    db.commit.assert_not_called()


# --- listing -------------------------------------------------------------


def test_list_executions_filters_by_plan_id_and_status() -> None:
    db = MagicMock()
    service = DedupExecutionService(db)

    service.list_executions(plan_id=1, status=DedupExecutionStatus.RUNNING)

    statement = db.scalars.call_args.args[0]
    compiled = str(statement.compile(compile_kwargs={"literal_binds": False}))
    assert "dedup_executions.plan_id" in compiled
    assert "dedup_executions.status" in compiled


def test_list_executions_filters_by_started_before() -> None:
    db = MagicMock()
    service = DedupExecutionService(db)

    service.list_executions(started_before=datetime(2026, 9, 12, tzinfo=UTC))

    statement = db.scalars.call_args.args[0]
    compiled = str(statement.compile(compile_kwargs={"literal_binds": False}))
    assert "dedup_executions.started_at <" in compiled


# --- recover_stale_execution ---------------------------------------------


def test_recover_stale_execution_raises_for_missing_execution() -> None:
    db = MagicMock()
    db.execute.return_value.scalar_one_or_none.return_value = None
    service = DedupExecutionService(db)

    with pytest.raises(ValueError, match="not found"):
        service.recover_stale_execution(999)


def test_recover_stale_execution_raises_for_non_running_execution() -> None:
    db = MagicMock()
    execution = _execution(status=DedupExecutionStatus.COMPLETED)
    db.execute.return_value.scalar_one_or_none.return_value = execution
    service = DedupExecutionService(db)

    with pytest.raises(ValueError, match="is not RUNNING"):
        service.recover_stale_execution(1)


def test_recover_stale_execution_raises_for_already_claimed_recovery() -> None:
    """Recovery locking parity with `_claim_execution`: a second
    concurrent recovery attempt on the same execution must refuse
    cleanly, not raise a raw IntegrityError."""
    db = MagicMock()
    execution = _execution()
    execution.recovery_claimed_at = datetime(2026, 9, 12, 11, 0, tzinfo=UTC)
    db.execute.return_value.scalar_one_or_none.return_value = execution
    service = DedupExecutionService(db)

    with pytest.raises(ValueError, match="already being recovered"):
        service.recover_stale_execution(1)


def test_recover_stale_execution_raises_when_nothing_to_recover() -> None:
    db = MagicMock()
    execution = _execution()
    plan_action = _plan_action()
    audit = _audit(plan_action_id=plan_action.id)
    db.execute.return_value.scalar_one_or_none.return_value = execution
    service = DedupExecutionService(db)
    service.get_action_audits = MagicMock(return_value=[audit])
    db.scalars.return_value = [plan_action]

    with pytest.raises(ValueError, match="nothing to recover"):
        service.recover_stale_execution(1)


def test_recover_stale_execution_records_not_attempted_for_unchanged_file(
    tmp_path,
) -> None:
    """The one case recovery can confidently classify without a guess:
    a file that still exists with exactly the hash/size the plan
    expected before any mutation could not possibly have been deleted -
    the mutation demonstrably did not happen."""
    dup_file = tmp_path / "dup.txt"
    dup_file.write_text("hello world")
    import hashlib

    content_hash = hashlib.sha256(b"hello world").hexdigest()

    execution = _execution()
    plan_action = _plan_action()
    plan_action.source_path = str(dup_file)
    plan_action.observed_exists = True
    plan_action.observed_content_hash = content_hash
    plan_action.observed_file_size = 11

    service = DedupExecutionService(db := MagicMock())
    db.execute.return_value.scalar_one_or_none.return_value = execution
    service.get_action_audits = MagicMock(return_value=[])
    db.scalars.return_value = [plan_action]
    service.record_action_result = MagicMock()
    service.complete_execution = MagicMock(return_value="finalized")

    result = service.recover_stale_execution(1)

    service.record_action_result.assert_called_once_with(
        1, plan_action.id, DedupExecutionActionResult.NOT_ATTEMPTED
    )
    service.complete_execution.assert_called_once_with(1)
    assert result == "finalized"


def test_recover_stale_execution_records_unknown_for_missing_file(tmp_path) -> None:
    missing_path = str(tmp_path / "gone.txt")

    execution = _execution()
    plan_action = _plan_action()
    plan_action.source_path = missing_path
    plan_action.observed_exists = True
    plan_action.observed_content_hash = "hash-a"
    plan_action.observed_file_size = 11

    service = DedupExecutionService(db := MagicMock())
    db.execute.return_value.scalar_one_or_none.return_value = execution
    service.get_action_audits = MagicMock(return_value=[])
    db.scalars.return_value = [plan_action]
    service.record_action_result = MagicMock()
    service.complete_execution = MagicMock(return_value="finalized")

    service.recover_stale_execution(1)

    call = service.record_action_result.call_args
    assert call.args == (1, plan_action.id, DedupExecutionActionResult.UNKNOWN)
    assert call.kwargs["filesystem_mutation_occurred"] is None
    assert call.kwargs["observed_content_hash"] is None
    assert "missing" in call.kwargs["error_message"]


def test_recover_stale_execution_records_unknown_for_changed_file(tmp_path) -> None:
    dup_file = tmp_path / "dup.txt"
    dup_file.write_text("something completely different")

    execution = _execution()
    plan_action = _plan_action()
    plan_action.source_path = str(dup_file)
    plan_action.observed_exists = True
    plan_action.observed_content_hash = "hash-a"
    plan_action.observed_file_size = 11

    service = DedupExecutionService(db := MagicMock())
    db.execute.return_value.scalar_one_or_none.return_value = execution
    service.get_action_audits = MagicMock(return_value=[])
    db.scalars.return_value = [plan_action]
    service.record_action_result = MagicMock()
    service.complete_execution = MagicMock(return_value="finalized")

    service.recover_stale_execution(1)

    call = service.record_action_result.call_args
    assert call.args == (1, plan_action.id, DedupExecutionActionResult.UNKNOWN)
    assert call.kwargs["filesystem_mutation_occurred"] is None
    assert call.kwargs["observed_content_hash"] is not None
    assert "not matching" in call.kwargs["error_message"]


def test_recover_stale_execution_only_recovers_unresolved_actions(tmp_path) -> None:
    """An action that already has a recorded outcome is left alone -
    recovery only fills in the gaps, never touches an existing row."""
    dup_file = tmp_path / "dup.txt"
    dup_file.write_text("hello world")

    execution = _execution()
    already_resolved = _plan_action(action_id=1)
    unresolved = _plan_action(action_id=2)
    unresolved.source_path = str(dup_file)
    existing_audit = _audit(plan_action_id=1, result=DedupExecutionActionResult.SUCCESS)

    service = DedupExecutionService(db := MagicMock())
    db.execute.return_value.scalar_one_or_none.return_value = execution
    service.get_action_audits = MagicMock(return_value=[existing_audit])
    db.scalars.return_value = [already_resolved, unresolved]
    service.record_action_result = MagicMock()
    service.complete_execution = MagicMock(return_value="finalized")

    service.recover_stale_execution(1)

    service.record_action_result.assert_called_once()
    assert service.record_action_result.call_args.args[1] == unresolved.id


def test_get_action_audits_returns_empty_for_no_audits() -> None:
    db = MagicMock()
    db.scalars.return_value = []
    service = DedupExecutionService(db)

    assert service.get_action_audits(1) == []
