"""
APScheduler Adapter

Scheduler adapter that triggers orchestration operations.
The scheduler is a TRIGGER, not an owner of business logic.

This adapter:
- Uses APScheduler for time-based execution
- Delegates business operations to the Orchestrator
- Handles notification scheduling (adapter-level concern)
- Provides hooks for external triggers (Google Calendar, etc.)

The orchestrator decides WHAT to do; the scheduler decides WHEN.
"""

from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.triggers.cron import CronTrigger
from apscheduler.triggers.interval import IntervalTrigger
from datetime import datetime, timedelta
from typing import Optional, Dict, Any, Callable
import logging
import time

# Path setup for imports
import sys
from pathlib import Path
APP_DIR = Path(__file__).parent.parent.parent.parent.resolve()
if str(APP_DIR) not in sys.path:
    sys.path.insert(0, str(APP_DIR))

from database import get_db_context
from models import Appointment, AppointmentStatus, Business
from sqlalchemy import and_

# Core imports
from jenga.core.orchestration.orchestrator import Orchestrator
from jenga.core.decisions.decision_gateway import DecisionGateway
from jenga.advisory.ml_advisor import MLRiskAdvisor, create_advisor_from_config
from jenga.adapters.storage.sqlalchemy_adapter import (
    SQLAlchemyWorkflowRepository,
    SQLAlchemyClientRepository,
    SQLAlchemyEventLogger,
)

logger = logging.getLogger(__name__)


