"""
State Machine and Temporal Validation for Jenga Appointment System

Provides:
1. State transition validation (prevents invalid appointment state changes)
2. Temporal validation (past dates, booking windows, overlaps)
3. Business rule enforcement

Ensures data integrity and prevents impossible appointment states.
"""

from datetime import datetime, timedelta
from typing import Set, Dict, Optional, List
from sqlalchemy.orm import Session

from models import AppointmentStatus, Appointment
from exceptions import (
    InvalidStateTransitionError,
    PastAppointmentError,
    OutOfWindowError,
    OverlapError,
    TemporalError
)


class StateTransitionValidator:
    """
    Enforces valid state transitions for appointments.

    Valid transitions:
    - SCHEDULED → CONFIRMED, CANCELLED
    - CONFIRMED → COMPLETED, NO_SHOW, CANCELLED
    - CANCELLED → (terminal, no transitions)
    - COMPLETED → (terminal, no transitions)
    - NO_SHOW → (terminal, no transitions)

    Terminal states (CANCELLED, COMPLETED, NO_SHOW) cannot transition further.
    """

    # Valid state transitions matrix
    VALID_TRANSITIONS: Dict[AppointmentStatus, Set[AppointmentStatus]] = {
        AppointmentStatus.SCHEDULED: {
            AppointmentStatus.CONFIRMED,
            AppointmentStatus.CANCELLED,
        },
        AppointmentStatus.CONFIRMED: {
            AppointmentStatus.COMPLETED,
            AppointmentStatus.NO_SHOW,
            AppointmentStatus.CANCELLED,
        },
        AppointmentStatus.CANCELLED: set(),  # Terminal state
        AppointmentStatus.COMPLETED: set(),  # Terminal state
        AppointmentStatus.NO_SHOW: set(),    # Terminal state
    }

    # Terminal states that cannot transition further
    TERMINAL_STATES = {
        AppointmentStatus.CANCELLED,
        AppointmentStatus.COMPLETED,
        AppointmentStatus.NO_SHOW,
    }

    @classmethod
    def can_transition(
        cls,
        from_state: AppointmentStatus,
        to_state: AppointmentStatus
    ) -> bool:
        """
        Check if state transition is valid.

        Args:
            from_state: Current appointment status
            to_state: Desired appointment status

        Returns:
            True if transition is valid, False otherwise
        """
        return to_state in cls.VALID_TRANSITIONS.get(from_state, set())

    @classmethod
    def validate_transition(
        cls,
        from_state: AppointmentStatus,
        to_state: AppointmentStatus
    ) -> None:
        """
        Validate state transition, raise exception if invalid.

        Args:
            from_state: Current appointment status
            to_state: Desired appointment status

        Raises:
            InvalidStateTransitionError: If transition is not allowed
        """
        if not cls.can_transition(from_state, to_state):
            valid_transitions = cls.VALID_TRANSITIONS.get(from_state, set())
            valid_names = [s.value for s in valid_transitions] if valid_transitions else ["none (terminal state)"]

            raise InvalidStateTransitionError(
                f"Cannot transition from {from_state.value} to {to_state.value}. "
                f"Valid transitions from {from_state.value}: {', '.join(valid_names)}"
            )

    @classmethod
    def is_terminal(cls, state: AppointmentStatus) -> bool:
        """
        Check if state is terminal (no further transitions allowed).

        Args:
            state: Appointment status to check

        Returns:
            True if state is terminal, False otherwise
        """
        return state in cls.TERMINAL_STATES

    @classmethod
    def get_valid_transitions(cls, from_state: AppointmentStatus) -> Set[AppointmentStatus]:
        """
        Get all valid transitions from a given state.

        Args:
            from_state: Current appointment status

        Returns:
            Set of valid target states
        """
        return cls.VALID_TRANSITIONS.get(from_state, set())


