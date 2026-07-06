"""
SQLAlchemy Storage Adapter

Bridges the core orchestration layer with SQLAlchemy persistence.
Implements the repository protocols defined in the orchestrator.

This is an ADAPTER - it translates between:
- Core domain objects (WorkflowInstance, client data dicts)
- SQLAlchemy models (Appointment, Client)

The core remains cloud-neutral; only this adapter knows about SQLAlchemy.
"""

from typing import List, Dict, Any, Optional
from datetime import datetime, timedelta
from jenga.core.time_utils import utc_now
from sqlalchemy.orm import Session
from sqlalchemy import and_

# Import from existing models (the adapter's external dependency)
import sys
from pathlib import Path
APP_DIR = Path(__file__).parent.parent.parent.parent.resolve()
if str(APP_DIR) not in sys.path:
    sys.path.insert(0, str(APP_DIR))

from models import (
    Appointment,
    Client,
    Business,
    AppointmentStatus,
    CascadeHistory,
    EventLog,
    ShiftOffer,
    OfferStatus
)

# Import core domain objects
from jenga.core.state.workflow_state import WorkflowInstance, WorkflowStatus
from jenga.core.orchestration.orchestrator import Offer, TimeWindow


def _status_to_workflow(status: AppointmentStatus) -> WorkflowStatus:
    """Convert SQLAlchemy AppointmentStatus to core WorkflowStatus."""
    mapping = {
        AppointmentStatus.SCHEDULED: WorkflowStatus.SCHEDULED,
        AppointmentStatus.CONFIRMED: WorkflowStatus.CONFIRMED,
        AppointmentStatus.CANCELLED: WorkflowStatus.CANCELLED,
        AppointmentStatus.COMPLETED: WorkflowStatus.COMPLETED,
        AppointmentStatus.NO_SHOW: WorkflowStatus.NO_SHOW,
    }
    return mapping.get(status, WorkflowStatus.SCHEDULED)


def _status_from_workflow(status: WorkflowStatus) -> AppointmentStatus:
    """Convert core WorkflowStatus to SQLAlchemy AppointmentStatus."""
    mapping = {
        WorkflowStatus.SCHEDULED: AppointmentStatus.SCHEDULED,
        WorkflowStatus.CONFIRMED: AppointmentStatus.CONFIRMED,
        WorkflowStatus.CANCELLED: AppointmentStatus.CANCELLED,
        WorkflowStatus.COMPLETED: AppointmentStatus.COMPLETED,
        WorkflowStatus.NO_SHOW: AppointmentStatus.NO_SHOW,
    }
    return mapping.get(status, AppointmentStatus.SCHEDULED)