class SchedulerAdapter:
    """
    APScheduler adapter for triggering orchestration operations.

    The scheduler does NOT own business logic.
    It triggers the orchestrator at configured intervals.
    """

    def __init__(self, config: Dict[str, Any]):
        """
        Initialize scheduler adapter.

        Args:
            config: Application configuration
        """
        self._scheduler = BackgroundScheduler()
        self._config = config
        self._started = False

        # Extract config sections
        self._features = config.get("features", {})
        self._scheduler_config = config.get("scheduler", {})
        self._ml_config = config.get("ml", {})
        self._notification_config = config.get("notifications", {})
        self._engine_config = config.get("engine", {})

    def _create_orchestrator(self, db) -> Orchestrator:
        """Create orchestrator with current database session."""
        workflow_repo = SQLAlchemyWorkflowRepository(db)
        client_repo = SQLAlchemyClientRepository(db)

        # Create ML advisor if enabled
        advisor = None
        if self._features.get("enable_ml_predictions", True):
            advisor = MLRiskAdvisor(self._ml_config)

        # Create decision gateway
        gateway = DecisionGateway(
            risk_advisor=advisor,
            high_risk_threshold=self._ml_config.get("risk_thresholds", {}).get("high", 0.7),
            medium_risk_threshold=self._ml_config.get("risk_thresholds", {}).get("medium", 0.4),
            max_cascade_depth=self._engine_config.get("max_cascade_depth", 10)
        )

        return Orchestrator(
            workflow_repository=workflow_repo,
            client_repository=client_repo,
            decision_gateway=gateway
        )

    def start(self) -> None:
        """Start the scheduler with configured jobs."""
        if self._started:
            return

        # Daily optimization
        if self._features.get("enable_cascade_optimization", True):
            self._scheduler.add_job(
                func=self._trigger_daily_optimization,
                trigger=CronTrigger(
                    hour=self._scheduler_config.get("daily_optimization_hour", 2),
                    minute=0
                ),
                id="daily_optimization",
                name="Daily Calendar Optimization",
                replace_existing=True
            )
            logger.info(
                f"Scheduled daily optimization at "
                f"{self._scheduler_config.get('daily_optimization_hour', 2)}:00"
            )

        # Reminder processing
        if self._features.get("enable_notifications", True):
            self._scheduler.add_job(
                func=self._trigger_reminders,
                trigger=IntervalTrigger(
                    hours=self._scheduler_config.get("reminder_check_interval_hours", 1)
                ),
                id="process_reminders",
                name="Process Appointment Reminders",
                replace_existing=True
            )
            logger.info(
                f"Scheduled reminders every "
                f"{self._scheduler_config.get('reminder_check_interval_hours', 1)}h"
            )

        # Auto-confirmations
        if self._features.get("enable_auto_confirmation", True):
            self._scheduler.add_job(
                func=self._trigger_auto_confirmations,
                trigger=IntervalTrigger(hours=1),
                id="process_auto_confirmations",
                name="Process Auto-Confirmations",
                replace_existing=True
            )

        # Google Calendar sync
        if self._features.get("enable_google_calendar_sync", False):
            self._scheduler.add_job(
                func=self._trigger_calendar_sync,
                trigger=IntervalTrigger(
                    minutes=self._scheduler_config.get("google_calendar_sync_minutes", 15)
                ),
                id="google_calendar_sync",
                name="Google Calendar Sync",
                replace_existing=True
            )
            logger.info(
                f"Scheduled Google Calendar sync every "
                f"{self._scheduler_config.get('google_calendar_sync_minutes', 15)} minutes"
            )

        self._scheduler.start()
        self._started = True
        logger.info("Scheduler adapter started")

    def shutdown(self) -> None:
        """Shutdown the scheduler gracefully."""
        if self._started:
            self._scheduler.shutdown()
            self._started = False
            logger.info("Scheduler adapter stopped")

    def _trigger_daily_optimization(self) -> None:
        """
        Trigger daily optimization via orchestrator.

        This is a TRIGGER - all business logic is in the orchestrator.
        """
        logger.info("Triggering daily optimization")
        start_time = time.time()

        with get_db_context() as db:
            businesses = db.query(Business).filter(Business.is_active == True).all()

            total_processed = 0
            for business in businesses:
                try:
                    orchestrator = self._create_orchestrator(db)

                    # Trigger risk recalculation
                    result = orchestrator.recalculate_risk_scores(business.id)
                    if result.success:
                        total_processed += result.data.get("updated_count", 0)

                    # Trigger optimization (cascade evaluation)
                    opt_result = orchestrator.run_optimization(business.id)

                    logger.info(
                        f"Business {business.id}: {result.data.get('updated_count', 0)} "
                        f"risks updated, optimization: {opt_result.success}"
                    )

                except Exception as e:
                    logger.error(
                        f"Error optimizing business {business.id}: {e}",
                        exc_info=True
                    )

            db.commit()

        duration = (time.time() - start_time) * 1000
        logger.info(
            f"Daily optimization complete: {total_processed} workflows, "
            f"{duration:.2f}ms"
        )

    def _trigger_reminders(self) -> None:
        """
        Trigger reminder processing.

        Notifications are an adapter concern - the orchestrator
        doesn't know about SMS/email channels.
        """
        logger.info("Processing reminders")

        # Import notifier here to avoid circular dependencies
        from notifier import NotificationService
        from events import EventLogger

        with get_db_context() as db:
            notifier = NotificationService(db)
            event_logger = EventLogger(db)

            reminder_config = self._notification_config.get("reminders", {})

            # 7-day reminders
            seven_day_hours = reminder_config.get("seven_day_hours", 168)
            self._process_reminder_window(
                db, notifier, event_logger,
                hours_ahead=seven_day_hours,
                flag_field="reminder_7_day_sent",
                reminder_type="7_day",
                send_func=notifier.send_7_day_reminder
            )

            # 48-hour reminders
            forty_eight_hours = reminder_config.get("forty_eight_hour_hours", 48)
            self._process_reminder_window(
                db, notifier, event_logger,
                hours_ahead=forty_eight_hours,
                flag_field="reminder_48_hour_sent",
                reminder_type="48_hour",
                send_func=notifier.send_48_hour_reminder
            )

            db.commit()

    def _process_reminder_window(
        self,
        db,
        notifier,
        event_logger,
        hours_ahead: int,
        flag_field: str,
        reminder_type: str,
        send_func: Callable
    ) -> None:
        """Process reminders for a specific time window."""
        window_start = datetime.utcnow() + timedelta(hours=hours_ahead - 1)
        window_end = datetime.utcnow() + timedelta(hours=hours_ahead + 1)

        appointments = db.query(Appointment).filter(
            and_(
                Appointment.status == AppointmentStatus.SCHEDULED,
                Appointment.appointment_time >= window_start,
                Appointment.appointment_time <= window_end,
                getattr(Appointment, flag_field) == False
            )
        ).all()

        sent_count = 0
        for appointment in appointments:
            try:
                status = send_func(appointment)
                setattr(appointment, flag_field, True)
                sent_count += 1

                event_logger.log_reminder_sent(
                    appointment_id=appointment.id,
                    business_id=appointment.business_id,
                    reminder_type=reminder_type,
                    channel="multi",
                    success=status.value == "success"
                )

            except Exception as e:
                logger.error(
                    f"Error sending {reminder_type} reminder for "
                    f"appointment {appointment.id}: {e}"
                )

        if sent_count > 0:
            logger.info(f"Sent {sent_count} {reminder_type} reminders")

    def _trigger_auto_confirmations(self) -> None:
        """
        Trigger auto-confirmations via orchestrator.

        Uses orchestrator for state transition, notification is adapter concern.
        """
        logger.info("Processing auto-confirmations")

        from notifier import NotificationService

        with get_db_context() as db:
            notifier = NotificationService(db)
            orchestrator = self._create_orchestrator(db)

            reminder_config = self._notification_config.get("reminders", {})
            auto_confirm_hours = reminder_config.get("auto_confirm_hours", 24)

            window_start = datetime.utcnow() + timedelta(hours=auto_confirm_hours - 1)
            window_end = datetime.utcnow() + timedelta(hours=auto_confirm_hours + 1)

            appointments = db.query(Appointment).filter(
                and_(
                    Appointment.status == AppointmentStatus.SCHEDULED,
                    Appointment.appointment_time >= window_start,
                    Appointment.appointment_time <= window_end,
                    Appointment.auto_confirmed == False
                )
            ).all()

            confirmed_count = 0
            for appointment in appointments:
                try:
                    # Use orchestrator for state transition
                    result = orchestrator.confirm_workflow(
                        workflow_id=appointment.id,
                        auto=True
                    )

                    if result.success:
                        # Send notification (adapter concern)
                        notifier.send_auto_confirmation(appointment)
                        appointment.auto_confirmed = True
                        confirmed_count += 1

                except Exception as e:
                    logger.error(
                        f"Error auto-confirming appointment {appointment.id}: {e}"
                    )

            db.commit()
            if confirmed_count > 0:
                logger.info(f"Auto-confirmed {confirmed_count} appointments")

    def _trigger_calendar_sync(self) -> None:
        """Trigger Google Calendar synchronization."""
        logger.info("Triggering Google Calendar sync")

        try:
            from calendar_sync import read_google_calendar

            with get_db_context() as db:
                result = read_google_calendar(db)
                logger.info(
                    f"Calendar sync: {result.get('synced', 0)} synced, "
                    f"{result.get('skipped', 0)} skipped"
                )

        except Exception as e:
            logger.error(f"Calendar sync error: {e}", exc_info=True)

    def trigger_now(self, job_id: str) -> bool:
        """
        Manually trigger a job immediately.

        Useful for testing or manual interventions.

        Args:
            job_id: The job ID to trigger

        Returns:
            True if triggered successfully
        """
        job = self._scheduler.get_job(job_id)
        if job is None:
            logger.warning(f"Job {job_id} not found")
            return False

        try:
            job.func()
            return True
        except Exception as e:
            logger.error(f"Error triggering job {job_id}: {e}")
            return False


def create_scheduler_from_config(config: Dict[str, Any]) -> SchedulerAdapter:
    """
    Factory function to create scheduler from configuration.

    Args:
        config: Application configuration dict

    Returns:
        Configured SchedulerAdapter
    """
    return SchedulerAdapter(config)