class TemporalValidator:
    """
    Validates temporal constraints on appointments.

    Ensures:
    - Appointments are not in the past
    - Appointments are within business booking window
    - No overlapping appointments for same provider
    - Appointments respect minimum advance booking time
    """

    def __init__(self, config):
        """
        Initialize temporal validator with configuration.

        Args:
            config: ValidationConfig from config_loader
        """
        self.config = config.appointment

    def validate_no_past_appointment(
        self,
        appointment_time: datetime,
        current_time: Optional[datetime] = None
    ) -> None:
        """
        Ensure appointment is not in the past.

        Args:
            appointment_time: Desired appointment time
            current_time: Current time (defaults to now)

        Raises:
            PastAppointmentError: If appointment is in the past or too soon
        """
        if current_time is None:
            current_time = datetime.utcnow()

        min_advance = timedelta(hours=self.config.min_advance_hours)
        earliest_allowed = current_time + min_advance

        if appointment_time < earliest_allowed:
            raise PastAppointmentError(
                f"Appointment time {appointment_time.isoformat()} is in the past or too soon. "
                f"Must be at least {self.config.min_advance_hours} hour(s) in advance. "
                f"Earliest allowed: {earliest_allowed.isoformat()}"
            )

    def validate_within_window(
        self,
        appointment_time: datetime,
        business_window_days: int,
        current_time: Optional[datetime] = None
    ) -> None:
        """
        Ensure appointment is within business booking window.

        Args:
            appointment_time: Desired appointment time
            business_window_days: Business-specific booking window (from database)
            current_time: Current time (defaults to now)

        Raises:
            OutOfWindowError: If appointment exceeds booking window
        """
        if current_time is None:
            current_time = datetime.utcnow()

        # Use business-specific window, but respect global max
        effective_window_days = min(
            business_window_days,
            self.config.max_future_days
        )

        max_future = current_time + timedelta(days=effective_window_days)

        if appointment_time > max_future:
            raise OutOfWindowError(
                f"Appointment exceeds booking window. "
                f"Appointment time: {appointment_time.isoformat()}, "
                f"Maximum allowed: {max_future.isoformat()} "
                f"(window: {effective_window_days} days)"
            )

    def validate_no_overlap(
        self,
        db: Session,
        appointment_time: datetime,
        duration_minutes: int,
        provider_id: Optional[str],
        business_id: int,
        exclude_appointment_id: Optional[int] = None
    ) -> None:
        """
        Ensure no overlapping appointments for same provider.

        Checks for time range collisions:
        - New appointment: [appointment_time, appointment_time + duration]
        - Existing appointments: [existing_time, existing_time + existing_duration]
        - Overlap if: (new_start < existing_end) AND (new_end > existing_start)

        Args:
            db: Database session
            appointment_time: Desired appointment time
            duration_minutes: Appointment duration
            provider_id: Provider ID (optional)
            business_id: Business ID for multi-tenant isolation
            exclude_appointment_id: Appointment ID to exclude (for updates)

        Raises:
            OverlapError: If appointment overlaps with existing appointment
        """
        # Skip validation if no provider specified
        if not provider_id:
            return

        # Calculate appointment end time
        end_time = appointment_time + timedelta(minutes=duration_minutes)

        # Query for potentially overlapping appointments
        # Only check SCHEDULED and CONFIRMED appointments (ignore CANCELLED, COMPLETED, NO_SHOW)
        query = db.query(Appointment).filter(
            Appointment.business_id == business_id,
            Appointment.provider_id == provider_id,
            Appointment.status.in_([AppointmentStatus.SCHEDULED, AppointmentStatus.CONFIRMED])
        )

        # Exclude current appointment (for updates)
        if exclude_appointment_id:
            query = query.filter(Appointment.id != exclude_appointment_id)

        # Get all appointments for this provider
        existing_appointments = query.all()

        # Check each appointment for overlap
        for existing in existing_appointments:
            existing_end = existing.appointment_time + timedelta(minutes=existing.duration_minutes)

            # Check for time range overlap
            # Overlap if: (new_start < existing_end) AND (new_end > existing_start)
            if appointment_time < existing_end and end_time > existing.appointment_time:
                raise OverlapError(
                    f"Appointment overlaps with existing appointment (ID: {existing.id}). "
                    f"New: {appointment_time.isoformat()} - {end_time.isoformat()} ({duration_minutes} min), "
                    f"Existing: {existing.appointment_time.isoformat()} - {existing_end.isoformat()} "
                    f"({existing.duration_minutes} min), "
                    f"Provider: {provider_id}"
                )

    def validate_duration(self, duration_minutes: int) -> None:
        """
        Validate appointment duration is within bounds.

        Args:
            duration_minutes: Appointment duration in minutes

        Raises:
            ValueError: If duration is invalid
        """
        if duration_minutes < self.config.min_duration_minutes:
            raise ValueError(
                f"Duration {duration_minutes} minutes is too short. "
                f"Minimum: {self.config.min_duration_minutes} minutes"
            )

        if duration_minutes > self.config.max_duration_minutes:
            raise ValueError(
                f"Duration {duration_minutes} minutes is too long. "
                f"Maximum: {self.config.max_duration_minutes} minutes"
            )

    def validate_all(
        self,
        db: Session,
        appointment_time: datetime,
        duration_minutes: int,
        provider_id: Optional[str],
        business_id: int,
        business_window_days: int,
        exclude_appointment_id: Optional[int] = None,
        current_time: Optional[datetime] = None
    ) -> None:
        """
        Run all temporal validations.

        Convenience method to run all validations in sequence.

        Args:
            db: Database session
            appointment_time: Desired appointment time
            duration_minutes: Appointment duration
            provider_id: Provider ID (optional)
            business_id: Business ID
            business_window_days: Business-specific booking window
            exclude_appointment_id: Appointment ID to exclude (for updates)
            current_time: Current time (defaults to now)

        Raises:
            PastAppointmentError: If appointment is in the past
            OutOfWindowError: If appointment exceeds booking window
            OverlapError: If appointment overlaps with existing appointment
            ValueError: If duration is invalid
        """
        # Validate duration
        self.validate_duration(duration_minutes)

        # Validate not in past
        self.validate_no_past_appointment(appointment_time, current_time)

        # Validate within window
        self.validate_within_window(
            appointment_time,
            business_window_days,
            current_time
        )

        # Validate no overlap
        self.validate_no_overlap(
            db,
            appointment_time,
            duration_minutes,
            provider_id,
            business_id,
            exclude_appointment_id
        )


