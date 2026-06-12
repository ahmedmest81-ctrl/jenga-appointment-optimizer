from sqlalchemy.orm import Session
from models import EventLog
from typing import Dict, Optional
from datetime import datetime
import json


class EventLogger:
    """Centralized event logging for analytics and billing."""
    
    EVENT_TYPES = {
        "appointment_created": "appointment.created",
        "appointment_cancelled": "appointment.cancelled",
        "appointment_completed": "appointment.completed",
        "appointment_no_show": "appointment.no_show",
        "risk_calculated": "risk.calculated",
        "cascade_triggered": "cascade.triggered",
        "cascade_move": "cascade.move",
        "cascade_completed": "cascade.completed",
        "reminder_sent": "reminder.sent",
        "auto_confirmed": "appointment.auto_confirmed",
        "optimization_run": "optimization.run",
    }
    
    def __init__(self, db: Session):
        self.db = db
    
    def log_event(
        self,
        business_id: int,
        event_type: str,
        event_data: Dict,
        appointment_id: Optional[int] = None
    ) -> EventLog:
        """Log an event to the database."""
        
        event = EventLog(
            business_id=business_id,
            appointment_id=appointment_id,
            event_type=event_type,
            event_data=event_data
        )
        
        self.db.add(event)
        self.db.commit()
        self.db.refresh(event)
        
        return event
    
    def log_appointment_created(
        self,
        appointment_id: int,
        business_id: int,
        client_id: int,
        appointment_time: datetime
    ):
        """Log appointment creation."""
        return self.log_event(
            business_id=business_id,
            event_type=self.EVENT_TYPES["appointment_created"],
            appointment_id=appointment_id,
            event_data={
                "client_id": client_id,
                "appointment_time": appointment_time.isoformat(),
            }
        )
    
    def log_appointment_cancelled(
        self,
        appointment_id: int,
        business_id: int,
        reason: Optional[str] = None
    ):
        """Log appointment cancellation."""
        return self.log_event(
            business_id=business_id,
            event_type=self.EVENT_TYPES["appointment_cancelled"],
            appointment_id=appointment_id,
            event_data={
                "reason": reason,
                "cancelled_at": datetime.utcnow().isoformat()
            }
        )
    
    def log_risk_calculation(
        self,
        appointment_id: int,
        business_id: int,
        risk_score: float,
        model_version: str,
        features: Dict
    ):
        """Log ML risk calculation."""
        return self.log_event(
            business_id=business_id,
            event_type=self.EVENT_TYPES["risk_calculated"],
            appointment_id=appointment_id,
            event_data={
                "risk_score": risk_score,
                "model_version": model_version,
                "features": features
            }
        )
    
    def log_cascade_triggered(
        self,
        business_id: int,
        trigger_appointment_id: int,
        reason: str
    ):
        """Log cascade optimization trigger."""
        return self.log_event(
            business_id=business_id,
            event_type=self.EVENT_TYPES["cascade_triggered"],
            appointment_id=trigger_appointment_id,
            event_data={
                "reason": reason,
                "triggered_at": datetime.utcnow().isoformat()
            }
        )
    
    def log_cascade_move(
        self,
        business_id: int,
        moved_appointment_id: int,
        from_time: datetime,
        to_time: datetime,
        cascade_depth: int,
        selection_score: float
    ):
        """Log individual cascade move."""
        return self.log_event(
            business_id=business_id,
            event_type=self.EVENT_TYPES["cascade_move"],
            appointment_id=moved_appointment_id,
            event_data={
                "from_time": from_time.isoformat(),
                "to_time": to_time.isoformat(),
                "cascade_depth": cascade_depth,
                "selection_score": selection_score,
                "time_saved_hours": (from_time - to_time).total_seconds() / 3600
            }
        )
    
    def log_cascade_completed(
        self,
        business_id: int,
        trigger_appointment_id: int,
        total_moves: int,
        execution_time_ms: float
    ):
        """Log cascade completion."""
        return self.log_event(
            business_id=business_id,
            event_type=self.EVENT_TYPES["cascade_completed"],
            appointment_id=trigger_appointment_id,
            event_data={
                "total_moves": total_moves,
                "execution_time_ms": execution_time_ms,
                "completed_at": datetime.utcnow().isoformat()
            }
        )
    
    def log_reminder_sent(
        self,
        appointment_id: int,
        business_id: int,
        reminder_type: str,
        channel: str,
        success: bool
    ):
        """Log reminder notification."""
        return self.log_event(
            business_id=business_id,
            event_type=self.EVENT_TYPES["reminder_sent"],
            appointment_id=appointment_id,
            event_data={
                "reminder_type": reminder_type,
                "channel": channel,
                "success": success,
                "sent_at": datetime.utcnow().isoformat()
            }
        )
    
    def log_optimization_run(
        self,
        business_id: int,
        appointments_processed: int,
        cascades_triggered: int,
        total_moves: int,
        execution_time_ms: float
    ):
        """Log daily optimization run."""
        return self.log_event(
            business_id=business_id,
            event_type=self.EVENT_TYPES["optimization_run"],
            event_data={
                "appointments_processed": appointments_processed,
                "cascades_triggered": cascades_triggered,
                "total_moves": total_moves,
                "execution_time_ms": execution_time_ms,
                "run_at": datetime.utcnow().isoformat()
            }
        )


# Module-level helper function for service layer compatibility
def log_event(
    db: Session,
    business_id: int,
    event_type: str,
    event_data: Dict,
    appointment_id: Optional[int] = None
) -> EventLog:
    """
    Helper function to log events without instantiating EventLogger.

    This function provides a simpler interface for service layer usage,
    allowing direct imports: `from events import log_event`

    Args:
        db: Database session
        business_id: Business identifier
        event_type: Type of event (use EventLogger.EVENT_TYPES)
        event_data: Event metadata dictionary
        appointment_id: Optional appointment ID

    Returns:
        EventLog: The created event log record
    """
    logger = EventLogger(db)
    return logger.log_event(
        business_id=business_id,
        event_type=event_type,
        event_data=event_data,
        appointment_id=appointment_id
    )