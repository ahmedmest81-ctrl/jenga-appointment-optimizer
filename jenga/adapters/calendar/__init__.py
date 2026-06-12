"""
Calendar Adapters

External calendar integrations for Jenga.

INVARIANTS:
- Calendar adapters are READERS and EFFECTORS, not orchestrators
- They translate between external calendars and Jenga's internal model
- They do NOT make decisions - that's the orchestrator's job
- They do NOT mutate Jenga state directly

Jenga is the system of record. Calendars inform Jenga.
"""

from jenga.adapters.calendar.protocol import (
    CalendarAdapter,
    CalendarEvent,
    CalendarEventType,
    CalendarConnectionStatus,
    CalendarProvider,
)
from jenga.adapters.calendar.calendly_adapter import (
    CalendlyAdapter,
    create_calendly_adapter,
)
from jenga.adapters.calendar.storage import (
    CalendarIntegrationRepository,
    create_calendar_repository,
)

__all__ = [
    # Protocol
    "CalendarAdapter",
    "CalendarEvent",
    "CalendarEventType",
    "CalendarConnectionStatus",
    "CalendarProvider",
    # Calendly
    "CalendlyAdapter",
    "create_calendly_adapter",
    # Storage
    "CalendarIntegrationRepository",
    "create_calendar_repository",
]
