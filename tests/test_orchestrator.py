from dataclasses import replace
from datetime import datetime, timedelta

from jenga.core.decisions.decision_gateway import DecisionGateway
from jenga.core.orchestration.orchestrator import Orchestrator
from jenga.core.state.workflow_state import WorkflowInstance, WorkflowStatus


class InMemoryWorkflowRepository:
    def __init__(self, workflows: list[WorkflowInstance]):
        self.workflows = {workflow.id: workflow for workflow in workflows}
        self.history: list[dict] = []

    def get_by_id(self, workflow_id: int, business_id: int):
        workflow = self.workflows.get(workflow_id)
        if workflow and workflow.business_id == business_id:
            return workflow
        return None

    def save(self, workflow: WorkflowInstance):
        self.workflows[workflow.id] = workflow
        return workflow

    def get_active_workflows(self, business_id: int, from_time=None, to_time=None):
        return [
            workflow
            for workflow in self.workflows.values()
            if workflow.business_id == business_id and workflow.is_active
        ]

    def get_cascade_candidates(self, business_id: int, after_time: datetime, min_risk: float):
        return [
            workflow
            for workflow in self.workflows.values()
            if workflow.business_id == business_id
            and workflow.is_active
            and workflow.appointment_time > after_time
            and workflow.risk_score >= min_risk
        ]

    def get_conflicting_workflows(
        self,
        business_id: int,
        start_time: datetime,
        duration_minutes: int,
        exclude_workflow_id=None,
    ):
        new_end = start_time + timedelta(minutes=duration_minutes)
        conflicts = []
        for workflow in self.workflows.values():
            if workflow.business_id != business_id or not workflow.is_active:
                continue
            if exclude_workflow_id is not None and workflow.id == exclude_workflow_id:
                continue
            existing_end = workflow.appointment_time + timedelta(
                minutes=workflow.duration_minutes
            )
            if workflow.appointment_time < new_end and existing_end > start_time:
                conflicts.append(workflow)
        return conflicts

    def update_time(self, workflow_id: int, new_time: datetime, business_id: int = None):
        current = self.workflows[workflow_id]
        updated = replace(
            current,
            appointment_time=new_time,
            move_count=current.move_count + 1,
        )
        self.workflows[workflow_id] = updated
        return updated

    def record_cascade(self, **entry):
        self.history.append(entry)


class InMemoryClientRepository:
    def __init__(self):
        self.cancelled_clients: list[int] = []

    def get_client_data(self, client_id: int):
        return {
            "segment": "regular",
            "is_flexible": True,
            "no_show_rate": 0.3,
            "cancellation_rate": 0.1,
            "total_appointments": 10,
        }

    def update_stats_on_completion(self, client_id: int):
        return None

    def update_stats_on_cancellation(self, client_id: int):
        self.cancelled_clients.append(client_id)

    def update_stats_on_no_show(self, client_id: int):
        return None


def workflow(
    workflow_id: int,
    client_id: int,
    appointment_time: datetime,
    risk_score: float,
) -> WorkflowInstance:
    return WorkflowInstance(
        id=workflow_id,
        business_id=1,
        client_id=client_id,
        status=WorkflowStatus.SCHEDULED,
        appointment_time=appointment_time,
        duration_minutes=30,
        risk_score=risk_score,
        is_movable=True,
        move_count=0,
    )


def test_cancellation_moves_best_candidate_into_short_term_capacity() -> None:
    now = datetime.utcnow()
    cancelled = workflow(1, 101, now + timedelta(hours=24), 0.2)
    candidate = workflow(2, 102, now + timedelta(days=3), 0.9)
    repository = InMemoryWorkflowRepository([cancelled, candidate])
    clients = InMemoryClientRepository()
    orchestrator = Orchestrator(
        workflow_repository=repository,
        client_repository=clients,
        decision_gateway=DecisionGateway(max_cascade_depth=3),
    )

    result = orchestrator.cancel_workflow(1, business_id=1, trigger_cascade=True)

    assert result.success
    assert result.workflow.status is WorkflowStatus.CANCELLED
    assert result.metadata["moves_count"] == 1
    assert repository.workflows[2].appointment_time.date() == cancelled.appointment_time.date()
    assert repository.workflows[2].move_count == 1
    assert repository.history[0]["moved_workflow_id"] == 2


def test_business_boundary_hides_workflow() -> None:
    appointment = workflow(1, 101, datetime.utcnow() + timedelta(days=2), 0.5)
    orchestrator = Orchestrator(
        workflow_repository=InMemoryWorkflowRepository([appointment]),
        client_repository=InMemoryClientRepository(),
    )

    result = orchestrator.cancel_workflow(1, business_id=999)

    assert not result.success
    assert result.error == "Workflow 1 not found"
