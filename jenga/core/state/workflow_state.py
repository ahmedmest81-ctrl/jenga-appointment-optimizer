"""
Workflow State Management

Core state machine for appointment workflow.
This is the SINGLE SOURCE OF TRUTH for state transitions.

Cloud-neutral: No external dependencies.
Deterministic: Same input always produces same output.
"""

from enum import Enum
from typing import Set, Dict, Optional
from datetime import datetime, timedelta
from dataclasses import dataclass


class WorkflowStatus(str, Enum):
    """
    Appointment workflow states.

    State machine:
    - SCHEDULED → CONFIRMED, CANCELLED
    - CONFIRMED → COMPLETED, NO_SHOW, CANCELLED
    - CANCELLED → (terminal)
    - COMPLETED → (terminal)
    - NO_SHOW → (terminal)
    """
    SCHEDULED = "scheduled"
    CONFIRMED = "confirmed"
    CANCELLED = "cancelled"
    COMPLETED = "completed"
    NO_SHOW = "no_show"


@dataclass(frozen=True)
class WorkflowInstance:
    """
    Immutable workflow instance representing an appointment's state.

    INVARIANTS:
    - This is frozen (immutable) - any "change" creates a new instance
    - State transitions MUST use with_status() which validates the transition
    - Only the orchestrator should create/modify WorkflowInstances
    - External systems must go through the orchestrator

    The frozen=True ensures no accidental mutation.
    """
    id: int
    business_id: int
    client_id: int
    status: WorkflowStatus
    appointment_time: datetime
    duration_minutes: int
    risk_score: float
    is_movable: bool
    move_count: int

    def __post_init__(self):
        """Validate invariants on creation."""
        # Risk score must be in [0, 1]
        if not (0.0 <= self.risk_score <= 1.0):
            object.__setattr__(self, 'risk_score', max(0.0, min(1.0, self.risk_score)))
        # Move count cannot be negative
        if self.move_count < 0:
            object.__setattr__(self, 'move_count', 0)
        # Duration must be positive
        if self.duration_minutes <= 0:
            raise ValueError("duration_minutes must be positive")

    def with_status(self, new_status: WorkflowStatus) -> 'WorkflowInstance':
        """
        Create new instance with updated status (immutable update).

        IMPORTANT: This validates the transition is legal.
        Raises InvalidTransitionError if transition is not allowed.
        """
        # Validate transition before creating new instance
        StateTransitionValidator.validate_transition(self.status, new_status)

        return WorkflowInstance(
            id=self.id,
            business_id=self.business_id,
            client_id=self.client_id,
            status=new_status,
            appointment_time=self.appointment_time,
            duration_minutes=self.duration_minutes,
            risk_score=self.risk_score,
            is_movable=self.is_movable,
            move_count=self.move_count
        )

    def with_risk_score(self, risk_score: float) -> 'WorkflowInstance':
        """Create new instance with updated risk score."""
        # Clamp to valid range
        clamped = max(0.0, min(1.0, risk_score))
        return WorkflowInstance(
            id=self.id,
            business_id=self.business_id,
            client_id=self.client_id,
            status=self.status,
            appointment_time=self.appointment_time,
            duration_minutes=self.duration_minutes,
            risk_score=clamped,
            is_movable=self.is_movable,
            move_count=self.move_count
        )

    @property
    def is_terminal(self) -> bool:
        """Check if workflow is in a terminal state."""
        return StateTransitionValidator.is_terminal(self.status)

    @property
    def is_active(self) -> bool:
        """Check if workflow is in an active (non-terminal) state."""
        return not self.is_terminal


