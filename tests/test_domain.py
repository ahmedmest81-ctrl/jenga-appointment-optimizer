from datetime import datetime, timedelta

import pytest

from jenga.core.state.workflow_state import (
    InvalidTransitionError,
    WorkflowInstance,
    WorkflowStatus,
)


def make_workflow(status: WorkflowStatus = WorkflowStatus.SCHEDULED) -> WorkflowInstance:
    return WorkflowInstance(
        id=1,
        business_id=10,
        client_id=20,
        status=status,
        appointment_time=datetime.utcnow() + timedelta(days=2),
        duration_minutes=30,
        risk_score=0.4,
        is_movable=True,
        move_count=0,
    )


def test_workflow_transition_is_immutable() -> None:
    scheduled = make_workflow()
    confirmed = scheduled.with_status(WorkflowStatus.CONFIRMED)

    assert scheduled.status is WorkflowStatus.SCHEDULED
    assert confirmed.status is WorkflowStatus.CONFIRMED


def test_invalid_transition_is_rejected() -> None:
    scheduled = make_workflow()

    with pytest.raises(InvalidTransitionError):
        scheduled.with_status(WorkflowStatus.COMPLETED)


def test_terminal_workflow_cannot_transition() -> None:
    cancelled = make_workflow(WorkflowStatus.CANCELLED)

    with pytest.raises(InvalidTransitionError):
        cancelled.with_status(WorkflowStatus.CONFIRMED)


def test_risk_score_is_clamped() -> None:
    workflow = make_workflow().with_risk_score(1.7)

    assert workflow.risk_score == 1.0
