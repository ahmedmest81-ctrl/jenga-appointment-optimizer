"""
Engine Core - Legacy Compatibility Wrapper

This module provides backward compatibility with existing code
while delegating to the new service layer architecture.

For new code, use services directly:
- services.CascadeService for optimization
- services.AppointmentService for appointment operations
"""

from sqlalchemy.orm import Session
from datetime import datetime
from typing import Optional
import logging

from config_loader import config

logger = logging.getLogger(__name__)


class EngineCore:
    """
    Legacy engine core wrapper.

    Delegates to new service layer for actual implementation.
    Maintained for backward compatibility with existing code.
    """

    def __init__(
        self,
        db: Session,
        notifier=None,
        reservation_expiry_minutes: Optional[int] = None,
        max_depth: Optional[int] = None
    ):
        """
        Initialize engine core.

        Args:
            db: Database session
            notifier: Notification service (legacy, optional)
            reservation_expiry_minutes: Reservation expiry (legacy, uses config)
            max_depth: Maximum cascade depth (legacy, uses config)
        """
        self.db = db
        self.notifier = notifier
        self.config = config

        # Use config values, but allow override for backward compatibility
        self.reservation_expiry_minutes = (
            reservation_expiry_minutes or
            config.engine.cascade.shift_offer_expiry_minutes
        )
        self.max_depth = max_depth or config.engine.cascade.max_depth

        # In-memory store for shift offers (legacy)
        self.shift_offers = {}

    def trigger_cascade(
        self,
        slot_time: datetime,
        business_id: int,
        provider_id: Optional[str] = None
    ) -> int:
        """
        Launch cascade optimization.

        Args:
            slot_time: Time of cancelled slot
            business_id: Business ID
            provider_id: Provider ID (optional)

        Returns:
            Number of moves made
        """
        # Import here to avoid circular imports
        from services.cascade_service import CascadeService

        cascade_service = CascadeService(self.db)

        try:
            # Get candidates
            candidates = cascade_service.get_cascade_candidates(
                cancelled_time=slot_time,
                business_id=business_id,
                limit=10
            )

            logger.info(
                f"Cascade triggered for slot {slot_time.isoformat()}: "
                f"{len(candidates)} candidates found"
            )

            # In full implementation, would process candidates here
            # For now, just return count
            return len(candidates)

        except Exception as e:
            logger.error(f"Cascade failed: {e}")
            return 0

    def calculate_all_risk_scores(self, business_id: int) -> int:
        """
        Recalculate risk scores for all appointments.

        Args:
            business_id: Business ID

        Returns:
            Number of appointments updated
        """
        # Import here to avoid circular imports
        from services.cascade_service import CascadeService

        cascade_service = CascadeService(self.db)
        return cascade_service.calculate_all_risk_scores(business_id)

    def identify_risky_appointments(
        self,
        business_id: int,
        days_ahead: Optional[int] = None
    ) -> list:
        """
        Identify high-risk appointments.

        Args:
            business_id: Business ID
            days_ahead: Look-ahead window in days

        Returns:
            List of high-risk appointments
        """
        # Import here to avoid circular imports
        from services.cascade_service import CascadeService

        cascade_service = CascadeService(self.db)
        return cascade_service.identify_risky_appointments(
            business_id,
            days_ahead
        )

    def handle_cancellation(self, appointment) -> int:
        """
        Handle appointment cancellation.

        Args:
            appointment: Appointment being cancelled

        Returns:
            Number of cascade moves made
        """
        # Import here to avoid circular imports
        from services.cascade_service import CascadeService

        cascade_service = CascadeService(self.db)
        result = cascade_service.handle_cancellation(appointment)
        return result.get("moves_count", 0)

    def _update_risk_score(self, appointment) -> None:
        """
        Update risk score for appointment.

        Args:
            appointment: Appointment to update
        """
        # Import here to avoid circular imports
        from services.cascade_service import CascadeService

        cascade_service = CascadeService(self.db)
        cascade_service._update_risk_score(appointment)

    def optimize_schedule(self, business_id: int) -> dict:
        """
        Run schedule optimization.

        Args:
            business_id: Business ID

        Returns:
            Optimization results dictionary
        """
        # Import here to avoid circular imports
        from services.cascade_service import CascadeService

        cascade_service = CascadeService(self.db)
        return cascade_service.optimize_schedule(business_id)

    def _expire_offers(self) -> None:
        """Expire old shift offers (legacy method)"""
        # Implementation moved to cascade service
        pass

    def accept_offer(self, offer_id: str) -> dict:
        """
        Accept a shift offer (legacy method).

        Args:
            offer_id: Offer ID

        Returns:
            Result dictionary
        """
        logger.warning("accept_offer called on legacy EngineCore - not implemented")
        return {"success": False, "message": "Not implemented"}

    def reject_offer(self, offer_id: str) -> dict:
        """
        Reject a shift offer (legacy method).

        Args:
            offer_id: Offer ID

        Returns:
            Result dictionary
        """
        logger.warning("reject_offer called on legacy EngineCore - not implemented")
        return {"success": False, "message": "Not implemented"}


# ===== Legacy Compatibility =====

# For code that imports Notifier from engine_core
class Notifier:
    """
    Legacy Notifier stub.

    For backward compatibility. New code should use:
    from notifier import NotificationService
    """

    def __init__(self, *args, **kwargs):
        logger.warning(
            "Legacy Notifier class used. "
            "Please migrate to: from notifier import NotificationService"
        )

    def send_notification(self, *args, **kwargs):
        """Legacy method - not implemented"""
        logger.warning("Legacy Notifier.send_notification called - not implemented")
        return False