class StateTransitionValidator:
    """
    Enforces valid state transitions for workflows.

    This class defines the ONLY valid state transitions.
    All state changes must pass through this validator.
    """

    # Valid state transitions matrix
    VALID_TRANSITIONS: Dict[WorkflowStatus, Set[WorkflowStatus]] = {
        WorkflowStatus.SCHEDULED: {
            WorkflowStatus.CONFIRMED,
            WorkflowStatus.CANCELLED,
        },
        WorkflowStatus.CONFIRMED: {
            WorkflowStatus.COMPLETED,
            WorkflowStatus.NO_SHOW,
            WorkflowStatus.CANCELLED,
        },
        WorkflowStatus.CANCELLED: set(),  # Terminal state
        WorkflowStatus.COMPLETED: set(),  # Terminal state
        WorkflowStatus.NO_SHOW: set(),    # Terminal state
    }

    # Terminal states that cannot transition further
    TERMINAL_STATES = {
        WorkflowStatus.CANCELLED,
        WorkflowStatus.COMPLETED,
        WorkflowStatus.NO_SHOW,
    }

    @classmethod
    def can_transition(
        cls,
        from_state: WorkflowStatus,
        to_state: WorkflowStatus
    ) -> bool:
        """Check if state transition is valid."""
        return to_state in cls.VALID_TRANSITIONS.get(from_state, set())

    @classmethod
    def validate_transition(
        cls,
        from_state: WorkflowStatus,
        to_state: WorkflowStatus
    ) -> None:
        """
        Validate state transition, raise exception if invalid.

        Raises:
            InvalidTransitionError: If transition is not allowed
        """
        if not cls.can_transition(from_state, to_state):
            valid = cls.VALID_TRANSITIONS.get(from_state, set())
            valid_names = [s.value for s in valid] if valid else ["none (terminal)"]
            raise InvalidTransitionError(
                f"Cannot transition from {from_state.value} to {to_state.value}. "
                f"Valid transitions: {', '.join(valid_names)}"
            )

    @classmethod
    def is_terminal(cls, state: WorkflowStatus) -> bool:
        """Check if state is terminal (no further transitions allowed)."""
        return state in cls.TERMINAL_STATES

    @classmethod
    def get_valid_transitions(cls, from_state: WorkflowStatus) -> Set[WorkflowStatus]:
        """Get all valid transitions from a given state."""
        return cls.VALID_TRANSITIONS.get(from_state, set())


class TemporalValidator:
    """
    Validates temporal constraints on workflows.

    Cloud-neutral: No database dependencies.
    All validation is pure logic.
    """

    def __init__(
        self,
        min_advance_hours: int = 1,
        max_future_days: int = 365,
        min_duration_minutes: int = 15,
        max_duration_minutes: int = 480
    ):
        self.min_advance_hours = min_advance_hours
        self.max_future_days = max_future_days
        self.min_duration_minutes = min_duration_minutes
        self.max_duration_minutes = max_duration_minutes

    def validate_not_in_past(
        self,
        appointment_time: datetime,
        current_time: Optional[datetime] = None
    ) -> None:
        """Ensure appointment is not in the past."""
        if current_time is None:
            current_time = datetime.utcnow()

        earliest = current_time + timedelta(hours=self.min_advance_hours)
        if appointment_time < earliest:
            raise PastAppointmentError(
                f"Appointment time {appointment_time.isoformat()} is too soon. "
                f"Must be at least {self.min_advance_hours} hour(s) in advance."
            )

    def validate_within_window(
        self,
        appointment_time: datetime,
        window_days: int,
        current_time: Optional[datetime] = None
    ) -> None:
        """Ensure appointment is within booking window."""
        if current_time is None:
            current_time = datetime.utcnow()

        effective_window = min(window_days, self.max_future_days)
        max_time = current_time + timedelta(days=effective_window)

        if appointment_time > max_time:
            raise OutOfWindowError(
                f"Appointment exceeds booking window ({effective_window} days)."
            )

    def validate_duration(self, duration_minutes: int) -> None:
        """Validate appointment duration is within bounds."""
        if duration_minutes < self.min_duration_minutes:
            raise DurationError(
                f"Duration {duration_minutes} min is too short. "
                f"Minimum: {self.min_duration_minutes} min."
            )
        if duration_minutes > self.max_duration_minutes:
            raise DurationError(
                f"Duration {duration_minutes} min is too long. "
                f"Maximum: {self.max_duration_minutes} min."
            )


# ===== Core Exceptions =====
# These are part of the core domain, not adapters

class WorkflowError(Exception):
    """Base exception for all workflow errors"""
    pass


class InvalidTransitionError(WorkflowError):
    """Raised when an invalid state transition is attempted"""
    pass


class PastAppointmentError(WorkflowError):
    """Raised when attempting to create an appointment in the past"""
    pass


class OutOfWindowError(WorkflowError):
    """Raised when appointment exceeds business booking window"""
    pass


class DurationError(WorkflowError):
    """Raised when appointment duration is invalid"""
    pass


class OverlapError(WorkflowError):
    """Raised when appointment overlaps with existing appointment"""
    pass
