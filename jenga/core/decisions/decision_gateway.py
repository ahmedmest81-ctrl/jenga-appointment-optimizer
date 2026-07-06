"""
Decision Gateway

Routes decisions for cascade optimization and risk assessment.
ML/heuristics are ADVISORY ONLY - they suggest, never execute.

The orchestrator makes final decisions based on:
1. Advisory input (ML suggestions)
2. Business rules (configuration)
3. State constraints (validator)

Cloud-neutral: No ML framework dependencies in core.
"""

from dataclasses import dataclass
from typing import List, Optional, Dict, Any, Protocol
from datetime import datetime

from jenga.core.time_utils import ensure_naive_utc, utc_now
from enum import Enum

from jenga.core.state.workflow_state import WorkflowInstance, WorkflowStatus


class RiskLevel(str, Enum):
    """Risk levels for appointment no-show prediction"""
    LOW = "low"
    MEDIUM = "medium"
    HIGH = "high"


@dataclass(frozen=True)
class RiskAssessment:
    """
    Immutable risk assessment result.

    This is what advisors return - never a direct action.
    """
    workflow_id: int
    risk_score: float
    risk_level: RiskLevel
    confidence: float
    factors: Dict[str, float]
    model_version: str
    calculated_at: datetime


@dataclass(frozen=True)
class CascadeCandidate:
    """
    A candidate for cascade move.

    Represents an appointment that COULD be moved earlier.
    The orchestrator decides whether to actually move it.
    """
    workflow_id: int
    current_time: datetime
    target_time: datetime
    priority_score: float
    risk_score: float
    move_count: int
    is_flexible: bool


@dataclass(frozen=True)
class CascadeDecision:
    """
    The orchestrator's decision on a cascade.

    This is a COMMAND that will be executed.
    """
    approved: bool
    candidates: List[CascadeCandidate]
    reason: str
    max_moves: int = 0


class RiskAdvisor(Protocol):
    """
    Protocol for risk advisors.

    Advisors MUST implement this interface.
    They can read workflow state and produce suggestions.
    They CANNOT execute anything or mutate state.
    """

    def assess_risk(
        self,
        workflow: WorkflowInstance,
        client_data: Dict[str, Any]
    ) -> RiskAssessment:
        """
        Assess no-show risk for a workflow.

        Args:
            workflow: The workflow instance to assess
            client_data: Client behavioral data (read-only)

        Returns:
            RiskAssessment with score and factors
        """
        ...

    def calculate_flexibility(
        self,
        workflow: WorkflowInstance,
        client_data: Dict[str, Any]
    ) -> float:
        """
        Calculate how flexible/movable a workflow is.

        Args:
            workflow: The workflow instance
            client_data: Client behavioral data (read-only)

        Returns:
            Flexibility score between 0.0 and 1.0
        """
        ...


