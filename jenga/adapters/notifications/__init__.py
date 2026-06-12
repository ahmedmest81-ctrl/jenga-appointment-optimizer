"""
Notification Adapters

Event-driven notification handling for Jenga.

INVARIANTS:
- Notifications are EFFECTS, not decisions
- The orchestrator emits events, adapters react
- Adapters NEVER call back into the orchestrator
- Notification timing is configurable, not hard-coded
"""

from jenga.adapters.notifications.notification_scheduler import (
    NotificationScheduler,
    NotificationType,
    ScheduledNotification,
    notification_scheduler,
    init_notification_scheduler,
)

__all__ = [
    "NotificationScheduler",
    "NotificationType",
    "ScheduledNotification",
    "notification_scheduler",
    "init_notification_scheduler",
]
