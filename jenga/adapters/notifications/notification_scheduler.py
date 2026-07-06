"""
Notification Scheduler Adapter

Event-driven notification scheduling for Jenga.

INVARIANTS:
- Notifications are EFFECTS, not decisions
- Orchestrator emits events, this adapter reacts
- Notification timing is configurable per business or globally
- Adapters never call back into the orchestrator

This adapter listens to:
- WorkflowCreatedEvent → Schedule initial reminders
- AppointmentRescheduledEvent → Cancel old reminders, schedule new ones
- WorkflowCancelledEvent → Cancel all reminders
- WorkflowCompletedEvent → Cancel any pending reminders

Notification offsets (default):
- 7 days before appointment (168 hours)
- 48 hours before appointment
"""

import logging
from datetime import datetime, timedelta
from jenga.core.time_utils import utc_now
from typing import Dict, List, Optional, Set
from dataclasses import dataclass, field
from enum import Enum

import sys
from pathlib import Path
APP_DIR = Path(__file__).parent.parent.parent.parent.resolve()
if str(APP_DIR) not in sys.path:
    sys.path.insert(0, str(APP_DIR))

from config_loader import config

# Import domain events
from jenga.core.events.domain_events import (
    DomainEvent,
    EventType,
    WorkflowCreatedEvent,
    WorkflowCancelledEvent,
    WorkflowCompletedEvent,
    AppointmentRescheduledEvent,
    event_bus
)

logger = logging.getLogger(__name__)


class NotificationType(str, Enum):
    """Types of notifications Jenga can send."""
    REMINDER_7_DAY = "reminder_7_day"
    REMINDER_48_HOUR = "reminder_48_hour"
    CONFIRMATION_REQUEST = "confirmation_request"
    APPOINTMENT_MOVED = "appointment_moved"
    APPOINTMENT_CANCELLED = "appointment_cancelled"


@dataclass(frozen=True)
class ScheduledNotification:
    """
    A notification scheduled to be sent at a specific time.

    Immutable - any change creates a new instance.
    """
    notification_id: str
    workflow_id: int
    client_id: int
    business_id: int
    notification_type: NotificationType
    scheduled_for: datetime  # When to send
    appointment_time: datetime  # The actual appointment time
    created_at: datetime = field(default_factory=utc_now)

    def to_dict(self) -> Dict:
        return {
            "notification_id": self.notification_id,
            "workflow_id": self.workflow_id,
            "client_id": self.client_id,
            "business_id": self.business_id,
            "notification_type": self.notification_type.value,
            "scheduled_for": self.scheduled_for.isoformat(),
            "appointment_time": self.appointment_time.isoformat(),
            "created_at": self.created_at.isoformat(),
        }