class DecisionGateway:
    """
    Central gateway for all orchestration decisions.

    Routes decisions through:
    1. Advisory layer (ML suggestions)
    2. Business rules (thresholds, limits)
    3. State validation

    The gateway COORDINATES decisions but does not EXECUTE them.
    Execution is the orchestrator's responsibility.
    """

    def __init__(
        self,
        risk_advisor: Optional[RiskAdvisor] = None,
        high_risk_threshold: float = 0.7,
        medium_risk_threshold: float = 0.4,
        max_cascade_depth: int = 10
    ):
        """
        Initialize decision gateway.

        Args:
            risk_advisor: Optional ML-based risk advisor
            high_risk_threshold: Threshold for high risk
            medium_risk_threshold: Threshold for medium risk
            max_cascade_depth: Maximum cascade moves allowed
        """
        self._risk_advisor = risk_advisor
        self._high_risk_threshold = high_risk_threshold
        self._medium_risk_threshold = medium_risk_threshold
        self._max_cascade_depth = max_cascade_depth

    @property
    def max_cascade_depth(self) -> int:
        """Public read access for orchestration (no private attribute reach-in)."""
        return self._max_cascade_depth

    def set_risk_advisor(self, advisor: RiskAdvisor) -> None:
        """Set or replace the risk advisor (for testing/configuration)"""
        self._risk_advisor = advisor

    def assess_workflow_risk(
        self,
        workflow: WorkflowInstance,
        client_data: Dict[str, Any]
    ) -> RiskAssessment:
        """
        Get risk assessment for a workflow.

        If no advisor is configured, returns neutral assessment.
        This ensures the system works without ML.
        """
        if self._risk_advisor is None:
            # No advisor - return neutral risk
            return RiskAssessment(
                workflow_id=workflow.id,
                risk_score=0.5,
                risk_level=RiskLevel.MEDIUM,
                confidence=0.0,
                factors={},
                model_version="none",
                calculated_at=utc_now()
            )

        assessment = self._risk_advisor.assess_risk(workflow, client_data)
        return assessment

    def classify_risk_level(self, risk_score: float) -> RiskLevel:
        """Classify risk score into level based on thresholds"""
        if risk_score >= self._high_risk_threshold:
            return RiskLevel.HIGH
        elif risk_score >= self._medium_risk_threshold:
            return RiskLevel.MEDIUM
        else:
            return RiskLevel.LOW

    def evaluate_cascade_candidates(
        self,
        cancelled_slot: datetime,
        candidates: List[WorkflowInstance],
        client_data_map: Dict[int, Dict[str, Any]]
    ) -> CascadeDecision:
        """
        Evaluate candidates for cascade optimization.

        This method SUGGESTS which candidates to move.
        The orchestrator makes the final decision.

        Args:
            cancelled_slot: Time of the cancelled appointment
            candidates: List of potential workflows to move
            client_data_map: Map of client_id → client data

        Returns:
            CascadeDecision with approved candidates
        """
        if not candidates:
            return CascadeDecision(
                approved=False,
                candidates=[],
                reason="No candidates available",
                max_moves=0
            )

        scored_candidates = []

        for workflow in candidates:
            # Skip if not movable
            if not workflow.is_movable:
                continue

            # Skip if in terminal state
            if workflow.status in (
                WorkflowStatus.CANCELLED,
                WorkflowStatus.COMPLETED,
                WorkflowStatus.NO_SHOW
            ):
                continue

            # Get client data
            client_data = client_data_map.get(workflow.client_id, {})

            # Calculate flexibility score
            if self._risk_advisor:
                flexibility = self._risk_advisor.calculate_flexibility(
                    workflow, client_data
                )
            else:
                # Default flexibility based on move count
                flexibility = max(0.0, 1.0 - (workflow.move_count * 0.2))

            # Priority score: high risk + high flexibility = better candidate
            priority = workflow.risk_score * flexibility

            scored_candidates.append(CascadeCandidate(
                workflow_id=workflow.id,
                current_time=workflow.appointment_time,
                target_time=cancelled_slot,
                priority_score=priority,
                risk_score=workflow.risk_score,
                move_count=workflow.move_count,
                is_flexible=client_data.get("is_flexible", True)
            ))

        # Sort by priority (highest first)
        scored_candidates.sort(key=lambda c: c.priority_score, reverse=True)

        # Limit to max cascade depth
        approved_candidates = scored_candidates[:self._max_cascade_depth]

        return CascadeDecision(
            approved=len(approved_candidates) > 0,
            candidates=approved_candidates,
            reason=f"Found {len(approved_candidates)} suitable candidates",
            max_moves=len(approved_candidates)
        )

    def should_trigger_cascade(
        self,
        cancelled_workflow: WorkflowInstance
    ) -> bool:
        """
        Determine if cascade should be triggered for cancellation.

        Business rule: Only trigger cascade if there's time value
        to recover (appointment is in the future).
        """
        return ensure_naive_utc(cancelled_workflow.appointment_time) > utc_now()
