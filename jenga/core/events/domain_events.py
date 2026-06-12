"""
Domain Events

Internal event language for the orchestration kernel.
These events represent significant domain occurrences.

Core emits events → Adapters translate events outward.
No adapter may redefine core meaning.

Cloud-neutral: No external dependencies.
"""

from dataclasses import dataclass, field
from datetime import datetime
from typing import Optional, Dict, Any
from enum import Enum
import uuid


class EventType(str, Enum):
    """Types of domain events emitted by the orchestrator"""

    # Workflow lifecycle events
    WORKFLOW_CREATED = "workflow.created"
    WORKFLOW_CONFIRMED = "workflow.confirmed"
    WORKFLOW_CANCELLED = "workflow.cancelled"
    WORKFLOW_COMPLETED = "workflow.completed"
    WORKFLOW_NO_SHOW = "workflow.no_show"

    # Risk/Advisory events
    RISK_CALCULATED = "risk.calculated"
    RISK_THRESHOLD_EXCEEDED = "risk.threshold_exceeded"

    # Cascade events
    CASCADE_TRIGGERED = "cascade.triggered"
    CASCADE_CANDIDATE_IDENTIFIED = "cascade.candidate_identified"
    CASCADE_MOVE_EXECUTED = "cascade.move_executed"
    CASCADE_COMPLETED = "cascade.completed"

    # Optimization events
    OPTIMIZATION_STARTED = "optimization.started"
    OPTIMIZATION_COMPLETED = "optimization.completed"

    # Offer lifecycle events (clinic-grade consent flow)
    EARLIER_SLOT_OFFERED = "offer.earlier_slot_offered"
    OFFER_ACCEPTED = "offer.accepted"
    OFFER_DECLINED = "offer.declined"
    OFFER_EXPIRED = "offer.expired"

    # Time-window events
    SLOT_AVAILABLE_IN_FUTURE = "slot.available_in_future"


@dataclass(frozen=True)
class DomainEvent:
    """
    Base domain event.

    Immutable, with unique ID and timestamp.
    All events flow through the orchestrator.
    """
    event_id: str = field(default_factory=lambda: str(uuid.uuid4()))
    timestamp: datetime = field(default_factory=datetime.utcnow)
    event_type: EventType = EventType.WORKFLOW_CREATED
    aggregate_id: Optional[int] = None  # workflow/appointment ID
    business_id: Optional[int] = None
    metadata: Dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> Dict[str, Any]:
        """Convert event to dictionary for serialization"""
        return {
            "event_id": self.event_id,
            "timestamp": self.timestamp.isoformat(),
            "event_type": self.event_type.value,
            "aggregate_id": self.aggregate_id,
            "business_id": self.business_id,
            "metadata": self.metadata
        }


@dataclass(frozen=True)
class WorkflowCreatedEvent(DomainEvent):
    """Emitted when a new workflow is created"""
    event_type: EventType = EventType.WORKFLOW_CREATED
    client_id: Optional[int] = None
    appointment_time: Optional[datetime] = None
    risk_score: float = 0.5


@dataclass(frozen=True)
class WorkflowCancelledEvent(DomainEvent):
    """Emitted when a workflow is cancelled"""
    event_type: EventType = EventType.WORKFLOW_CANCELLED
    trigger_cascade: bool = False
    reason: Optional[str] = None


@dataclass(frozen=True)
class WorkflowCompletedEvent(DomainEvent):
    """Emitted when a workflow is completed"""
    event_type: EventType = EventType.WORKFLOW_COMPLETED


@dataclass(frozen=True)
class WorkflowNoShowEvent(DomainEvent):
    """Emitted when a workflow is marked as no-show"""
    event_type: EventType = EventType.WORKFLOW_NO_SHOW
    client_no_show_rate: float = 0.0


