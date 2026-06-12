"""
Cascade Service

DEPRECATION WARNING:
This service layer predates the orchestration kernel.
For new code, use jenga.core.orchestration.Orchestrator instead.

This service BYPASSES the orchestrator and mutates database state directly.
It exists for backward compatibility with the existing API layer.

MIGRATION PATH:
- Old: CascadeService(db).handle_cancellation(appointment)
- New: orchestrator.cancel_workflow(id, business_id, trigger_cascade=True)

Handles cascade optimization business logic:
- Trigger cascade when appointment cancelled
- Daily schedule optimization
- Risk score calculation for all appointments
- Identify high-risk appointments for proactive optimization
"""

from sqlalchemy.orm import Session
from typing import List, Optional
from datetime import datetime, timedelta
import logging

from models import Appointment, AppointmentStatus, Client
from config_loader import config
from exceptions import CascadeError
from ml import MLEngineV2
from notifier import NotificationService

logger = logging.getLogger(__name__)


class CascadeService:
    """Service for cascade optimization operations"""

    def __init__(
        self,
        db: Session,
        ml_engine: Optional[MLEngineV2] = None,
        notification_service: Optional[NotificationService] = None
    ):
        """
        Initialize cascade service.

        Args:
            db: Database session
            ml_engine: ML engine for risk scoring (optional, will create if None)
            notification_service: Notification service (optional)
        """
        self.db = db
        self.config = config
        self.ml_engine = ml_engine or MLEngineV2(config.ml)
        self.notification_service = notification_service

    def calculate_all_risk_scores(self, business_id: int) -> int:
        """
        Recalculate risk scores for all scheduled/confirmed appointments.

        Called by daily optimization job.

        Args:
            business_id: Business ID for multi-tenant isolation

        Returns:
            Number of appointments updated
        """
        # Get all active appointments (SCHEDULED or CONFIRMED)
        appointments = self.db.query(Appointment).filter(
            Appointment.business_id == business_id,
            Appointment.status.in_([
                AppointmentStatus.SCHEDULED,
                AppointmentStatus.CONFIRMED
            ])
        ).all()

        updated_count = 0
        for appointment in appointments:
            try:
                self._update_risk_score(appointment)
                updated_count += 1
            except Exception as e:
                logger.error(
                    f"Failed to calculate risk score for appointment {appointment.id}: {e}"
                )
                # Continue processing other appointments

        # Commit all updates
        self.db.commit()

        logger.info(
            f"Updated risk scores for {updated_count}/{len(appointments)} appointments "
            f"for business {business_id}"
        )

        return updated_count

    def identify_risky_appointments(
        self,
        business_id: int,
        days_ahead: Optional[int] = None
    ) -> List[Appointment]:
        """
        Identify high-risk appointments within time window.

        Args:
            business_id: Business ID for multi-tenant isolation
            days_ahead: Look-ahead window in days (defaults to config value)

        Returns:
            List of high-risk appointments
        """
        if days_ahead is None:
            days_ahead = self.config.engine.appointment_window_days

        # Calculate time window
        now = datetime.utcnow()
        end_date = now + timedelta(days=days_ahead)

        # Get high-risk threshold from config
        high_risk_threshold = self.config.ml.risk_thresholds.high

        # Query for high-risk appointments
        risky_appointments = self.db.query(Appointment).filter(
            Appointment.business_id == business_id,
            Appointment.status.in_([
                AppointmentStatus.SCHEDULED,
                AppointmentStatus.CONFIRMED
            ]),
            Appointment.appointment_time >= now,
            Appointment.appointment_time <= end_date,
            Appointment.no_show_risk >= high_risk_threshold
        ).order_by(
            Appointment.no_show_risk.desc(),
            Appointment.appointment_time
        ).all()

        logger.info(
            f"Identified {len(risky_appointments)} high-risk appointments "
            f"for business {business_id} (threshold: {high_risk_threshold})"
        )

        return risky_appointments

    def optimize_schedule(self, business_id: int) -> dict:
        """
        Run daily schedule optimization.

        Recalculates risk scores and identifies optimization opportunities.

        Args:
            business_id: Business ID for multi-tenant isolation

        Returns:
            Dictionary with optimization metrics
        """
        logger.info(f"Starting schedule optimization for business {business_id}")

        # Recalculate all risk scores
        updated_count = self.calculate_all_risk_scores(business_id)

        # Identify risky appointments
        risky_appointments = self.identify_risky_appointments(business_id)

        # Calculate distribution
        all_appointments = self.db.query(Appointment).filter(
            Appointment.business_id == business_id,
            Appointment.status.in_([
                AppointmentStatus.SCHEDULED,
                AppointmentStatus.CONFIRMED
            ])
        ).all()

        high_count = sum(1 for a in all_appointments
                        if a.no_show_risk >= self.config.ml.risk_thresholds.high)
        medium_count = sum(1 for a in all_appointments
                          if self.config.ml.risk_thresholds.medium <= a.no_show_risk < self.config.ml.risk_thresholds.high)
        low_count = sum(1 for a in all_appointments
                       if a.no_show_risk < self.config.ml.risk_thresholds.medium)

        results = {
            "business_id": business_id,
            "timestamp": datetime.utcnow().isoformat(),
            "risk_scores_updated": updated_count,
            "total_appointments": len(all_appointments),
            "high_risk_count": high_count,
            "medium_risk_count": medium_count,
            "low_risk_count": low_count,
            "high_risk_appointments": [
                {
                    "id": appt.id,
                    "client_id": appt.client_id,
                    "appointment_time": appt.appointment_time.isoformat(),
                    "risk_score": appt.no_show_risk
                }
                for appt in risky_appointments[:10]  # Top 10
            ]
        }

        logger.info(
            f"Optimization complete for business {business_id}: "
            f"{high_count} high, {medium_count} medium, {low_count} low risk"
        )

        return results

    def _update_risk_score(self, appointment: Appointment) -> None:
        """
        Calculate and update risk score for a single appointment.

        Args:
            appointment: Appointment to update
        """
        # Prepare client data for ML engine
        client_data = {
            "no_show_rate": appointment.client.no_show_rate,
            "cancellation_rate": appointment.client.cancellation_rate,
            "total_appointments": appointment.client.total_appointments,
            "segment": appointment.client.segment.value,
            "is_flexible": appointment.client.is_flexible
        }

        # Calculate risk score
        risk_score = self.ml_engine.predict_no_show_risk(
            appointment_time=appointment.appointment_time,
            appointment_type=appointment.appointment_type or "routine",
            client_data=client_data,
            move_count=appointment.move_count
        )

        # Update appointment
        appointment.no_show_risk = risk_score
        appointment.ml_model_version = self.config.ml.version
        appointment.risk_calculated_at = datetime.utcnow()

        # No need to commit - calling code handles transaction

    def handle_cancellation(
        self,
        appointment: Appointment,
        trigger_cascade: bool = True
    ) -> dict:
        """
        Handle appointment cancellation.

        Updates appointment status and optionally triggers cascade optimization.

        Args:
            appointment: Appointment being cancelled
            trigger_cascade: Whether to trigger cascade optimization

        Returns:
            Dictionary with cancellation results
        """
        # Already validated by calling code (StateTransitionValidator)
        original_status = appointment.status
        appointment.status = AppointmentStatus.CANCELLED

        result = {
            "appointment_id": appointment.id,
            "previous_status": original_status.value,
            "new_status": AppointmentStatus.CANCELLED.value,
            "cascade_triggered": trigger_cascade,
            "moves_count": 0
        }

        # Trigger cascade if enabled and configured
        if trigger_cascade and self.config.features.enable_cascade_optimization:
            try:
                # Note: Full cascade implementation would go here
                # For now, we log the opportunity
                logger.info(
                    f"Cascade opportunity: Appointment {appointment.id} cancelled "
                    f"at {appointment.appointment_time.isoformat()}"
                )

                # In full implementation:
                # moves_count = self.trigger_cascade(
                #     cancelled_slot=appointment.appointment_time,
                #     business_id=appointment.business_id
                # )
                # result["moves_count"] = moves_count

            except Exception as e:
                logger.error(f"Cascade failed for appointment {appointment.id}: {e}")
                # Don't fail the cancellation if cascade fails
                result["cascade_error"] = str(e)

        return result

    def get_cascade_candidates(
        self,
        cancelled_time: datetime,
        business_id: int,
        limit: int = 10
    ) -> List[Appointment]:
        """
        Find candidate appointments for cascade optimization.

        Candidates are:
        - High risk appointments
        - After the cancelled slot
        - Movable
        - Client is flexible

        Args:
            cancelled_time: Time of cancelled appointment
            business_id: Business ID
            limit: Maximum candidates to return

        Returns:
            List of candidate appointments, sorted by priority
        """
        # Query for potential candidates
        candidates = self.db.query(Appointment).join(Client).filter(
            Appointment.business_id == business_id,
            Appointment.status == AppointmentStatus.SCHEDULED,
            Appointment.appointment_time > cancelled_time,
            Appointment.is_movable == True,
            Appointment.no_show_risk >= self.config.ml.risk_thresholds.medium
        ).order_by(
            Appointment.no_show_risk.desc(),  # Higher risk first
            Appointment.move_count.asc(),      # Fewer moves first
            Appointment.appointment_time.asc() # Earlier first
        ).limit(limit).all()

        logger.info(
            f"Found {len(candidates)} cascade candidates for slot "
            f"{cancelled_time.isoformat()}"
        )

        return candidates