class NotificationScheduler:
    """
    Schedules and manages appointment notifications.

    This is an ADAPTER - it reacts to orchestrator events and schedules
    notifications via the configured notification channels.

    INVARIANTS:
    - Does NOT call back into the orchestrator
    - Does NOT make orchestration decisions
    - Only schedules/cancels notifications based on events
    - Notification timing comes from configuration, not hard-coded

    In production, this would integrate with:
    - Email service (SendGrid, SES, etc.)
    - SMS service (Twilio, etc.)
    - Push notification service (Firebase, etc.)
    - Job queue (Celery, APScheduler, etc.)
    """

    def __init__(
        self,
        reminder_7_day_hours: Optional[int] = None,
        reminder_48_hour_hours: Optional[int] = None,
    ):
        """
        Initialize notification scheduler.

        Args:
            reminder_7_day_hours: Hours before appointment for 7-day reminder
            reminder_48_hour_hours: Hours before appointment for 48-hour reminder
        """
        # Use config values, allow override for testing
        self._reminder_7_day_hours = (
            reminder_7_day_hours or
            config.notifications.reminders.seven_day_hours
        )
        self._reminder_48_hour_hours = (
            reminder_48_hour_hours or
            config.notifications.reminders.forty_eight_hour_hours
        )

        # In-memory storage for scheduled notifications
        # In production, this would be a database or job queue
        self._scheduled: Dict[str, ScheduledNotification] = {}
        self._by_workflow: Dict[int, Set[str]] = {}  # workflow_id → notification_ids

        # Register event handlers
        self._registered = False

    def register_handlers(self) -> None:
        """
        Register event handlers with the event bus.

        Call this once at application startup.
        """
        if self._registered:
            return

        event_bus.subscribe(EventType.WORKFLOW_CREATED, self._on_workflow_created)
        event_bus.subscribe(EventType.WORKFLOW_CANCELLED, self._on_workflow_cancelled)
        event_bus.subscribe(EventType.WORKFLOW_COMPLETED, self._on_workflow_completed)
        # AppointmentRescheduledEvent uses CASCADE_MOVE_EXECUTED type but we also
        # want to handle it specifically
        event_bus.subscribe(EventType.CASCADE_MOVE_EXECUTED, self._on_appointment_rescheduled)

        self._registered = True
        logger.info("NotificationScheduler handlers registered with event bus")

    def _generate_notification_id(
        self,
        workflow_id: int,
        notification_type: NotificationType
    ) -> str:
        """Generate unique notification ID."""
        return f"{workflow_id}_{notification_type.value}_{utc_now().timestamp()}"

    def _on_workflow_created(self, event: DomainEvent) -> None:
        """
        Handle workflow created event.

        Schedules initial reminders based on appointment time.
        """
        if not isinstance(event, WorkflowCreatedEvent):
            return

        if event.appointment_time is None:
            logger.warning(f"WorkflowCreatedEvent {event.aggregate_id} has no appointment_time")
            return

        self.schedule_reminders(
            workflow_id=event.aggregate_id,
            client_id=event.client_id,
            business_id=event.business_id,
            appointment_time=event.appointment_time
        )

    def _on_workflow_cancelled(self, event: DomainEvent) -> None:
        """
        Handle workflow cancelled event.

        Cancels all scheduled reminders for this workflow.
        """
        if not isinstance(event, WorkflowCancelledEvent):
            return

        self.cancel_all_reminders(event.aggregate_id)

    def _on_workflow_completed(self, event: DomainEvent) -> None:
        """
        Handle workflow completed event.

        Cancels any pending reminders (appointment already happened).
        """
        if not isinstance(event, WorkflowCompletedEvent):
            return

        self.cancel_all_reminders(event.aggregate_id)

    def _on_appointment_rescheduled(self, event: DomainEvent) -> None:
        """
        Handle appointment rescheduled event.

        Cancels old reminders and schedules new ones for the new time.
        """
        if not isinstance(event, AppointmentRescheduledEvent):
            # Could also be SlotReassignedEvent, handle both
            return

        if event.new_time is None:
            return

        # Cancel old reminders
        self.cancel_all_reminders(event.workflow_id)

        # Schedule new reminders for new time
        self.schedule_reminders(
            workflow_id=event.workflow_id,
            client_id=event.client_id,
            business_id=event.business_id,
            appointment_time=event.new_time
        )

        # Also schedule a "your appointment was moved" notification
        self._schedule_moved_notification(event)

        logger.info(
            f"Rescheduled notifications for workflow {event.workflow_id}: "
            f"{event.old_time} → {event.new_time}"
        )

    def schedule_reminders(
        self,
        workflow_id: int,
        client_id: int,
        business_id: int,
        appointment_time: datetime
    ) -> List[ScheduledNotification]:
        """
        Schedule reminder notifications for an appointment.

        Args:
            workflow_id: Workflow/appointment ID
            client_id: Client to notify
            business_id: Business ID
            appointment_time: When the appointment is

        Returns:
            List of scheduled notifications
        """
        scheduled = []
        now = utc_now()

        # 7-day reminder
        reminder_7_day_time = appointment_time - timedelta(hours=self._reminder_7_day_hours)
        if reminder_7_day_time > now:
            notif = self._schedule_notification(
                workflow_id=workflow_id,
                client_id=client_id,
                business_id=business_id,
                notification_type=NotificationType.REMINDER_7_DAY,
                scheduled_for=reminder_7_day_time,
                appointment_time=appointment_time
            )
            scheduled.append(notif)
            logger.info(
                f"Scheduled 7-day reminder for workflow {workflow_id} "
                f"at {reminder_7_day_time.isoformat()}"
            )

        # 48-hour reminder
        reminder_48_hour_time = appointment_time - timedelta(hours=self._reminder_48_hour_hours)
        if reminder_48_hour_time > now:
            notif = self._schedule_notification(
                workflow_id=workflow_id,
                client_id=client_id,
                business_id=business_id,
                notification_type=NotificationType.REMINDER_48_HOUR,
                scheduled_for=reminder_48_hour_time,
                appointment_time=appointment_time
            )
            scheduled.append(notif)
            logger.info(
                f"Scheduled 48-hour reminder for workflow {workflow_id} "
                f"at {reminder_48_hour_time.isoformat()}"
            )

        return scheduled

    def _schedule_notification(
        self,
        workflow_id: int,
        client_id: int,
        business_id: int,
        notification_type: NotificationType,
        scheduled_for: datetime,
        appointment_time: datetime
    ) -> ScheduledNotification:
        """
        Schedule a single notification.

        In production, this would add a job to a queue.
        """
        notification_id = self._generate_notification_id(workflow_id, notification_type)

        notification = ScheduledNotification(
            notification_id=notification_id,
            workflow_id=workflow_id,
            client_id=client_id,
            business_id=business_id,
            notification_type=notification_type,
            scheduled_for=scheduled_for,
            appointment_time=appointment_time
        )

        # Store notification
        self._scheduled[notification_id] = notification

        # Track by workflow
        if workflow_id not in self._by_workflow:
            self._by_workflow[workflow_id] = set()
        self._by_workflow[workflow_id].add(notification_id)

        return notification

    def _schedule_moved_notification(self, event: AppointmentRescheduledEvent) -> None:
        """
        Schedule immediate notification that appointment was moved.

        This notifies the client their appointment time changed.
        """
        # Send immediately (scheduled_for = now)
        notification = self._schedule_notification(
            workflow_id=event.workflow_id,
            client_id=event.client_id,
            business_id=event.business_id,
            notification_type=NotificationType.APPOINTMENT_MOVED,
            scheduled_for=utc_now(),  # Send now
            appointment_time=event.new_time
        )

        logger.info(
            f"Scheduled 'appointment moved' notification for workflow {event.workflow_id}"
        )

    def cancel_all_reminders(self, workflow_id: int) -> int:
        """
        Cancel all scheduled reminders for a workflow.

        Args:
            workflow_id: Workflow/appointment ID

        Returns:
            Number of notifications cancelled
        """
        if workflow_id not in self._by_workflow:
            return 0

        notification_ids = self._by_workflow[workflow_id]
        cancelled_count = 0

        for notification_id in list(notification_ids):
            if notification_id in self._scheduled:
                del self._scheduled[notification_id]
                cancelled_count += 1

        del self._by_workflow[workflow_id]

        if cancelled_count > 0:
            logger.info(f"Cancelled {cancelled_count} notifications for workflow {workflow_id}")

        return cancelled_count

    def get_pending_notifications(
        self,
        workflow_id: Optional[int] = None,
        before: Optional[datetime] = None
    ) -> List[ScheduledNotification]:
        """
        Get pending notifications.

        Args:
            workflow_id: Filter by workflow (optional)
            before: Filter by scheduled time (optional)

        Returns:
            List of scheduled notifications
        """
        notifications = list(self._scheduled.values())

        if workflow_id is not None:
            notifications = [n for n in notifications if n.workflow_id == workflow_id]

        if before is not None:
            notifications = [n for n in notifications if n.scheduled_for <= before]

        return sorted(notifications, key=lambda n: n.scheduled_for)

    def get_due_notifications(self) -> List[ScheduledNotification]:
        """
        Get notifications that are due to be sent.

        Returns:
            List of notifications scheduled for now or earlier
        """
        return self.get_pending_notifications(before=utc_now())


# Global notification scheduler instance
notification_scheduler = NotificationScheduler()


def init_notification_scheduler() -> None:
    """
    Initialize the notification scheduler.

    Call this at application startup to register event handlers.
    """
    notification_scheduler.register_handlers()
    logger.info("Notification scheduler initialized")