@dataclass(frozen=True)
class RiskCalculatedEvent(DomainEvent):
    """Emitted when risk score is calculated"""
    event_type: EventType = EventType.RISK_CALCULATED
    risk_score: float = 0.0
    model_version: str = ""
    factors: Dict[str, float] = field(default_factory=dict)


@dataclass(frozen=True)
class CascadeTriggeredEvent(DomainEvent):
    """Emitted when a cascade optimization is triggered"""
    event_type: EventType = EventType.CASCADE_TRIGGERED
    cancelled_slot_time: Optional[datetime] = None
    candidate_count: int = 0


@dataclass(frozen=True)
class SlotReassignedEvent(DomainEvent):
    """
    Emitted when an appointment is moved to an earlier slot during cascade.

    This event is a FACT that triggers:
    - Notification rescheduling for the moved client
    - Business notification of the change
    """
    event_type: EventType = EventType.CASCADE_MOVE_EXECUTED
    workflow_id: int = 0
    client_id: int = 0
    from_day: Optional[datetime] = None  # Original appointment date
    to_day: Optional[datetime] = None    # New appointment date
    preserved_time: Optional[str] = None  # Time-of-day preserved (HH:MM)
    cascade_depth: int = 0
    move_count: int = 0  # Total moves for this appointment


@dataclass(frozen=True)
class CascadeCompletedEvent(DomainEvent):
    """
    Emitted when a cascade optimization completes.

    Summarizes the cascade results.
    """
    event_type: EventType = EventType.CASCADE_COMPLETED
    trigger_workflow_id: int = 0
    total_moves: int = 0
    max_depth_reached: int = 0
    moved_workflow_ids: tuple = field(default_factory=tuple)


@dataclass(frozen=True)
class AppointmentRescheduledEvent(DomainEvent):
    """
    Emitted when any appointment time changes.

    Notification adapters listen to this to reschedule reminders.
    """
    workflow_id: int = 0
    client_id: int = 0
    old_time: Optional[datetime] = None
    new_time: Optional[datetime] = None
    reason: str = "cascade"  # cascade, manual, etc.


@dataclass(frozen=True)
class OptimizationCompletedEvent(DomainEvent):
    """Emitted when optimization run completes"""
    event_type: EventType = EventType.OPTIMIZATION_COMPLETED
    appointments_processed: int = 0
    high_risk_count: int = 0
    medium_risk_count: int = 0
    low_risk_count: int = 0
    execution_time_ms: float = 0.0


# ============================================================================
# Offer Lifecycle Events (Clinic-Grade Consent Flow)
# ============================================================================

@dataclass(frozen=True)
class EarlierSlotOfferedEvent(DomainEvent):
    """
    Emitted when an earlier slot is offered to a client.

    This is a FACT: the offer was made. Notification adapters
    should send the actual offer communication to the client.

    Lifecycle: This event → client responds → OfferAccepted/Declined/Expired
    """
    event_type: EventType = EventType.EARLIER_SLOT_OFFERED
    offer_id: str = ""  # UUID of the ShiftOffer
    workflow_id: int = 0  # The appointment being offered to move
    client_id: int = 0
    from_time: Optional[datetime] = None  # Current appointment time
    to_time: Optional[datetime] = None    # Offered earlier slot
    expires_at: Optional[datetime] = None  # When offer expires
    time_window: str = ""  # "short_term", "medium_term", "long_term"
    trigger_workflow_id: Optional[int] = None  # The cancelled appointment that opened the slot
    priority_score: float = 0.0  # Selection score from DecisionGateway


@dataclass(frozen=True)
class OfferAcceptedEvent(DomainEvent):
    """
    Emitted when a client accepts an earlier slot offer.

    This triggers the actual move: orchestrator executes the reschedule
    and emits AppointmentRescheduledEvent.
    """
    event_type: EventType = EventType.OFFER_ACCEPTED
    offer_id: str = ""
    workflow_id: int = 0
    client_id: int = 0
    from_time: Optional[datetime] = None
    to_time: Optional[datetime] = None
    response_time_seconds: float = 0.0  # How long client took to respond