class SQLAlchemyWorkflowRepository:
    """
    SQLAlchemy implementation of WorkflowRepository protocol.

    Translates between Appointment models and WorkflowInstance domain objects.

    INVARIANT: This is the ONLY place where database state mutations occur.
    All state changes must flow through WorkflowInstance -> save().
    """

    def __init__(self, db: Session):
        self._db = db

    def _to_workflow(self, appointment: Appointment) -> WorkflowInstance:
        """Convert Appointment model to WorkflowInstance."""
        return WorkflowInstance(
            id=appointment.id,
            business_id=appointment.business_id,
            client_id=appointment.client_id,
            status=_status_to_workflow(appointment.status),
            appointment_time=appointment.appointment_time,
            duration_minutes=appointment.duration_minutes,
            risk_score=appointment.no_show_risk or 0.5,
            is_movable=appointment.is_movable,
            move_count=appointment.move_count or 0
        )

    def get(self, workflow_id: int) -> Optional[WorkflowInstance]:
        """Get a workflow by ID (no business isolation)."""
        appointment = self._db.query(Appointment).filter(
            Appointment.id == workflow_id
        ).first()

        if appointment is None:
            return None

        return self._to_workflow(appointment)

    def get_by_id(self, workflow_id: int, business_id: int) -> Optional[WorkflowInstance]:
        """
        Get workflow by ID with business isolation.

        REQUIRED: business_id must match for multi-tenant safety.
        """
        appointment = self._db.query(Appointment).filter(
            Appointment.id == workflow_id,
            Appointment.business_id == business_id
        ).first()

        if appointment is None:
            return None

        return self._to_workflow(appointment)

    def save(self, workflow: WorkflowInstance) -> WorkflowInstance:
        """
        Save a workflow (create or update).

        For updates, only changes allowed fields (status, risk_score, move_count).
        """
        appointment = self._db.query(Appointment).filter(
            Appointment.id == workflow.id
        ).first()

        if appointment is None:
            raise ValueError(f"Cannot save workflow {workflow.id}: not found in database")

        # Update allowed fields
        appointment.status = _status_from_workflow(workflow.status)
        appointment.no_show_risk = workflow.risk_score
        appointment.is_movable = workflow.is_movable
        appointment.move_count = workflow.move_count
        appointment.updated_at = utc_now()

        self._db.flush()
        return self._to_workflow(appointment)

    def find_by_business(
        self,
        business_id: int,
        status: Optional[WorkflowStatus] = None,
        from_time: Optional[datetime] = None,
        to_time: Optional[datetime] = None
    ) -> List[WorkflowInstance]:
        """Find workflows for a business with optional filters."""
        query = self._db.query(Appointment).filter(
            Appointment.business_id == business_id
        )

        if status is not None:
            query = query.filter(
                Appointment.status == _status_from_workflow(status)
            )

        if from_time is not None:
            query = query.filter(Appointment.appointment_time >= from_time)

        if to_time is not None:
            query = query.filter(Appointment.appointment_time <= to_time)

        query = query.order_by(Appointment.appointment_time)

        return [self._to_workflow(apt) for apt in query.all()]

    def get_active_workflows(
        self,
        business_id: int,
        from_time: Optional[datetime] = None,
        to_time: Optional[datetime] = None
    ) -> List[WorkflowInstance]:
        """
        Get active (SCHEDULED or CONFIRMED) workflows.

        Implements WorkflowRepository protocol for orchestrator.
        """
        active_statuses = [
            AppointmentStatus.SCHEDULED,
            AppointmentStatus.CONFIRMED
        ]

        query = self._db.query(Appointment).filter(
            Appointment.business_id == business_id,
            Appointment.status.in_(active_statuses)
        )

        if from_time is not None:
            query = query.filter(Appointment.appointment_time >= from_time)

        if to_time is not None:
            query = query.filter(Appointment.appointment_time <= to_time)

        query = query.order_by(Appointment.appointment_time)

        return [self._to_workflow(apt) for apt in query.all()]

    def get_cascade_candidates(
        self,
        business_id: int,
        after_time: datetime,
        min_risk: float
    ) -> List[WorkflowInstance]:
        """
        Get potential cascade candidates.

        Implements WorkflowRepository protocol for orchestrator.
        Returns movable workflows with risk >= min_risk.
        """
        terminal_statuses = [
            AppointmentStatus.CANCELLED,
            AppointmentStatus.COMPLETED,
            AppointmentStatus.NO_SHOW
        ]

        appointments = self._db.query(Appointment).filter(
            Appointment.business_id == business_id,
            Appointment.appointment_time > after_time,
            Appointment.is_movable == True,
            Appointment.no_show_risk >= min_risk,
            ~Appointment.status.in_(terminal_statuses)
        ).order_by(
            Appointment.no_show_risk.desc(),
            Appointment.appointment_time
        ).limit(50).all()

        return [self._to_workflow(apt) for apt in appointments]

    def find_cascade_candidates(
        self,
        business_id: int,
        after_time: datetime,
        limit: int = 50
    ) -> List[WorkflowInstance]:
        """
        Find workflows that could be cascade-moved.

        DEPRECATED: Use get_cascade_candidates instead.
        Returns movable, non-terminal appointments after the given time.
        """
        terminal_statuses = [
            AppointmentStatus.CANCELLED,
            AppointmentStatus.COMPLETED,
            AppointmentStatus.NO_SHOW
        ]

        appointments = self._db.query(Appointment).filter(
            Appointment.business_id == business_id,
            Appointment.appointment_time > after_time,
            Appointment.is_movable == True,
            ~Appointment.status.in_(terminal_statuses)
        ).order_by(
            Appointment.appointment_time
        ).limit(limit).all()

        return [self._to_workflow(apt) for apt in appointments]

    def find_by_time_range(
        self,
        business_id: int,
        start: datetime,
        end: datetime
    ) -> List[WorkflowInstance]:
        """Find workflows in a time range."""
        appointments = self._db.query(Appointment).filter(
            Appointment.business_id == business_id,
            Appointment.appointment_time >= start,
            Appointment.appointment_time < end
        ).order_by(Appointment.appointment_time).all()

        return [self._to_workflow(apt) for apt in appointments]

    def update_time(
        self,
        workflow_id: int,
        new_time: datetime,
        business_id: int
    ) -> WorkflowInstance:
        """Update workflow appointment time (for cascade moves).

        Tenant-scoped: the business_id filter guarantees this method can
        never mutate another tenant's appointment, regardless of caller bugs.
        """
        appointment = self._db.query(Appointment).filter(
            Appointment.id == workflow_id,
            Appointment.business_id == business_id
        ).first()

        if appointment is None:
            raise ValueError(
                f"Workflow {workflow_id} not found for business {business_id}"
            )

        appointment.appointment_time = new_time
        appointment.move_count = (appointment.move_count or 0) + 1
        appointment.last_moved_at = utc_now()
        appointment.updated_at = utc_now()

        self._db.flush()
        return self._to_workflow(appointment)

    def get_conflicting_workflows(
        self,
        business_id: int,
        start_time: datetime,
        duration_minutes: int,
        exclude_workflow_id=None
    ):
        """
        Active appointments overlapping [start_time, start_time + duration).

        Overlap rule: existing.start < new.end AND existing.end > new.start.
        The end time is derived from duration in Python for cross-database
        portability (SQLite cannot add minutes to a column in a filter), so we
        pre-filter by a conservative window and finish the check in Python.
        """
        from datetime import timedelta

        new_end = start_time + timedelta(minutes=duration_minutes)
        # No appointment longer than 24h; conservative pre-filter window.
        window_start = start_time - timedelta(hours=24)

        terminal_statuses = [
            AppointmentStatus.CANCELLED,
            AppointmentStatus.COMPLETED,
            AppointmentStatus.NO_SHOW
        ]

        query = self._db.query(Appointment).filter(
            Appointment.business_id == business_id,
            Appointment.appointment_time < new_end,
            Appointment.appointment_time > window_start,
            ~Appointment.status.in_(terminal_statuses)
        )
        if exclude_workflow_id is not None:
            query = query.filter(Appointment.id != exclude_workflow_id)

        conflicts = []
        for appointment in query.all():
            existing_end = appointment.appointment_time + timedelta(
                minutes=appointment.duration_minutes or 0
            )
            if existing_end > start_time:
                conflicts.append(self._to_workflow(appointment))
        return conflicts

    def record_cascade(
        self,
        business_id: int,
        trigger_workflow_id: int,
        moved_workflow_id: int,
        from_time: datetime,
        to_time: datetime,
        depth: int,
        score: float
    ) -> None:
        """Record a cascade move in history."""
        history = CascadeHistory(
            business_id=business_id,
            trigger_appointment_id=trigger_workflow_id,
            moved_appointment_id=moved_workflow_id,
            from_time=from_time,
            to_time=to_time,
            cascade_depth=depth,
            selection_score=score
        )
        self._db.add(history)
        self._db.flush()


