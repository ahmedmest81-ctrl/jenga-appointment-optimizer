"""
Calendar Adapter Protocol

Defines the interface that all calendar providers must implement.
Jenga does not depend on any specific calendar - this protocol abstracts them.

INVARIANTS:
- Calendar adapters are stateless translators
- They fetch data from external systems and convert to Jenga's model
- They push changes to external systems when requested
- They NEVER make orchestration decisions
- They NEVER mutate Jenga's internal state directly

Calendars are inputs and effectors. Jenga is the brain.
"""

from abc import ABC, abstractmethod
from dataclasses import dataclass
from datetime import datetime
from enum import Enum
from typing import List, Optional, Dict, Any


class CalendarProvider(str, Enum):
    """Supported calendar providers."""
    CALENDLY = "calendly"
    GOOGLE = "google"
    OUTLOOK = "outlook"
    MANUAL = "manual"  # No external calendar, Jenga-only


class CalendarConnectionStatus(str, Enum):
    """Status of calendar connection."""
    CONNECTED = "connected"
    DISCONNECTED = "disconnected"
    EXPIRED = "expired"
    INVALID = "invalid"
    PENDING = "pending"


@dataclass(frozen=True)
class CalendarEventType:
    """
    Event type from the calendar system.

    Maps to Calendly's event types, Google's calendar types, etc.
    Frozen for immutability.
    """
    external_id: str  # ID in the calendar system
    name: str
    duration_minutes: int
    provider: CalendarProvider
    color: Optional[str] = None
    description: Optional[str] = None

    def to_dict(self) -> Dict[str, Any]:
        """Convert to dictionary for API responses."""
        return {
            "external_id": self.external_id,
            "name": self.name,
            "duration_minutes": self.duration_minutes,
            "provider": self.provider.value,
            "color": self.color,
            "description": self.description,
        }


@dataclass(frozen=True)
class CalendarEvent:
    """
    An event fetched from an external calendar.

    This is Jenga's INTERNAL representation of an external event.
    It is NOT the same as the external system's event structure.

    Frozen for immutability - calendar events are facts, not mutable state.
    """
    external_id: str  # ID in the calendar system (e.g., Calendly event URI)
    provider: CalendarProvider

    # Timing
    start_time: datetime
    end_time: datetime
    duration_minutes: int

    # Participants
    invitee_email: Optional[str] = None
    invitee_name: Optional[str] = None
    invitee_phone: Optional[str] = None

    # Event details
    event_type_id: Optional[str] = None
    event_type_name: Optional[str] = None
    location: Optional[str] = None

    # Status (calendar-native, not Jenga status)
    is_cancelled: bool = False

    # Raw data for debugging
    raw_data: Optional[Dict[str, Any]] = None

    def to_dict(self) -> Dict[str, Any]:
        """Convert to dictionary for API responses and storage."""
        return {
            "external_id": self.external_id,
            "provider": self.provider.value,
            "start_time": self.start_time.isoformat(),
            "end_time": self.end_time.isoformat(),
            "duration_minutes": self.duration_minutes,
            "invitee_email": self.invitee_email,
            "invitee_name": self.invitee_name,
            "invitee_phone": self.invitee_phone,
            "event_type_id": self.event_type_id,
            "event_type_name": self.event_type_name,
            "location": self.location,
            "is_cancelled": self.is_cancelled,
        }


@dataclass
class ConnectionResult:
    """Result of a connection attempt."""
    success: bool
    status: CalendarConnectionStatus
    user_info: Optional[Dict[str, Any]] = None
    error: Optional[str] = None


@dataclass
class FetchResult:
    """Result of fetching events or event types."""
    success: bool
    data: List[Any]  # CalendarEvent or CalendarEventType
    error: Optional[str] = None
    raw_response: Optional[Dict[str, Any]] = None


class CalendarAdapter(ABC):
    """
    Abstract base class for calendar adapters.

    All calendar providers (Calendly, Google, Outlook) implement this protocol.

    INVARIANTS:
    - Adapters are stateless (no internal state beyond credentials)
    - Adapters are read/write translators, not orchestrators
    - Adapters handle provider-specific API details
    - Adapters convert to/from Jenga's CalendarEvent model

    The adapter pattern ensures Jenga works without ANY calendar,
    and can work with ANY calendar that implements this protocol.
    """

    @property
    @abstractmethod
    def provider(self) -> CalendarProvider:
        """Return the provider type for this adapter."""
        ...

    @abstractmethod
    def validate_token(self, token: str) -> ConnectionResult:
        """
        Validate that a token is valid and retrieve user info.

        This is typically called when connecting a calendar.
        Should verify the token works and return user metadata.

        Args:
            token: The access/API token to validate

        Returns:
            ConnectionResult with success status and user info
        """
        ...

    @abstractmethod
    def get_event_types(self, token: str) -> FetchResult:
        """
        Fetch available event types from the calendar.

        For Calendly: event types the user has configured
        For Google: calendar list
        For Outlook: calendar list

        Args:
            token: Valid access token

        Returns:
            FetchResult with list of CalendarEventType
        """
        ...

    @abstractmethod
    def get_scheduled_events(
        self,
        token: str,
        from_time: Optional[datetime] = None,
        to_time: Optional[datetime] = None,
        event_type_id: Optional[str] = None,
    ) -> FetchResult:
        """
        Fetch scheduled events from the calendar.

        Args:
            token: Valid access token
            from_time: Start of time range (optional, defaults to now)
            to_time: End of time range (optional, defaults to 30 days out)
            event_type_id: Filter to specific event type (optional)

        Returns:
            FetchResult with list of CalendarEvent
        """
        ...

    @abstractmethod
    def get_event_by_id(self, token: str, event_id: str) -> Optional[CalendarEvent]:
        """
        Fetch a single event by its external ID.

        Args:
            token: Valid access token
            event_id: External event ID

        Returns:
            CalendarEvent if found, None otherwise
        """
        ...

    def cancel_event(self, token: str, event_id: str, reason: Optional[str] = None) -> bool:
        """
        Cancel an event in the external calendar.

        This is an EFFECTOR method - it changes external state.
        Not all providers support this, so default implementation returns False.

        Args:
            token: Valid access token
            event_id: External event ID to cancel
            reason: Optional cancellation reason

        Returns:
            True if cancelled successfully, False otherwise
        """
        return False  # Default: not supported

    def reschedule_event(
        self,
        token: str,
        event_id: str,
        new_start_time: datetime,
        new_end_time: datetime,
    ) -> Optional[CalendarEvent]:
        """
        Reschedule an event in the external calendar.

        This is an EFFECTOR method - it changes external state.
        Not all providers support this, so default implementation returns None.

        Args:
            token: Valid access token
            event_id: External event ID to reschedule
            new_start_time: New start time
            new_end_time: New end time

        Returns:
            Updated CalendarEvent if successful, None otherwise
        """
        return None  # Default: not supported