@dataclass(frozen=True)
class OfferDeclinedEvent(DomainEvent):
    """
    Emitted when a client declines an earlier slot offer.

    Orchestrator should try next candidate (if max_offers_per_slot not reached).
    """
    event_type: EventType = EventType.OFFER_DECLINED
    offer_id: str = ""
    workflow_id: int = 0
    client_id: int = 0
    to_time: Optional[datetime] = None  # The slot that was declined
    response_time_seconds: float = 0.0
    attempts_remaining: int = 0  # How many more candidates to try


@dataclass(frozen=True)
class OfferExpiredEvent(DomainEvent):
    """
    Emitted when an offer expires without response.

    Similar to declined - orchestrator should try next candidate.
    """
    event_type: EventType = EventType.OFFER_EXPIRED
    offer_id: str = ""
    workflow_id: int = 0
    client_id: int = 0
    to_time: Optional[datetime] = None  # The slot that expired
    offer_duration_seconds: float = 0.0  # How long the offer was active
    attempts_remaining: int = 0


@dataclass(frozen=True)
class SlotAvailableInFutureEvent(DomainEvent):
    """
    Emitted when a long-term slot becomes available.

    For far-future cancellations (>= long_term_days), we don't
    immediately cascade. Instead, we notify:
    1. Wishlist users who want earlier appointments
    2. Patients with later appointments who might want to move up

    This is informational - no immediate action required.
    """
    event_type: EventType = EventType.SLOT_AVAILABLE_IN_FUTURE
    slot_time: Optional[datetime] = None
    slot_duration_minutes: int = 0
    cancelled_workflow_id: int = 0  # The appointment that was cancelled
    days_until_slot: int = 0  # How far in the future
    provider_id: Optional[str] = None  # If slot is provider-specific


class EventBus:
    """
    Simple in-process event bus.

    Allows adapters to subscribe to domain events.
    Core publishes events → Adapters react.

    INVARIANTS:
    - Events represent FACTS (what happened), not commands
    - Event handlers MUST NOT mutate core state
    - Event handlers MUST NOT call back into the orchestrator
    - Handler failures are logged but do not break the flow

    This is an in-memory bus. For distributed systems,
    adapters can translate to external message brokers.
    """

    def __init__(self):
        self._handlers: Dict[EventType, list] = {}
        self._global_handlers: list = []

    def subscribe(self, event_type: EventType, handler):
        """
        Subscribe to specific event type.

        WARNING: Handlers must be side-effect free with respect to core state.
        They may log, notify external systems, or update read models only.
        """
        if event_type not in self._handlers:
            self._handlers[event_type] = []
        self._handlers[event_type].append(handler)

    def subscribe_all(self, handler):
        """Subscribe to all events (use sparingly)."""
        self._global_handlers.append(handler)

    def publish(self, event: DomainEvent):
        """
        Publish event to all subscribers.

        Events are delivered synchronously. Handler errors are logged
        but do not propagate (event delivery is best-effort).
        """
        import logging
        logger = logging.getLogger(__name__)

        # Call type-specific handlers
        handlers = self._handlers.get(event.event_type, [])
        for handler in handlers:
            try:
                handler(event)
            except Exception as e:
                # Log but don't break - events are informational
                logger.warning(
                    f"Event handler failed for {event.event_type.value}: {e}"
                )

        # Call global handlers
        for handler in self._global_handlers:
            try:
                handler(event)
            except Exception as e:
                logger.warning(
                    f"Global event handler failed for {event.event_type.value}: {e}"
                )

    def clear(self):
        """Clear all subscriptions (useful for testing)."""
        self._handlers.clear()
        self._global_handlers.clear()


# Global event bus instance
# Adapters can import and subscribe to this
event_bus = EventBus()