class SQLAlchemyClientRepository:
    """
    SQLAlchemy implementation of ClientRepository protocol.

    Provides read access to client data for advisory calculations.

    INVARIANT: Client statistics are updated ONLY through this repository.
    Direct manipulation of Client model outside this class is a violation.
    """

    def __init__(self, db: Session):
        self._db = db

    def get_client_data(self, client_id: int) -> Dict[str, Any]:
        """Get client data as a dictionary for advisory calculations."""
        client = self._db.query(Client).filter(
            Client.id == client_id
        ).first()

        if client is None:
            return {}

        return {
            "id": client.id,
            "business_id": client.business_id,
            "segment": client.segment.value if client.segment else "regular",
            "total_appointments": client.total_appointments or 0,
            "completed_appointments": client.completed_appointments or 0,
            "cancelled_appointments": client.cancelled_appointments or 0,
            "no_show_appointments": client.no_show_appointments or 0,
            "no_show_rate": client.no_show_rate or 0.0,
            "cancellation_rate": client.cancellation_rate or 0.0,
            "is_flexible": client.is_flexible if client.is_flexible is not None else True,
        }

    def get_bulk_client_data(
        self,
        client_ids: List[int]
    ) -> Dict[int, Dict[str, Any]]:
        """Get client data for multiple clients at once."""
        if not client_ids:
            return {}

        clients = self._db.query(Client).filter(
            Client.id.in_(client_ids)
        ).all()

        return {
            client.id: {
                "id": client.id,
                "business_id": client.business_id,
                "segment": client.segment.value if client.segment else "regular",
                "total_appointments": client.total_appointments or 0,
                "completed_appointments": client.completed_appointments or 0,
                "cancelled_appointments": client.cancelled_appointments or 0,
                "no_show_appointments": client.no_show_appointments or 0,
                "no_show_rate": client.no_show_rate or 0.0,
                "cancellation_rate": client.cancellation_rate or 0.0,
                "is_flexible": client.is_flexible if client.is_flexible is not None else True,
            }
            for client in clients
        }

    def update_statistics(self, client_id: int) -> None:
        """
        Recalculate client statistics from their appointment history.

        This should be called after status changes.
        """
        client = self._db.query(Client).filter(
            Client.id == client_id
        ).first()

        if client is None:
            return

        # Count appointments by status
        appointments = self._db.query(Appointment).filter(
            Appointment.client_id == client_id
        ).all()

        total = len(appointments)
        completed = sum(1 for a in appointments if a.status == AppointmentStatus.COMPLETED)
        cancelled = sum(1 for a in appointments if a.status == AppointmentStatus.CANCELLED)
        no_shows = sum(1 for a in appointments if a.status == AppointmentStatus.NO_SHOW)

        client.total_appointments = total
        client.completed_appointments = completed
        client.cancelled_appointments = cancelled
        client.no_show_appointments = no_shows

        # Calculate rates (avoid division by zero)
        past_appointments = completed + cancelled + no_shows
        if past_appointments > 0:
            client.no_show_rate = no_shows / past_appointments
            client.cancellation_rate = cancelled / past_appointments
        else:
            client.no_show_rate = 0.0
            client.cancellation_rate = 0.0

        client.updated_at = utc_now()
        self._db.flush()

    def update_stats_on_completion(self, client_id: int) -> None:
        """
        Update client stats when appointment completes.

        Implements ClientRepository protocol for orchestrator.
        """
        self.update_statistics(client_id)

    def update_stats_on_cancellation(self, client_id: int) -> None:
        """
        Update client stats when appointment cancelled.

        Implements ClientRepository protocol for orchestrator.
        """
        self.update_statistics(client_id)

    def update_stats_on_no_show(self, client_id: int) -> None:
        """
        Update client stats when marked no-show.

        Implements ClientRepository protocol for orchestrator.
        """
        self.update_statistics(client_id)