def validate_appointment_status_for_action(
    appointment: Appointment,
    allowed_states: Set[AppointmentStatus],
    action: str
) -> None:
    """
    Helper function to validate appointment is in allowed state for action.

    Args:
        appointment: Appointment to check
        allowed_states: Set of allowed states
        action: Action being performed (for error message)

    Raises:
        InvalidStateTransitionError: If appointment not in allowed state
    """
    if appointment.status not in allowed_states:
        allowed_names = [s.value for s in allowed_states]
        raise InvalidStateTransitionError(
            f"Cannot {action} appointment in {appointment.status.value} state. "
            f"Allowed states: {', '.join(allowed_names)}"
        )


def validate_appointment_time_has_passed(
    appointment: Appointment,
    current_time: Optional[datetime] = None
) -> None:
    """
    Helper function to validate appointment time has passed.

    Used for actions that should only happen after appointment time
    (e.g., marking completed, marking no-show).

    Args:
        appointment: Appointment to check
        current_time: Current time (defaults to now)

    Raises:
        TemporalError: If appointment time has not passed
    """
    if current_time is None:
        current_time = datetime.utcnow()

    if appointment.appointment_time > current_time:
        raise TemporalError(
            f"Cannot perform action on future appointment. "
            f"Appointment time: {appointment.appointment_time.isoformat()}, "
            f"Current time: {current_time.isoformat()}"
        )
