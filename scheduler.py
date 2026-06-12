"""
Scheduler - Background Job Management

Manages automated system operations:
- Daily optimization (risk recalculation, opportunity identification)
- Appointment reminders (7-day, 48-hour)
- Auto-confirmations (24-hour)
- Google Calendar synchronization

Uses new service layer architecture for business logic.
"""

from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.triggers.cron import CronTrigger
from apscheduler.triggers.interval import IntervalTrigger
from datetime import datetime, timedelta
from sqlalchemy.orm import Session
from sqlalchemy import and_
import logging
import time

from database import SessionLocal, get_db_context
from models import Appointment, AppointmentStatus, Business
from config_loader import config
from services.cascade_service import CascadeService
from services.appointment_service import AppointmentService
from notifier import NotificationService
from events import EventLogger
from ml import MLEngineV2
from calendar_sync import read_google_calendar

logger = logging.getLogger(__name__)


class JengaScheduler:
    """
    Background scheduler for automated optimization and reminders.

    Uses service layer for business logic orchestration.
    Configuration-driven timing and intervals.
    """

    def __init__(self):
        """Initialize scheduler with configuration."""
        self.scheduler = BackgroundScheduler()
        self.config = config

    def start(self):
        """Start the scheduler with all configured jobs."""
        if not self.config.scheduler.enabled:
            logger.info("Jenga scheduler is disabled by configuration")
            return

        # Daily optimization at configured hour (default: 2 AM)
        if self.config.features.enable_cascade_optimization:
            self.scheduler.add_job(
                func=self.daily_optimization,
                trigger=CronTrigger(
                    hour=self.config.scheduler.daily_optimization_hour,
                    minute=0
                ),
                id="daily_optimization",
                name="Daily Calendar Optimization",
                replace_existing=True
            )

        # Check for reminders at configured interval (default: every hour)
        if self.config.features.enable_notifications:
            self.scheduler.add_job(
                func=self.process_reminders,
                trigger=IntervalTrigger(
                    hours=self.config.scheduler.reminder_check_interval_hours
                ),
                id="process_reminders",
                name="Process Appointment Reminders",
                replace_existing=True
            )

        # Check for auto-confirmations every hour
        if self.config.features.enable_auto_confirmation:
            self.scheduler.add_job(
                func=self.process_auto_confirmations,
                trigger=IntervalTrigger(hours=1),
                id="process_auto_confirmations",
                name="Process Auto-Confirmations",
                replace_existing=True
            )

        # Google Calendar sync at configured interval (merged from background_scheduler)
        if self.config.features.enable_google_calendar_sync:
            self.scheduler.add_job(
                func=self.sync_google_calendar,
                trigger=IntervalTrigger(
                    minutes=self.config.scheduler.google_calendar_sync_minutes
                ),
                id="google_calendar_sync",
                name="Google Calendar Sync",
                replace_existing=True
            )
            logger.info(
                f"Google Calendar sync enabled: every "
                f"{self.config.scheduler.google_calendar_sync_minutes} minutes"
            )

        self.scheduler.start()
        logger.info(
            f"Jenga scheduler started (optimization: "
            f"{self.config.scheduler.daily_optimization_hour}:00, "
            f"reminders: every {self.config.scheduler.reminder_check_interval_hours}h)"
        )

    def shutdown(self):
        """Shutdown the scheduler gracefully."""
        if self.scheduler.running:
            self.scheduler.shutdown()
            logger.info("Jenga scheduler stopped")

    def daily_optimization(self):
        """
        Daily optimization run for all active businesses.

        Operations:
        - Recalculate risk scores for all appointments
        - Identify high-risk appointments
        - Log analytics and metrics

        Uses CascadeService for business logic.
        """
        logger.info("Starting daily optimization run")
        start_time = time.time()

        with get_db_context() as db:
            businesses = db.query(Business).filter(Business.is_active == True).all()

            total_appointments = 0
            total_high_risk = 0

            for business in businesses:
                try:
                    # Initialize services with config
                    ml_engine = MLEngineV2(self.config.ml)
                    cascade_service = CascadeService(
                        db=db,
                        ml_engine=ml_engine,
                        notification_service=NotificationService(db)
                    )
                    event_logger = EventLogger(db)

                    # Recalculate all risk scores
                    appointments_processed = cascade_service.calculate_all_risk_scores(
                        business.id
                    )
                    total_appointments += appointments_processed

                    # Identify high-risk appointments
                    risky = cascade_service.identify_risky_appointments(
                        business.id,
                        days_ahead=self.config.engine.appointment_window_days
                    )
                    total_high_risk += len(risky)

                    logger.info(
                        f"Business {business.id}: {appointments_processed} appointments, "
                        f"{len(risky)} high-risk"
                    )

                    # Log optimization event
                    event_logger.log_event(
                        business_id=business.id,
                        event_type="scheduler.daily_optimization",
                        event_data={
                            "appointments_processed": appointments_processed,
                            "high_risk_count": len(risky),
                            "timestamp": datetime.utcnow().isoformat()
                        }
                    )

                except Exception as e:
                    logger.error(
                        f"Error optimizing business {business.id}: {str(e)}",
                        exc_info=True
                    )

            execution_time = (time.time() - start_time) * 1000
            logger.info(
                f"Daily optimization completed: {total_appointments} appointments, "
                f"{total_high_risk} high-risk, {execution_time:.2f}ms"
            )

    def process_reminders(self):
        """
        Process 7-day and 48-hour reminders for upcoming appointments.

        Uses configuration for reminder timing windows.
        """
        logger.info("Processing appointment reminders")

        with get_db_context() as db:
            notifier = NotificationService(db)
            event_logger = EventLogger(db)

            # 7-day reminders
            seven_day_hours = self.config.notifications.reminders.seven_day_hours
            seven_day_window_start = datetime.utcnow() + timedelta(
                hours=seven_day_hours - 1
            )
            seven_day_window_end = datetime.utcnow() + timedelta(
                hours=seven_day_hours + 1
            )

            seven_day_appointments = db.query(Appointment).filter(
                and_(
                    Appointment.status == AppointmentStatus.SCHEDULED,
                    Appointment.appointment_time >= seven_day_window_start,
                    Appointment.appointment_time <= seven_day_window_end,
                    Appointment.reminder_7_day_sent == False
                )
            ).all()

            for appointment in seven_day_appointments:
                try:
                    status = notifier.send_7_day_reminder(appointment)
                    appointment.reminder_7_day_sent = True

                    event_logger.log_reminder_sent(
                        appointment_id=appointment.id,
                        business_id=appointment.business_id,
                        reminder_type="7_day",
                        channel="multi",
                        success=status.value == "success"
                    )

                except Exception as e:
                    logger.error(
                        f"Error sending 7-day reminder for appointment "
                        f"{appointment.id}: {str(e)}"
                    )

            db.commit()
            logger.info(f"Sent {len(seven_day_appointments)} 7-day reminders")

            # 48-hour reminders
            forty_eight_hours = self.config.notifications.reminders.forty_eight_hour_hours
            forty_eight_window_start = datetime.utcnow() + timedelta(
                hours=forty_eight_hours - 1
            )
            forty_eight_window_end = datetime.utcnow() + timedelta(
                hours=forty_eight_hours + 1
            )

            forty_eight_appointments = db.query(Appointment).filter(
                and_(
                    Appointment.status == AppointmentStatus.SCHEDULED,
                    Appointment.appointment_time >= forty_eight_window_start,
                    Appointment.appointment_time <= forty_eight_window_end,
                    Appointment.reminder_48_hour_sent == False
                )
            ).all()

            for appointment in forty_eight_appointments:
                try:
                    status = notifier.send_48_hour_reminder(appointment)
                    appointment.reminder_48_hour_sent = True

                    event_logger.log_reminder_sent(
                        appointment_id=appointment.id,
                        business_id=appointment.business_id,
                        reminder_type="48_hour",
                        channel="multi",
                        success=status.value == "success"
                    )

                except Exception as e:
                    logger.error(
                        f"Error sending 48-hour reminder for appointment "
                        f"{appointment.id}: {str(e)}"
                    )

            db.commit()
            logger.info(f"Sent {len(forty_eight_appointments)} 48-hour reminders")

    def process_auto_confirmations(self):
        """
        Process 24-hour auto-confirmations for appointments.

        Automatically confirms appointments 24 hours before appointment time.
        Uses state machine validation through service layer (future enhancement).
        """
        logger.info("Processing auto-confirmations")

        with get_db_context() as db:
            notifier = NotificationService(db)
            event_logger = EventLogger(db)

            # 24-hour window
            auto_confirm_hours = self.config.notifications.reminders.auto_confirm_hours
            confirm_window_start = datetime.utcnow() + timedelta(
                hours=auto_confirm_hours - 1
            )
            confirm_window_end = datetime.utcnow() + timedelta(
                hours=auto_confirm_hours + 1
            )

            appointments = db.query(Appointment).filter(
                and_(
                    Appointment.status == AppointmentStatus.SCHEDULED,
                    Appointment.appointment_time >= confirm_window_start,
                    Appointment.appointment_time <= confirm_window_end,
                    Appointment.auto_confirmed == False
                )
            ).all()

            for appointment in appointments:
                try:
                    # Send confirmation notification
                    status = notifier.send_auto_confirmation(appointment)

                    # Update appointment status
                    # TODO: Use AppointmentService for proper state validation
                    appointment.auto_confirmed = True
                    appointment.status = AppointmentStatus.CONFIRMED

                    event_logger.log_event(
                        business_id=appointment.business_id,
                        event_type="appointment.auto_confirmed",
                        appointment_id=appointment.id,
                        event_data={
                            "confirmed_at": datetime.utcnow().isoformat(),
                            "notification_success": status.value == "success"
                        }
                    )

                except Exception as e:
                    logger.error(
                        f"Error auto-confirming appointment {appointment.id}: {str(e)}"
                    )

            db.commit()
            logger.info(f"Auto-confirmed {len(appointments)} appointments")

    def sync_google_calendar(self):
        """
        Synchronize with Google Calendar.

        Merged from background_scheduler.py.
        Uses calendar_sync module for integration logic.
        """
        logger.info("Starting Google Calendar sync")

        try:
            with get_db_context() as db:
                # Call calendar sync integration
                read_google_calendar(db)
                logger.info("Google Calendar sync completed")

        except Exception as e:
            logger.error(f"Error syncing Google Calendar: {str(e)}", exc_info=True)


# Global scheduler instance
scheduler = JengaScheduler()
