"""
Jenga Orchestrator

The CENTRAL ORCHESTRATION KERNEL.

All workflow execution flows through this orchestrator.
External systems (schedulers, APIs, calendars) only REQUEST execution.
Only the orchestrator EXECUTES and MUTATES state.

Design Principles:
- Single entry point for all workflow operations
- Deterministic execution
- Cloud-neutral (no external dependencies)
- Adapters translate to/from external systems
"""

from typing import Optional, Dict, Any, List, Protocol
from datetime import datetime, timedelta
from dataclasses import dataclass
from enum import Enum
import logging
import uuid

from jenga.core.time_utils import ensure_naive_utc, utc_now
from jenga.core.state.workflow_state import (
    WorkflowInstance,
    WorkflowStatus,
    StateTransitionValidator,
    TemporalValidator,
    InvalidTransitionError,
    PastAppointmentError,
    OutOfWindowError,
    OverlapError
)
from jenga.core.events.domain_events import (
    DomainEvent,
    EventType,
    WorkflowCreatedEvent,
    WorkflowCancelledEvent,
    WorkflowCompletedEvent,
    WorkflowNoShowEvent,
    RiskCalculatedEvent,
    CascadeTriggeredEvent,
    CascadeCompletedEvent,
    SlotReassignedEvent,
    AppointmentRescheduledEvent,
    OptimizationCompletedEvent,
    EarlierSlotOfferedEvent,
    OfferAcceptedEvent,
    OfferDeclinedEvent,
    OfferExpiredEvent,
    SlotAvailableInFutureEvent,
    event_bus
)
from jenga.core.decisions.decision_gateway import (
    DecisionGateway,
    RiskAssessment,
    CascadeDecision,
    RiskAdvisor
)

logger = logging.getLogger(__name__)


class WorkflowRepository(Protocol):
    """
    Protocol for workflow persistence.

    Adapters implement this to provide storage.
    Core does not know about SQLAlchemy, MongoDB, etc.
    """

    def get_by_id(self, workflow_id: int, business_id: int) -> Optional[WorkflowInstance]:
        """Get workflow by ID"""
        ...

    def save(self, workflow: WorkflowInstance) -> WorkflowInstance:
        """Save workflow (create or update)"""
        ...

    def get_active_workflows(
        self,
        business_id: int,
        from_time: Optional[datetime] = None,
        to_time: Optional[datetime] = None
    ) -> List[WorkflowInstance]:
        """Get active (scheduled/confirmed) workflows"""
        ...

    def get_cascade_candidates(
        self,
        business_id: int,
        after_time: datetime,
        min_risk: float
    ) -> List[WorkflowInstance]:
        """Get potential cascade candidates"""
        ...

    def update_time(
        self,
        workflow_id: int,
        new_time: datetime,
        business_id: int
    ) -> WorkflowInstance:
        """
        Update workflow appointment time (for cascade moves).

        Tenant-scoped: business_id is REQUIRED so a repository can never
        mutate another tenant's workflow. Increments move_count automatically.
        """
        ...

    def get_conflicting_workflows(
        self,
        business_id: int,
        start_time: datetime,
        duration_minutes: int,
        exclude_workflow_id: Optional[int] = None
    ) -> List[WorkflowInstance]:
        """
        Get active workflows overlapping [start_time, start_time + duration).

        Used to guarantee no move or offer acceptance double-books a slot.
        """
        ...

    def record_cascade(
        self,
        business_id: int,
        trigger_workflow_id: int,
        moved_workflow_id: int,
        from_time: datetime,
        to_time: datetime,
        depth: int,
        score: float
    ) -> None:
        """Record a cascade move in history for audit/analytics."""
        ...


class ClientRepository(Protocol):
    """Protocol for client data access"""

    def get_client_data(self, client_id: int) -> Dict[str, Any]:
        """Get client behavioral data for risk assessment"""
        ...

    def update_stats_on_completion(self, client_id: int) -> None:
        """Update client stats when appointment completes"""
        ...

    def update_stats_on_cancellation(self, client_id: int) -> None:
        """Update client stats when appointment cancelled"""
        ...

    def update_stats_on_no_show(self, client_id: int) -> None:
        """Update client stats when marked no-show"""
        ...


class TimeWindow(str, Enum):
    """
    Time window classification for cancelled slots.

    Determines orchestrator behavior based on urgency.
    """
    SHORT_TERM = "short_term"    # < 48 hours: urgent, auto-move or urgent offer
    MEDIUM_TERM = "medium_term"  # 7-14 days: offer earlier slots
    LONG_TERM = "long_term"      # >= 14 days: notify wishlist, no immediate cascade


@dataclass(frozen=True)
class Offer:
    """
    Immutable offer representation within the orchestrator.

    This is the core domain object - adapters translate to/from storage.
    """
    offer_id: str
    business_id: int
    workflow_id: int  # The appointment being offered to move
    client_id: int
    from_time: datetime  # Current appointment time
    to_time: datetime    # Offered earlier slot
    expires_at: datetime
    time_window: TimeWindow
    trigger_workflow_id: Optional[int] = None  # The cancelled appointment
    priority_score: float = 0.0
    status: str = "offered"  # offered, accepted, declined, expired


class OfferRepository(Protocol):
    """
    Protocol for offer persistence.

    Adapters implement this to store/retrieve shift offers.
    """

    def create_offer(self, offer: Offer) -> Offer:
        """Create a new offer. Returns offer with assigned ID if needed."""
        ...

    def get_offer(self, offer_id: str, business_id: int) -> Optional[Offer]:
        """Get offer by ID."""
        ...

    def get_active_offer_for_workflow(
        self,
        workflow_id: int,
        business_id: int
    ) -> Optional[Offer]:
        """Get active (non-expired, non-responded) offer for a workflow."""
        ...

    def get_active_offers_for_slot(
        self,
        slot_time: datetime,
        business_id: int
    ) -> List[Offer]:
        """Get all active offers for a given slot time."""
        ...

    def update_offer_status(
        self,
        offer_id: str,
        status: str,
        responded_at: Optional[datetime] = None
    ) -> Offer:
        """Update offer status (accepted, declined, expired)."""
        ...

    def count_offers_for_slot(
        self,
        slot_time: datetime,
        business_id: int,
        trigger_workflow_id: int
    ) -> int:
        """Count how many offers have been made for this slot."""
        ...

    def get_expired_offers(
        self,
        business_id: int,
        current_time: datetime
    ) -> List["Offer"]:
        """Get offers past expires_at that are still in 'offered' status."""
        ...

    def get_offers_for_trigger(
        self,
        trigger_workflow_id: int,
        business_id: int
    ) -> List["Offer"]:
        """Get all offers (any status) created for a given cancelled slot."""
        ...