class SQLAlchemyEventLogger:
    """
    SQLAlchemy implementation for event logging.

    Persists domain events to the EventLog table.
    """

    def __init__(self, db: Session):
        self._db = db

    def log_event(
        self,
        business_id: int,
        event_type: str,
        event_data: Dict[str, Any],
        appointment_id: Optional[int] = None
    ) -> None:
        """Log an event to the database."""
        event = EventLog(
            business_id=business_id,
            appointment_id=appointment_id,
            event_type=event_type,
            event_data=event_data
        )
        self._db.add(event)
        self._db.flush()


def _time_window_to_str(tw: TimeWindow) -> str:
    """Convert TimeWindow enum to string for storage."""
    return tw.value


def _str_to_time_window(s: Optional[str]) -> TimeWindow:
    """Convert stored string to TimeWindow enum."""
    if s is None:
        return TimeWindow.MEDIUM_TERM
    return TimeWindow(s)


def _offer_status_to_str(status: OfferStatus) -> str:
    """Convert OfferStatus enum to string."""
    return status.value


def _str_to_offer_status(s: str) -> str:
    """Convert string to offer status string for domain object."""
    return s


class SQLAlchemyOfferRepository:
    """
    SQLAlchemy implementation of OfferRepository protocol.

    Translates between ShiftOffer models and Offer domain objects.

    INVARIANT: Offer status changes are atomic and validated.
    """

    def __init__(self, db: Session):
        self._db = db

    def _to_offer(self, shift_offer: ShiftOffer) -> Offer:
        """Convert ShiftOffer model to Offer domain object."""
        return Offer(
            offer_id=str(shift_offer.id),
            business_id=shift_offer.business_id,
            workflow_id=shift_offer.appointment_id,
            client_id=shift_offer.client_id,
            from_time=shift_offer.from_time,
            to_time=shift_offer.to_time,
            expires_at=shift_offer.expires_at,
            time_window=_str_to_time_window(shift_offer.time_window),
            trigger_workflow_id=shift_offer.trigger_workflow_id,
            priority_score=shift_offer.priority_score or 0.0,
            status=shift_offer.status.value if shift_offer.status else "offered"
        )

    def create_offer(self, offer: Offer) -> Offer:
        """
        Create a new offer.

        Returns offer with assigned ID.
        """
        import uuid

        shift_offer = ShiftOffer(
            id=uuid.UUID(offer.offer_id) if offer.offer_id else uuid.uuid4(),
            business_id=offer.business_id,
            appointment_id=offer.workflow_id,
            client_id=offer.client_id,
            from_time=offer.from_time,
            to_time=offer.to_time,
            expires_at=offer.expires_at,
            status=OfferStatus.OFFERED,
            trigger_workflow_id=offer.trigger_workflow_id,
            time_window=_time_window_to_str(offer.time_window),
            priority_score=offer.priority_score
        )

        self._db.add(shift_offer)
        self._db.flush()

        return self._to_offer(shift_offer)

    def get_offer(self, offer_id: str, business_id: int) -> Optional[Offer]:
        """Get offer by ID."""
        import uuid as uuid_module

        try:
            offer_uuid = uuid_module.UUID(offer_id)
        except ValueError:
            return None

        shift_offer = self._db.query(ShiftOffer).filter(
            ShiftOffer.id == offer_uuid,
            ShiftOffer.business_id == business_id
        ).first()

        if shift_offer is None:
            return None

        return self._to_offer(shift_offer)

    def get_active_offer_for_workflow(
        self,
        workflow_id: int,
        business_id: int
    ) -> Optional[Offer]:
        """Get active (non-expired, non-responded) offer for a workflow."""
        shift_offer = self._db.query(ShiftOffer).filter(
            ShiftOffer.appointment_id == workflow_id,
            ShiftOffer.business_id == business_id,
            ShiftOffer.status == OfferStatus.OFFERED
        ).first()

        if shift_offer is None:
            return None

        return self._to_offer(shift_offer)

    def get_active_offers_for_slot(
        self,
        slot_time: datetime,
        business_id: int
    ) -> List[Offer]:
        """Get all active offers for a given slot time."""
        shift_offers = self._db.query(ShiftOffer).filter(
            ShiftOffer.to_time == slot_time,
            ShiftOffer.business_id == business_id,
            ShiftOffer.status == OfferStatus.OFFERED
        ).all()

        return [self._to_offer(so) for so in shift_offers]

    def update_offer_status(
        self,
        offer_id: str,
        status: str,
        responded_at: Optional[datetime] = None
    ) -> Offer:
        """Update offer status (accepted, declined, expired)."""
        import uuid as uuid_module

        offer_uuid = uuid_module.UUID(offer_id)

        shift_offer = self._db.query(ShiftOffer).filter(
            ShiftOffer.id == offer_uuid
        ).first()

        if shift_offer is None:
            raise ValueError(f"Offer {offer_id} not found")

        # Map status string to enum
        status_map = {
            "offered": OfferStatus.OFFERED,
            "accepted": OfferStatus.ACCEPTED,
            "declined": OfferStatus.DECLINED,
            "expired": OfferStatus.EXPIRED
        }

        shift_offer.status = status_map.get(status, OfferStatus.OFFERED)
        shift_offer.responded_at = responded_at

        self._db.flush()
        return self._to_offer(shift_offer)

    def count_offers_for_slot(
        self,
        slot_time: datetime,
        business_id: int,
        trigger_workflow_id: int
    ) -> int:
        """Count how many offers have been made for this slot."""
        count = self._db.query(ShiftOffer).filter(
            ShiftOffer.to_time == slot_time,
            ShiftOffer.business_id == business_id,
            ShiftOffer.trigger_workflow_id == trigger_workflow_id
        ).count()

        return count

    def get_expired_offers(
        self,
        business_id: int,
        current_time: datetime
    ) -> List[Offer]:
        """Get all offers that have expired but are still in OFFERED status."""
        shift_offers = self._db.query(ShiftOffer).filter(
            ShiftOffer.business_id == business_id,
            ShiftOffer.status == OfferStatus.OFFERED,
            ShiftOffer.expires_at < current_time
        ).all()

        return [self._to_offer(so) for so in shift_offers]

    def get_offers_for_trigger(
        self,
        trigger_workflow_id: int,
        business_id: int
    ) -> List[Offer]:
        """All offers (any status) created for a given cancelled slot."""
        shift_offers = self._db.query(ShiftOffer).filter(
            ShiftOffer.business_id == business_id,
            ShiftOffer.trigger_workflow_id == trigger_workflow_id
        ).all()
        return [self._to_offer(so) for so in shift_offers]


def create_repositories(db: Session) -> tuple:
    """
    Factory function to create all repositories from a database session.

    Returns:
        Tuple of (workflow_repository, client_repository, event_logger, offer_repository)
    """
    return (
        SQLAlchemyWorkflowRepository(db),
        SQLAlchemyClientRepository(db),
        SQLAlchemyEventLogger(db),
        SQLAlchemyOfferRepository(db)
    )