@dataclass
class ExecutionResult:
    """Result of an orchestration operation"""
    success: bool
    workflow: Optional[WorkflowInstance]
    events: List[DomainEvent]
    error: Optional[str] = None
    metadata: Dict[str, Any] = None

    def __post_init__(self):
        if self.metadata is None:
            self.metadata = {}


class Orchestrator:
    """
    The Jenga Orchestration Kernel.

    This is the SINGLE ENTRY POINT for all workflow operations.
    External systems must go through the orchestrator.

    INVARIANTS:
    - Immutable after construction (no setters)
    - All workflow operations MUST use public methods
    - Adapters cannot access internal state
    - State transitions validated before persistence

    Responsibilities:
    - Validate state transitions
    - Execute workflow operations
    - Coordinate with advisory layer (ML)
    - Emit domain events
    - Delegate persistence to adapters

    Non-responsibilities (handled by adapters):
    - Scheduling
    - HTTP/API handling
    - Database queries
    - Notifications
    - External integrations
    """

    def __init__(
        self,
        workflow_repository: WorkflowRepository,
        client_repository: ClientRepository,
        decision_gateway: Optional[DecisionGateway] = None,
        temporal_validator: Optional[TemporalValidator] = None,
        offer_repository: Optional[OfferRepository] = None,
        time_window_config: Optional[Dict[str, Any]] = None,
        consent_config: Optional[Dict[str, Any]] = None
    ):
        """
        Initialize orchestrator.

        All dependencies MUST be provided at construction.
        The orchestrator is effectively immutable after init.

        Args:
            workflow_repository: Storage adapter for workflows
            client_repository: Storage adapter for clients
            decision_gateway: Decision routing (optional)
            temporal_validator: Temporal validation (optional)
            offer_repository: Storage adapter for offers (optional, enables offer flow)
            time_window_config: Time window thresholds (optional)
            consent_config: Consent policy config (optional)
        """
        # Private attributes - do not access from outside
        self._workflows = workflow_repository
        self._clients = client_repository
        self._gateway = decision_gateway or DecisionGateway()
        self._temporal = temporal_validator or TemporalValidator()
        self._offers = offer_repository  # May be None if offer flow not enabled
        self._event_bus = event_bus

        # Time window configuration (with defaults)
        tw_config = time_window_config or {}
        self._long_term_days = tw_config.get("long_term_days", 14)
        self._medium_term_days = tw_config.get("medium_term_days", 7)
        self._short_term_hours = tw_config.get("short_term_hours", 48)
        self._long_term_action = tw_config.get("long_term_action", "notify")
        self._medium_term_action = tw_config.get("medium_term_action", "offer")
        self._short_term_action = tw_config.get("short_term_action", "auto_move")
        self._medium_term_offer_expiry_hours = tw_config.get("medium_term_offer_expiry_hours", 24)
        self._short_term_offer_expiry_minutes = tw_config.get("short_term_offer_expiry_minutes", 30)

        # Consent configuration (with defaults)
        c_config = consent_config or {}
        self._require_consent_for_moves = c_config.get("require_consent_for_moves", False)
        self._vip_always_offer = c_config.get("vip_always_offer", True)
        self._max_offers_per_slot = c_config.get("max_offers_per_slot", 3)
        self._offer_timeout_action = c_config.get("offer_timeout_action", "next_candidate")

    # NOTE: No setters. Configuration is done at construction time only.
    # This prevents runtime mutation that could break invariants.

    # ===== Workflow Operations =====

    def create_workflow(
        self,
        business_id: int,
        client_id: int,
        appointment_time: datetime,
        duration_minutes: int,
        window_days: int = 30,
        is_movable: bool = True,
        current_time: Optional[datetime] = None
    ) -> ExecutionResult:
        """
        Create a new workflow instance.

        This is the ONLY way to create workflows.

        Args:
            business_id: Business identifier
            client_id: Client identifier
            appointment_time: Appointment datetime
            duration_minutes: Duration in minutes
            window_days: Business booking window
            is_movable: Whether appointment can be moved
            current_time: Current time (for testing)

        Returns:
            ExecutionResult with created workflow
        """
        events = []

        try:
            # Normalize to canonical naive UTC (API may send tz-aware times)
            appointment_time = ensure_naive_utc(appointment_time)
            if current_time is not None:
                current_time = ensure_naive_utc(current_time)

            # Validate temporal constraints
            self._temporal.validate_not_in_past(appointment_time, current_time)
            self._temporal.validate_within_window(appointment_time, window_days, current_time)
            self._temporal.validate_duration(duration_minutes)

            # Get client data for risk assessment
            client_data = self._clients.get_client_data(client_id)

            # Create workflow instance
            workflow = WorkflowInstance(
                id=0,  # Will be assigned by repository
                business_id=business_id,
                client_id=client_id,
                status=WorkflowStatus.SCHEDULED,
                appointment_time=appointment_time,
                duration_minutes=duration_minutes,
                risk_score=0.5,  # Neutral initial
                is_movable=is_movable,
                move_count=0
            )

            # Get risk assessment from advisory layer
            assessment = self._gateway.assess_workflow_risk(workflow, client_data)
            workflow = workflow.with_risk_score(assessment.risk_score)

            # Persist
            workflow = self._workflows.save(workflow)

            # Emit events
            created_event = WorkflowCreatedEvent(
                aggregate_id=workflow.id,
                business_id=business_id,
                client_id=client_id,
                appointment_time=appointment_time,
                risk_score=assessment.risk_score
            )
            events.append(created_event)
            self._event_bus.publish(created_event)

            risk_event = RiskCalculatedEvent(
                aggregate_id=workflow.id,
                business_id=business_id,
                risk_score=assessment.risk_score,
                model_version=assessment.model_version,
                factors=assessment.factors
            )
            events.append(risk_event)
            self._event_bus.publish(risk_event)

            logger.info(
                f"Created workflow {workflow.id} for business {business_id} "
                f"(risk: {assessment.risk_score:.3f})"
            )

            return ExecutionResult(
                success=True,
                workflow=workflow,
                events=events
            )

        except (PastAppointmentError, OutOfWindowError) as e:
            logger.warning(f"Workflow creation failed: {e}")
            return ExecutionResult(
                success=False,
                workflow=None,
                events=events,
                error=str(e)
            )

    def cancel_workflow(
        self,
        workflow_id: int,
        business_id: int,
        trigger_cascade: bool = True
    ) -> ExecutionResult:
        """
        Cancel a workflow.

        Args:
            workflow_id: Workflow to cancel
            business_id: Business identifier (for isolation)
            trigger_cascade: Whether to trigger cascade optimization

        Returns:
            ExecutionResult with cancelled workflow
        """
        events = []

        try:
            # Get workflow
            workflow = self._workflows.get_by_id(workflow_id, business_id)
            if workflow is None:
                return ExecutionResult(
                    success=False,
                    workflow=None,
                    events=events,
                    error=f"Workflow {workflow_id} not found"
                )

            # Validate transition
            StateTransitionValidator.validate_transition(
                workflow.status,
                WorkflowStatus.CANCELLED
            )

            # Update state
            cancelled_workflow = workflow.with_status(WorkflowStatus.CANCELLED)
            cancelled_workflow = self._workflows.save(cancelled_workflow)

            # Update client stats
            self._clients.update_stats_on_cancellation(workflow.client_id)

            # Emit cancellation event
            cancel_event = WorkflowCancelledEvent(
                aggregate_id=workflow_id,
                business_id=business_id,
                trigger_cascade=trigger_cascade
            )
            events.append(cancel_event)
            self._event_bus.publish(cancel_event)

            # Handle cascade if requested
            cascade_result = {"moves_count": 0}
            if trigger_cascade and self._gateway.should_trigger_cascade(workflow):
                cascade_result = self._execute_cascade(
                    cancelled_workflow=workflow,
                    business_id=business_id,
                    events=events
                )

            logger.info(
                f"Cancelled workflow {workflow_id} "
                f"(cascade moves: {cascade_result.get('moves_count', 0)})"
            )

            return ExecutionResult(
                success=True,
                workflow=cancelled_workflow,
                events=events,
                metadata=cascade_result
            )

        except InvalidTransitionError as e:
            logger.warning(f"Cannot cancel workflow {workflow_id}: {e}")
            return ExecutionResult(
                success=False,
                workflow=None,
                events=events,
                error=str(e)
            )

    def complete_workflow(
        self,
        workflow_id: int,
        business_id: int
    ) -> ExecutionResult:
        """Mark workflow as completed"""
        events = []

        try:
            workflow = self._workflows.get_by_id(workflow_id, business_id)
            if workflow is None:
                return ExecutionResult(
                    success=False,
                    workflow=None,
                    events=events,
                    error=f"Workflow {workflow_id} not found"
                )

            StateTransitionValidator.validate_transition(
                workflow.status,
                WorkflowStatus.COMPLETED
            )

            completed = workflow.with_status(WorkflowStatus.COMPLETED)
            completed = self._workflows.save(completed)

            self._clients.update_stats_on_completion(workflow.client_id)

            complete_event = WorkflowCompletedEvent(
                aggregate_id=workflow_id,
                business_id=business_id
            )
            events.append(complete_event)
            self._event_bus.publish(complete_event)

            logger.info(f"Completed workflow {workflow_id}")

            return ExecutionResult(
                success=True,
                workflow=completed,
                events=events
            )

        except InvalidTransitionError as e:
            return ExecutionResult(
                success=False,
                workflow=None,
                events=events,
                error=str(e)
            )

    def mark_no_show(
        self,
        workflow_id: int,
        business_id: int
    ) -> ExecutionResult:
        """Mark workflow as no-show"""
        events = []

        try:
            workflow = self._workflows.get_by_id(workflow_id, business_id)
            if workflow is None:
                return ExecutionResult(
                    success=False,
                    workflow=None,
                    events=events,
                    error=f"Workflow {workflow_id} not found"
                )

            StateTransitionValidator.validate_transition(
                workflow.status,
                WorkflowStatus.NO_SHOW
            )

            no_show = workflow.with_status(WorkflowStatus.NO_SHOW)
            no_show = self._workflows.save(no_show)

            self._clients.update_stats_on_no_show(workflow.client_id)
            client_data = self._clients.get_client_data(workflow.client_id)

            no_show_event = WorkflowNoShowEvent(
                aggregate_id=workflow_id,
                business_id=business_id,
                client_no_show_rate=client_data.get("no_show_rate", 0.0)
            )
            events.append(no_show_event)
            self._event_bus.publish(no_show_event)

            logger.info(f"Marked workflow {workflow_id} as no-show")

            return ExecutionResult(
                success=True,
                workflow=no_show,
                events=events
            )

        except InvalidTransitionError as e:
            return ExecutionResult(
                success=False,
                workflow=None,
                events=events,
                error=str(e)
            )

    def confirm_workflow(
        self,
        workflow_id: int,
        business_id: int
    ) -> ExecutionResult:
        """Confirm a scheduled workflow"""
        events = []

        try:
            workflow = self._workflows.get_by_id(workflow_id, business_id)
            if workflow is None:
                return ExecutionResult(
                    success=False,
                    workflow=None,
                    events=events,
                    error=f"Workflow {workflow_id} not found"
                )

            StateTransitionValidator.validate_transition(
                workflow.status,
                WorkflowStatus.CONFIRMED
            )

            confirmed = workflow.with_status(WorkflowStatus.CONFIRMED)
            confirmed = self._workflows.save(confirmed)

            logger.info(f"Confirmed workflow {workflow_id}")

            return ExecutionResult(
                success=True,
                workflow=confirmed,
                events=events
            )

        except InvalidTransitionError as e:
            return ExecutionResult(
                success=False,
                workflow=None,
                events=events,
                error=str(e)
            )

    # ===== Optimization Operations =====

    def recalculate_risk_scores(self, business_id: int) -> ExecutionResult:
        """
        Recalculate risk scores for all active workflows.

        This is typically triggered by a scheduler adapter,
        but the orchestrator owns the execution.
        """
        events = []
        updated_count = 0

        workflows = self._workflows.get_active_workflows(business_id)

        for workflow in workflows:
            try:
                client_data = self._clients.get_client_data(workflow.client_id)
                assessment = self._gateway.assess_workflow_risk(workflow, client_data)

                if workflow.risk_score != assessment.risk_score:
                    updated = workflow.with_risk_score(assessment.risk_score)
                    self._workflows.save(updated)
                    updated_count += 1

                    risk_event = RiskCalculatedEvent(
                        aggregate_id=workflow.id,
                        business_id=business_id,
                        risk_score=assessment.risk_score,
                        model_version=assessment.model_version,
                        factors=assessment.factors
                    )
                    events.append(risk_event)
                    self._event_bus.publish(risk_event)

            except Exception as e:
                logger.error(f"Failed to update risk for workflow {workflow.id}: {e}")

        logger.info(
            f"Recalculated risk scores: {updated_count}/{len(workflows)} "
            f"updated for business {business_id}"
        )

        return ExecutionResult(
            success=True,
            workflow=None,
            events=events,
            metadata={"updated_count": updated_count, "total": len(workflows)}
        )

    def _execute_cascade(
        self,
        cancelled_workflow: WorkflowInstance,
        business_id: int,
        events: List[DomainEvent],
        current_time: Optional[datetime] = None,
        excluded_workflow_ids: Optional[set] = None
    ) -> Dict[str, Any]:
        """
        Execute cascade optimization after cancellation.

        TIME-WINDOW-AWARE CASCADE LOGIC:
        - First classifies the cancelled slot into a time window
        - LONG_TERM (>=14 days): Notify wishlist, no cascade
        - MEDIUM_TERM (7-14 days): Create offers OR auto-move based on config
        - SHORT_TERM (<48 hours): Urgent auto-move OR urgent offers

        DAY-BASED CASCADE LOGIC (when auto-moving):
        - Cascade operates on DAYS, not hours
        - Time-of-day is PRESERVED when moving appointments
        - A day is a capacity bucket (multiple appointments per day)
        - Appointments only move to EARLIER days

        ALGORITHM:
        1. Classify time window of cancelled slot
        2. If LONG_TERM: emit SlotAvailableInFutureEvent and return
        3. If offer flow is used: create offer for best candidate and return
        4. Otherwise (auto-move):
           a. Start with open_day = cancelled appointment's date
           b. Find candidates scheduled on days AFTER open_day
           c. Select best candidate via DecisionGateway
           d. Move candidate to open_day (preserve time-of-day)
           e. Set open_day = candidate's previous day
           f. Repeat until no candidates or max depth reached

        TERMINATION GUARANTEES:
        - max_cascade_depth prevents infinite loops
        - moved_workflow_ids prevents moving same appointment twice
        - Day-based ordering ensures forward progress
        """
        now = ensure_naive_utc(current_time) if current_time else utc_now()
        cancelled_time = ensure_naive_utc(cancelled_workflow.appointment_time)
        open_day = cancelled_time.date()

        # Classify time window
        time_window = self._classify_time_window(cancelled_time, now)

        logger.info(
            f"Cascade triggered for business {business_id}: "
            f"slot={cancelled_time}, time_window={time_window.value}"
        )

        # LONG_TERM: Just notify, no cascade
        if time_window == TimeWindow.LONG_TERM:
            notify_result = self.notify_slot_available(
                business_id=business_id,
                cancelled_workflow=cancelled_workflow,
                current_time=now
            )
            events.extend(notify_result.events)

            return {
                "moves_count": 0,
                "cascade_depth": 0,
                "moved_workflow_ids": [],
                "time_window": time_window.value,
                "action": "notify",
                "reason": "Long-term slot - notified wishlist"
            }

        # Tracking variables
        cascade_depth = 0
        max_depth = self._gateway.max_cascade_depth
        # Seed with workflows already offered/moved for this slot (e.g. after
        # an offer expired) so the same client is never re-selected.
        moved_workflow_ids: set = set(excluded_workflow_ids or set())
        total_moves = 0
        offers_created = 0

        # Emit cascade triggered event
        cascade_event = CascadeTriggeredEvent(
            aggregate_id=cancelled_workflow.id,
            business_id=business_id,
            cancelled_slot_time=cancelled_time
        )
        events.append(cascade_event)
        self._event_bus.publish(cascade_event)

        logger.info(
            f"Cascade started for business {business_id}: "
            f"open_day={open_day}, max_depth={max_depth}, time_window={time_window.value}"
        )

        # Cascade loop
        while cascade_depth < max_depth:
            # Get candidates from days AFTER open_day
            # We use open_day at midnight as the cutoff
            from datetime import time as time_class
            cutoff_time = datetime.combine(open_day, time_class.min)

            min_risk = self._gateway._medium_risk_threshold
            candidates = self._workflows.get_cascade_candidates(
                business_id=business_id,
                after_time=cutoff_time,
                min_risk=min_risk
            )

            # Filter: only candidates on days AFTER open_day, not already moved
            eligible_candidates = [
                c for c in candidates
                if c.appointment_time.date() > open_day
                and c.id not in moved_workflow_ids
                and c.is_movable
            ]

            if not eligible_candidates:
                logger.info(
                    f"Cascade stopping at depth {cascade_depth}: "
                    f"no eligible candidates after {open_day}"
                )
                break

            # Get client data for candidates
            client_data_map = {
                c.client_id: self._clients.get_client_data(c.client_id)
                for c in eligible_candidates
            }

            # Get decision from gateway
            decision = self._gateway.evaluate_cascade_candidates(
                cancelled_slot=cutoff_time,
                candidates=eligible_candidates,
                client_data_map=client_data_map
            )

            if not decision.approved or not decision.candidates:
                logger.info(
                    f"Cascade stopping at depth {cascade_depth}: "
                    f"no approved candidates"
                )
                break

            # Select the TOP candidate (best ranked)
            selected = decision.candidates[0]
            selected_workflow = next(
                (c for c in eligible_candidates if c.id == selected.workflow_id),
                None
            )

            if selected_workflow is None:
                logger.warning(f"Selected workflow {selected.workflow_id} not found")
                break

            # Calculate new time: preserve time-of-day, change day
            old_time = selected_workflow.appointment_time
            preserved_time = old_time.time()
            new_time = datetime.combine(open_day, preserved_time)

            # Get client data for offer flow decision
            client_data = client_data_map.get(selected_workflow.client_id, {})

            # Check if we should use offer flow instead of auto-move
            use_offer = self._should_use_offer_flow(time_window, client_data)

            if use_offer and self._offers is not None:
                # Create an offer instead of auto-moving
                logger.info(
                    f"Creating offer for workflow {selected_workflow.id}: "
                    f"{old_time} → {new_time} (time_window={time_window.value})"
                )

                offer_result = self.create_offer(
                    business_id=business_id,
                    candidate_workflow=selected_workflow,
                    slot_time=new_time,
                    trigger_workflow_id=cancelled_workflow.id,
                    priority_score=selected.priority_score,
                    current_time=now
                )

                if offer_result.success:
                    events.extend(offer_result.events)
                    offers_created += 1

                    # For offer flow, we don't continue cascade immediately
                    # The cascade continues when the offer is accepted
                    logger.info(
                        f"Offer created - cascade paused pending response. "
                        f"Offer ID: {offer_result.metadata.get('offer_id')}"
                    )

                    # Emit completed event with offer info
                    completed_event = CascadeCompletedEvent(
                        aggregate_id=cancelled_workflow.id,
                        business_id=business_id,
                        trigger_workflow_id=cancelled_workflow.id,
                        total_moves=total_moves,
                        max_depth_reached=cascade_depth,
                        moved_workflow_ids=tuple(moved_workflow_ids)
                    )
                    events.append(completed_event)
                    self._event_bus.publish(completed_event)

                    return {
                        "moves_count": total_moves,
                        "cascade_depth": cascade_depth,
                        "moved_workflow_ids": list(moved_workflow_ids),
                        "time_window": time_window.value,
                        "action": "offer",
                        "offers_created": offers_created,
                        "pending_offer_id": offer_result.metadata.get("offer_id"),
                        "reason": f"Offer created for {time_window.value} slot"
                    }
                else:
                    # Offer creation failed, try next candidate
                    logger.warning(
                        f"Offer creation failed: {offer_result.error}. "
                        f"Trying next candidate."
                    )
                    # Mark this candidate as processed so we don't try again
                    moved_workflow_ids.add(selected_workflow.id)
                    continue

            # DOUBLE-BOOKING GUARD: the target slot must be free.
            if self._slot_has_conflict(
                business_id=business_id,
                start_time=new_time,
                duration_minutes=selected_workflow.duration_minutes,
                exclude_workflow_id=selected_workflow.id
            ):
                logger.info(
                    f"Cascade skipping workflow {selected_workflow.id}: "
                    f"target slot {new_time} is occupied"
                )
                moved_workflow_ids.add(selected_workflow.id)
                continue

            # AUTO-MOVE: Execute the move directly
            logger.info(
                f"Cascade move: workflow {selected_workflow.id} "
                f"from {old_time.date()} to {open_day} "
                f"(time preserved: {preserved_time})"
            )

            try:
                updated_workflow = self._workflows.update_time(
                    workflow_id=selected_workflow.id,
                    new_time=new_time,
                    business_id=business_id
                )

                # Record cascade in history
                self._workflows.record_cascade(
                    business_id=business_id,
                    trigger_workflow_id=cancelled_workflow.id,
                    moved_workflow_id=selected_workflow.id,
                    from_time=old_time,
                    to_time=new_time,
                    depth=cascade_depth + 1,
                    score=selected.priority_score
                )

                # Emit SlotReassignedEvent
                slot_event = SlotReassignedEvent(
                    aggregate_id=selected_workflow.id,
                    business_id=business_id,
                    workflow_id=selected_workflow.id,
                    client_id=selected_workflow.client_id,
                    from_day=old_time,
                    to_day=new_time,
                    preserved_time=preserved_time.strftime("%H:%M"),
                    cascade_depth=cascade_depth + 1,
                    move_count=updated_workflow.move_count
                )
                events.append(slot_event)
                self._event_bus.publish(slot_event)

                # Emit AppointmentRescheduledEvent (for notification system)
                reschedule_event = AppointmentRescheduledEvent(
                    aggregate_id=selected_workflow.id,
                    business_id=business_id,
                    workflow_id=selected_workflow.id,
                    client_id=selected_workflow.client_id,
                    old_time=old_time,
                    new_time=new_time,
                    reason="cascade"
                )
                events.append(reschedule_event)
                self._event_bus.publish(reschedule_event)

                # Update tracking
                moved_workflow_ids.add(selected_workflow.id)
                total_moves += 1
                cascade_depth += 1

                # Set open_day to the day the candidate was moved FROM
                # This creates the cascading effect
                open_day = old_time.date()

                logger.info(
                    f"Cascade move successful: depth={cascade_depth}, "
                    f"next open_day={open_day}"
                )

            except Exception as e:
                logger.error(
                    f"Cascade move failed for workflow {selected_workflow.id}: {e}"
                )
                break

        # Emit cascade completed event
        completed_event = CascadeCompletedEvent(
            aggregate_id=cancelled_workflow.id,
            business_id=business_id,
            trigger_workflow_id=cancelled_workflow.id,
            total_moves=total_moves,
            max_depth_reached=cascade_depth,
            moved_workflow_ids=tuple(moved_workflow_ids)
        )
        events.append(completed_event)
        self._event_bus.publish(completed_event)

        logger.info(
            f"Cascade completed for business {business_id}: "
            f"{total_moves} moves, depth={cascade_depth}, time_window={time_window.value}"
        )

        return {
            "moves_count": total_moves,
            "cascade_depth": cascade_depth,
            "moved_workflow_ids": list(moved_workflow_ids),
            "time_window": time_window.value,
            "action": "auto_move",
            "offers_created": offers_created,
            "reason": f"Completed with {total_moves} moves"
        }

    def run_optimization(self, business_id: int) -> ExecutionResult:
        """
        Run full optimization for a business.

        Combines risk recalculation with identification of high-risk appointments.
        """
        import time
        start = time.time()

        # Recalculate risks
        risk_result = self.recalculate_risk_scores(business_id)

        # Get distribution
        workflows = self._workflows.get_active_workflows(business_id)
        high = sum(1 for w in workflows if w.risk_score >= 0.7)
        medium = sum(1 for w in workflows if 0.4 <= w.risk_score < 0.7)
        low = sum(1 for w in workflows if w.risk_score < 0.4)

        execution_time = (time.time() - start) * 1000

        opt_event = OptimizationCompletedEvent(
            business_id=business_id,
            appointments_processed=risk_result.metadata.get("total", 0),
            high_risk_count=high,
            medium_risk_count=medium,
            low_risk_count=low,
            execution_time_ms=execution_time
        )
        self._event_bus.publish(opt_event)

        logger.info(
            f"Optimization completed for business {business_id}: "
            f"{high} high, {medium} medium, {low} low risk"
        )

        return ExecutionResult(
            success=True,
            workflow=None,
            events=risk_result.events + [opt_event],
            metadata={
                "total": len(workflows),
                "high_risk": high,
                "medium_risk": medium,
                "low_risk": low,
                "execution_time_ms": execution_time
            }
        )

    # ===== Time Window and Offer Operations =====

    def process_expired_offers(
        self,
        business_id: int,
        current_time: Optional[datetime] = None
    ) -> ExecutionResult:
        """
        Expire overdue offers and, per configuration, continue the cascade.

        This closes the loop the offer flow leaves open: an offer that a
        client ignores must not silently strand the freed slot. For each
        expired offer we:
          1. Mark it 'expired' and emit OfferExpiredEvent.
          2. If offer_timeout_action == 'next_candidate' and the trigger
             workflow is known, re-run the cascade for that slot, excluding
             every workflow that has already received an offer for it, so the
             next-best candidate gets the offer.

        Designed to be called periodically by a scheduler adapter.
        """
        events: List[DomainEvent] = []
        now = ensure_naive_utc(current_time) if current_time else utc_now()

        if self._offers is None:
            return ExecutionResult(
                success=False,
                workflow=None,
                events=events,
                error="Offer repository not configured"
            )

        expired_offers = self._offers.get_expired_offers(business_id, now)
        expired_count = 0
        cascades_continued = 0

        for offer in expired_offers:
            self._offers.update_offer_status(offer.offer_id, "expired", now)
            expired_count += 1

            expired_event = OfferExpiredEvent(
                aggregate_id=offer.workflow_id,
                business_id=business_id,
                offer_id=offer.offer_id,
                workflow_id=offer.workflow_id,
                client_id=offer.client_id,
                to_time=offer.to_time
            )
            events.append(expired_event)
            self._event_bus.publish(expired_event)
            logger.info(f"Expired offer {offer.offer_id} (slot {offer.to_time})")

            if (
                self._offer_timeout_action == "next_candidate"
                and offer.trigger_workflow_id is not None
            ):
                trigger_workflow = self._workflows.get_by_id(
                    offer.trigger_workflow_id, business_id
                )
                if trigger_workflow is None:
                    continue

                # Every workflow that already got an offer for this slot is
                # excluded, so expiry never re-offers to the same client.
                prior_offers = self._offers.get_offers_for_trigger(
                    trigger_workflow_id=offer.trigger_workflow_id,
                    business_id=business_id
                )
                excluded = {o.workflow_id for o in prior_offers}

                cascade_result = self._execute_cascade(
                    cancelled_workflow=trigger_workflow,
                    business_id=business_id,
                    events=events,
                    current_time=now,
                    excluded_workflow_ids=excluded
                )
                if cascade_result.get("offers_created") or cascade_result.get("moves_count"):
                    cascades_continued += 1

        return ExecutionResult(
            success=True,
            workflow=None,
            events=events,
            metadata={
                "expired_count": expired_count,
                "cascades_continued": cascades_continued
            }
        )

    def _slot_has_conflict(
        self,
        business_id: int,
        start_time: datetime,
        duration_minutes: int,
        exclude_workflow_id: Optional[int] = None
    ) -> bool:
        """
        True if moving an appointment of the given duration to start_time
        would overlap any active appointment for this business.

        This is the double-booking guard: EVERY path that changes an
        appointment's time (cascade auto-move, offer creation, offer
        acceptance) MUST pass this check first.
        """
        conflicts = self._workflows.get_conflicting_workflows(
            business_id=business_id,
            start_time=start_time,
            duration_minutes=duration_minutes,
            exclude_workflow_id=exclude_workflow_id
        )
        if conflicts:
            logger.info(
                f"Slot conflict at {start_time} (business {business_id}): "
                f"overlaps workflows {[w.id for w in conflicts]}"
            )
        return bool(conflicts)

    def _classify_time_window(
        self,
        slot_time: datetime,
        current_time: Optional[datetime] = None
    ) -> TimeWindow:
        """
        Classify a slot into a time window based on how far in the future it is.

        Args:
            slot_time: The datetime of the slot
            current_time: Current time (for testing), defaults to now

        Returns:
            TimeWindow classification (SHORT_TERM, MEDIUM_TERM, or LONG_TERM)
        """
        now = ensure_naive_utc(current_time) if current_time else utc_now()
        time_until = ensure_naive_utc(slot_time) - now

        # Convert to hours for comparison
        hours_until = time_until.total_seconds() / 3600
        days_until = hours_until / 24

        if hours_until < self._short_term_hours:
            return TimeWindow.SHORT_TERM
        elif days_until < self._medium_term_days:
            # Between short_term_hours and medium_term_days
            # This is actually medium-term behavior
            return TimeWindow.MEDIUM_TERM
        elif days_until < self._long_term_days:
            return TimeWindow.MEDIUM_TERM
        else:
            return TimeWindow.LONG_TERM

    def _should_use_offer_flow(
        self,
        time_window: TimeWindow,
        client_data: Dict[str, Any]
    ) -> bool:
        """
        Determine if we should use offer flow (consent) vs auto-move.

        Args:
            time_window: The classified time window
            client_data: Client behavioral data (includes segment)

        Returns:
            True if offer flow should be used, False for auto-move
        """
        # Always require consent if configured
        if self._require_consent_for_moves:
            return True

        # VIP clients always get offers if configured
        if self._vip_always_offer:
            segment = client_data.get("segment", "regular")
            if segment == "vip":
                return True

        # Determine based on time window action
        if time_window == TimeWindow.SHORT_TERM:
            return self._short_term_action == "offer"
        elif time_window == TimeWindow.MEDIUM_TERM:
            return self._medium_term_action == "offer"
        else:  # LONG_TERM
            # Long-term always uses notify (no auto-move)
            return True

    def _get_offer_expiry(
        self,
        time_window: TimeWindow,
        current_time: Optional[datetime] = None
    ) -> datetime:
        """
        Get the expiry time for an offer based on time window.

        Args:
            time_window: The time window classification
            current_time: Current time (for testing)

        Returns:
            Datetime when the offer expires
        """
        now = current_time or utc_now()

        if time_window == TimeWindow.SHORT_TERM:
            return now + timedelta(minutes=self._short_term_offer_expiry_minutes)
        else:  # MEDIUM_TERM (LONG_TERM doesn't make offers)
            return now + timedelta(hours=self._medium_term_offer_expiry_hours)

    def create_offer(
        self,
        business_id: int,
        candidate_workflow: WorkflowInstance,
        slot_time: datetime,
        trigger_workflow_id: int,
        priority_score: float = 0.0,
        current_time: Optional[datetime] = None
    ) -> ExecutionResult:
        """
        Create an offer to move a candidate to an earlier slot.

        Args:
            business_id: Business identifier
            candidate_workflow: The workflow being offered to move
            slot_time: The earlier slot being offered
            trigger_workflow_id: The cancelled workflow that opened this slot
            priority_score: Selection score from DecisionGateway
            current_time: Current time (for testing)

        Returns:
            ExecutionResult with created offer
        """
        events = []

        if self._offers is None:
            return ExecutionResult(
                success=False,
                workflow=None,
                events=events,
                error="Offer repository not configured"
            )

        # Classify time window
        time_window = self._classify_time_window(slot_time, current_time)

        # Long-term slots don't make offers - just notify
        if time_window == TimeWindow.LONG_TERM:
            return ExecutionResult(
                success=False,
                workflow=None,
                events=events,
                error="Long-term slots use notification, not offers"
            )

        # Don't offer a slot that is no longer free (a direct booking or a
        # concurrent cascade may have taken it since the cancellation).
        if self._slot_has_conflict(
            business_id=business_id,
            start_time=slot_time,
            duration_minutes=candidate_workflow.duration_minutes,
            exclude_workflow_id=candidate_workflow.id
        ):
            return ExecutionResult(
                success=False,
                workflow=None,
                events=events,
                error=f"Slot {slot_time} is no longer available"
            )

        # Check max offers per slot
        offer_count = self._offers.count_offers_for_slot(
            slot_time=slot_time,
            business_id=business_id,
            trigger_workflow_id=trigger_workflow_id
        )
        if offer_count >= self._max_offers_per_slot:
            return ExecutionResult(
                success=False,
                workflow=None,
                events=events,
                error=f"Max offers ({self._max_offers_per_slot}) reached for slot"
            )

        # Check if workflow already has an active offer
        existing_offer = self._offers.get_active_offer_for_workflow(
            workflow_id=candidate_workflow.id,
            business_id=business_id
        )
        if existing_offer is not None:
            return ExecutionResult(
                success=False,
                workflow=None,
                events=events,
                error="Workflow already has an active offer"
            )

        # Calculate expiry
        expires_at = self._get_offer_expiry(time_window, current_time)

        # Create offer
        offer = Offer(
            offer_id=str(uuid.uuid4()),
            business_id=business_id,
            workflow_id=candidate_workflow.id,
            client_id=candidate_workflow.client_id,
            from_time=candidate_workflow.appointment_time,
            to_time=slot_time,
            expires_at=expires_at,
            time_window=time_window,
            trigger_workflow_id=trigger_workflow_id,
            priority_score=priority_score,
            status="offered"
        )

        # Persist offer
        created_offer = self._offers.create_offer(offer)

        # Emit event
        offer_event = EarlierSlotOfferedEvent(
            aggregate_id=candidate_workflow.id,
            business_id=business_id,
            offer_id=created_offer.offer_id,
            workflow_id=candidate_workflow.id,
            client_id=candidate_workflow.client_id,
            from_time=candidate_workflow.appointment_time,
            to_time=slot_time,
            expires_at=expires_at,
            time_window=time_window.value,
            trigger_workflow_id=trigger_workflow_id,
            priority_score=priority_score
        )
        events.append(offer_event)
        self._event_bus.publish(offer_event)

        logger.info(
            f"Created offer {created_offer.offer_id} for workflow {candidate_workflow.id}: "
            f"{candidate_workflow.appointment_time} → {slot_time} "
            f"(expires: {expires_at}, window: {time_window.value})"
        )

        return ExecutionResult(
            success=True,
            workflow=candidate_workflow,
            events=events,
            metadata={
                "offer_id": created_offer.offer_id,
                "time_window": time_window.value,
                "expires_at": expires_at.isoformat()
            }
        )

    def accept_offer(
        self,
        offer_id: str,
        business_id: int,
        current_time: Optional[datetime] = None
    ) -> ExecutionResult:
        """
        Accept an offer - moves the appointment to the earlier slot.

        Args:
            offer_id: The offer ID
            business_id: Business identifier
            current_time: Current time (for testing)

        Returns:
            ExecutionResult with moved workflow
        """
        events = []
        now = current_time or utc_now()

        if self._offers is None:
            return ExecutionResult(
                success=False,
                workflow=None,
                events=events,
                error="Offer repository not configured"
            )

        # Get offer
        offer = self._offers.get_offer(offer_id, business_id)
        if offer is None:
            return ExecutionResult(
                success=False,
                workflow=None,
                events=events,
                error=f"Offer {offer_id} not found"
            )

        # Check if offer has expired
        if now > ensure_naive_utc(offer.expires_at):
            # Mark as expired and fail
            self._offers.update_offer_status(offer_id, "expired", now)
            return ExecutionResult(
                success=False,
                workflow=None,
                events=events,
                error="Offer has expired"
            )

        # Check offer status
        if offer.status != "offered":
            return ExecutionResult(
                success=False,
                workflow=None,
                events=events,
                error=f"Offer is not active (status: {offer.status})"
            )

        # Get workflow
        workflow = self._workflows.get_by_id(offer.workflow_id, business_id)
        if workflow is None:
            return ExecutionResult(
                success=False,
                workflow=None,
                events=events,
                error=f"Workflow {offer.workflow_id} not found"
            )

        # STATE GUARD: the appointment must still be active. It may have been
        # cancelled, completed, or marked no-show since the offer was created.
        if workflow.status not in (WorkflowStatus.SCHEDULED, WorkflowStatus.CONFIRMED):
            self._offers.update_offer_status(offer_id, "expired", now)
            return ExecutionResult(
                success=False,
                workflow=None,
                events=events,
                error=(
                    f"Appointment is no longer active "
                    f"(status: {workflow.status.value}); offer invalidated"
                )
            )

        # DOUBLE-BOOKING GUARD: the offered slot must still be free. Another
        # acceptance, a cascade, or a direct booking may have filled it during
        # the offer window.
        if self._slot_has_conflict(
            business_id=business_id,
            start_time=offer.to_time,
            duration_minutes=workflow.duration_minutes,
            exclude_workflow_id=workflow.id
        ):
            self._offers.update_offer_status(offer_id, "expired", now)
            return ExecutionResult(
                success=False,
                workflow=None,
                events=events,
                error="Offered slot is no longer available; offer invalidated"
            )

        # Execute the move
        try:
            updated_workflow = self._workflows.update_time(
                workflow_id=offer.workflow_id,
                new_time=offer.to_time,
                business_id=business_id
            )

            # Record cascade if trigger workflow exists
            if offer.trigger_workflow_id:
                self._workflows.record_cascade(
                    business_id=business_id,
                    trigger_workflow_id=offer.trigger_workflow_id,
                    moved_workflow_id=offer.workflow_id,
                    from_time=offer.from_time,
                    to_time=offer.to_time,
                    depth=1,  # Direct offer acceptance
                    score=offer.priority_score
                )

            # Update offer status
            self._offers.update_offer_status(offer_id, "accepted", now)

            # SIBLING INVALIDATION: any other active offer for this same slot
            # must die now, or a second client could accept into an occupied
            # slot (race -> double booking).
            siblings = self._offers.get_active_offers_for_slot(
                slot_time=offer.to_time,
                business_id=business_id
            )
            for sibling in siblings:
                if sibling.offer_id != offer_id and sibling.status == "offered":
                    self._offers.update_offer_status(sibling.offer_id, "expired", now)
                    expired_event = OfferExpiredEvent(
                        aggregate_id=sibling.workflow_id,
                        business_id=business_id,
                        offer_id=sibling.offer_id,
                        workflow_id=sibling.workflow_id,
                        client_id=sibling.client_id
                    )
                    events.append(expired_event)
                    self._event_bus.publish(expired_event)
                    logger.info(
                        f"Invalidated sibling offer {sibling.offer_id} "
                        f"for slot {offer.to_time}"
                    )

            # Calculate response time
            # Note: We'd need created_at from offer for accurate timing
            response_time_seconds = 0.0

            # Emit events
            accept_event = OfferAcceptedEvent(
                aggregate_id=offer.workflow_id,
                business_id=business_id,
                offer_id=offer_id,
                workflow_id=offer.workflow_id,
                client_id=offer.client_id,
                from_time=offer.from_time,
                to_time=offer.to_time,
                response_time_seconds=response_time_seconds
            )
            events.append(accept_event)
            self._event_bus.publish(accept_event)

            # Emit reschedule event (for notification system)
            reschedule_event = AppointmentRescheduledEvent(
                aggregate_id=offer.workflow_id,
                business_id=business_id,
                workflow_id=offer.workflow_id,
                client_id=offer.client_id,
                old_time=offer.from_time,
                new_time=offer.to_time,
                reason="offer_accepted"
            )
            events.append(reschedule_event)
            self._event_bus.publish(reschedule_event)

            logger.info(
                f"Offer {offer_id} accepted: workflow {offer.workflow_id} "
                f"moved from {offer.from_time} to {offer.to_time}"
            )

            return ExecutionResult(
                success=True,
                workflow=updated_workflow,
                events=events,
                metadata={
                    "offer_id": offer_id,
                    "from_time": offer.from_time.isoformat(),
                    "to_time": offer.to_time.isoformat()
                }
            )

        except Exception as e:
            logger.error(f"Failed to execute offer acceptance: {e}")
            return ExecutionResult(
                success=False,
                workflow=None,
                events=events,
                error=str(e)
            )

    def decline_offer(
        self,
        offer_id: str,
        business_id: int,
        current_time: Optional[datetime] = None
    ) -> ExecutionResult:
        """
        Decline an offer - optionally triggers next candidate.

        Args:
            offer_id: The offer ID
            business_id: Business identifier
            current_time: Current time (for testing)

        Returns:
            ExecutionResult with decline confirmation
        """
        events = []
        now = current_time or utc_now()

        if self._offers is None:
            return ExecutionResult(
                success=False,
                workflow=None,
                events=events,
                error="Offer repository not configured"
            )

        # Get offer
        offer = self._offers.get_offer(offer_id, business_id)
        if offer is None:
            return ExecutionResult(
                success=False,
                workflow=None,
                events=events,
                error=f"Offer {offer_id} not found"
            )

        # Check offer status
        if offer.status != "offered":
            return ExecutionResult(
                success=False,
                workflow=None,
                events=events,
                error=f"Offer is not active (status: {offer.status})"
            )

        # Update offer status
        self._offers.update_offer_status(offer_id, "declined", now)

        # Calculate remaining attempts
        if offer.trigger_workflow_id:
            offer_count = self._offers.count_offers_for_slot(
                slot_time=offer.to_time,
                business_id=business_id,
                trigger_workflow_id=offer.trigger_workflow_id
            )
            attempts_remaining = max(0, self._max_offers_per_slot - offer_count)
        else:
            attempts_remaining = 0

        # Emit event
        decline_event = OfferDeclinedEvent(
            aggregate_id=offer.workflow_id,
            business_id=business_id,
            offer_id=offer_id,
            workflow_id=offer.workflow_id,
            client_id=offer.client_id,
            to_time=offer.to_time,
            response_time_seconds=0.0,
            attempts_remaining=attempts_remaining
        )
        events.append(decline_event)
        self._event_bus.publish(decline_event)

        logger.info(
            f"Offer {offer_id} declined: {attempts_remaining} attempts remaining"
        )

        return ExecutionResult(
            success=True,
            workflow=None,
            events=events,
            metadata={
                "offer_id": offer_id,
                "attempts_remaining": attempts_remaining,
                "next_action": self._offer_timeout_action
            }
        )

    def expire_offers(
        self,
        business_id: int,
        current_time: Optional[datetime] = None
    ) -> ExecutionResult:
        """
        Expire all past-due offers for a business.

        This should be called periodically by a scheduler.

        Args:
            business_id: Business identifier
            current_time: Current time (for testing)

        Returns:
            ExecutionResult with count of expired offers
        """
        events = []
        now = current_time or utc_now()
        expired_count = 0

        if self._offers is None:
            return ExecutionResult(
                success=True,
                workflow=None,
                events=events,
                metadata={"expired_count": 0}
            )

        # Get all active workflows and check their offers
        # Note: In production, this would be a more efficient query
        workflows = self._workflows.get_active_workflows(business_id)

        for workflow in workflows:
            offer = self._offers.get_active_offer_for_workflow(
                workflow_id=workflow.id,
                business_id=business_id
            )

            if offer and offer.status == "offered" and now > offer.expires_at:
                # Mark as expired
                self._offers.update_offer_status(offer.offer_id, "expired", now)

                # Calculate remaining attempts
                if offer.trigger_workflow_id:
                    offer_count = self._offers.count_offers_for_slot(
                        slot_time=offer.to_time,
                        business_id=business_id,
                        trigger_workflow_id=offer.trigger_workflow_id
                    )
                    attempts_remaining = max(0, self._max_offers_per_slot - offer_count)
                else:
                    attempts_remaining = 0

                # Emit event
                expire_event = OfferExpiredEvent(
                    aggregate_id=workflow.id,
                    business_id=business_id,
                    offer_id=offer.offer_id,
                    workflow_id=workflow.id,
                    client_id=workflow.client_id,
                    to_time=offer.to_time,
                    offer_duration_seconds=(now - offer.expires_at).total_seconds() + (
                        self._medium_term_offer_expiry_hours * 3600
                    ),
                    attempts_remaining=attempts_remaining
                )
                events.append(expire_event)
                self._event_bus.publish(expire_event)

                expired_count += 1
                logger.info(f"Expired offer {offer.offer_id} for workflow {workflow.id}")

        return ExecutionResult(
            success=True,
            workflow=None,
            events=events,
            metadata={"expired_count": expired_count}
        )

    def notify_slot_available(
        self,
        business_id: int,
        cancelled_workflow: WorkflowInstance,
        current_time: Optional[datetime] = None
    ) -> ExecutionResult:
        """
        Emit notification for a long-term available slot.

        For far-future cancellations, we notify wishlist users
        instead of cascading.

        Args:
            business_id: Business identifier
            cancelled_workflow: The cancelled workflow
            current_time: Current time (for testing)

        Returns:
            ExecutionResult with notification event
        """
        events = []
        now = current_time or utc_now()

        slot_time = cancelled_workflow.appointment_time
        time_until = slot_time - now
        days_until = int(time_until.total_seconds() / 86400)

        # Emit slot available event
        slot_event = SlotAvailableInFutureEvent(
            aggregate_id=cancelled_workflow.id,
            business_id=business_id,
            slot_time=slot_time,
            slot_duration_minutes=cancelled_workflow.duration_minutes,
            cancelled_workflow_id=cancelled_workflow.id,
            days_until_slot=days_until,
            provider_id=None  # Could be added from workflow metadata
        )
        events.append(slot_event)
        self._event_bus.publish(slot_event)

        logger.info(
            f"Emitted SlotAvailableInFutureEvent for workflow {cancelled_workflow.id}: "
            f"{slot_time} ({days_until} days out)"
        )

        return ExecutionResult(
            success=True,
            workflow=None,
            events=events,
            metadata={
                "slot_time": slot_time.isoformat(),
                "days_until": days_until
            }
        )
