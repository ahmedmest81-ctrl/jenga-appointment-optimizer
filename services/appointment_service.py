"""
Appointment Service

DEPRECATION WARNING:
This service layer predates the orchestration kernel.
For new code, use jenga.core.orchestration.Orchestrator instead.

This service BYPASSES the orchestrator and mutates database state directly.
It exists for backward compatibility with the existing API layer.

MIGRATION PATH:
- Old: AppointmentService(db).cancel_appointment(id, business_id)
- New: orchestrator.cancel_workflow(id, business_id)

Core business logic for appointment operations:
- Create appointments with full validation
- Cancel appointments with cascade triggering
- Complete appointments
- Mark no-shows
- Update appointment status with state validation

This service coordinates between:
- State machine (validation)
- Temporal validator (time constraints)
- Client service (statistics updates)
- Cascade service (optimization)
- ML engine (risk scoring)
- Event logging
"""
import warnings

from sqlalchemy.orm import Session
from typing import Optional
from datetime import datetime
import logging

from models import Appointment, AppointmentStatus, Business, EventLog
from config_loader import config
from exceptions import (
    InvalidStateTransitionError,
    ResourceNotFoundError,
    ValidationError
)
from state_machine import StateTransitionValidator, TemporalValidator
from services.client_service import ClientService
from services.cascade_service import CascadeService
from ml import MLEngineV2
from utils import parse_datetime
from events import log_event

logger = logging.getLogger(__name__)


class AppointmentService:
    """Service for appointment management operations"""

    def __init__(
        self,
        db: Session,
        client_service: Optional[ClientService] = None,
        cascade_service: Optional[CascadeService] = None,
        ml_engine: Optional[MLEngineV2] = None
    ):
        """
        Initialize appointment service.

        Args:
            db: Database session
            client_service: Client service (optional, will create if None)
            cascade_service: Cascade service (optional, will create if None)
            ml_engine: ML engine (optional, will create if None)
        """
        self.db = db
        self.config = config
        self.client_service = client_service or ClientService(db)
        self.cascade_service = cascade_service or CascadeService(db, ml_engine)
        self.ml_engine = ml_engine or MLEngineV2(config.ml)
        self.temporal_validator = TemporalValidator(config.validation)

    def create_appointment(
        self,
        business_id: int,
        client_external_id: str,
        external_id: str,
        appointment_time_str: str,
        duration_minutes: int = 60,
        appointment_type: Optional[str] = "routine",
        provider_id: Optional[str] = None,
        is_movable: bool = True
    ) -> Appointment:
        """
        Create new appointment with full validation and risk scoring.

        Args:
            business_id: Business ID
            client_external_id: Client's external ID
            external_id: Appointment's external ID
            appointment_time_str: Appointment time (ISO8601 string)
            duration_minutes: Duration in minutes
            appointment_type: Type of appointment
            provider_id: Provider ID (optional)
            is_movable: Whether appointment can be moved

        Returns:
            Created appointment

        Raises:
            ValidationError: If validation fails
            ResourceNotFoundError: If client not found
            OverlapError: If appointment overlaps
        """
        # Parse appointment time
        try:
            appointment_time = parse_datetime(appointment_time_str)
        except Exception as e:
            raise ValidationError(f"Invalid appointment time format: {e}")

        # Get business
        business = self.db.query(Business).filter(Business.id == business_id).first()
        if not business:
            raise ResourceNotFoundError(f"Business {business_id} not found")

        # Get client
        client = self.client_service.get_client_by_external_id(
            client_external_id,
            business_id
        )
        if not client:
            raise ResourceNotFoundError(
                f"Client {client_external_id} not found for business {business_id}"
            )

        # Temporal validation
        self.temporal_validator.validate_all(
            db=self.db,
            appointment_time=appointment_time,
            duration_minutes=duration_minutes,
            provider_id=provider_id,
            business_id=business_id,
            business_window_days=business.appointment_window_days
        )

        # Create appointment
        appointment = Appointment(
            business_id=business_id,
            client_id=client.id,
            external_id=external_id,
            appointment_time=appointment_time,
            duration_minutes=duration_minutes,
            appointment_type=appointment_type,
            provider_id=provider_id,
            status=AppointmentStatus.SCHEDULED,
            is_movable=is_movable,
            move_count=0,
            no_show_risk=0.5  # Initial neutral risk
        )

        self.db.add(appointment)
        self.db.flush()  # Get ID without committing

        # Calculate risk score
        self.cascade_service._update_risk_score(appointment)

        # Update client statistics
        self.client_service.update_client_stats_on_appointment_created(client)

        # Log event
        log_event(
            self.db,
            business_id=business_id,
            event_type="appointment.created",
            entity_type="appointment",
            entity_id=appointment.id,
            metadata={
                "client_id": client.id,
                "appointment_time": appointment_time.isoformat(),
                "risk_score": appointment.no_show_risk
            }
        )

        # Commit transaction
        self.db.commit()
        self.db.refresh(appointment)

        logger.info(
            f"Created appointment {appointment.id} for client {client.id} "
            f"at {appointment_time.isoformat()} (risk: {appointment.no_show_risk:.3f})"
        )

        return appointment

    def cancel_appointment(
        self,
        appointment_id: int,
        business_id: int,
        trigger_cascade: bool = True
    ) -> dict:
        """
        Cancel appointment with state validation and optional cascade.

        IMPORTANT: This method now routes through the Orchestrator for cascade execution.
        The orchestrator is the single entry point for all workflow operations.

        Args:
            appointment_id: Appointment ID
            business_id: Business ID for multi-tenant isolation
            trigger_cascade: Whether to trigger cascade optimization

        Returns:
            Dictionary with cancellation results

        Raises:
            ResourceNotFoundError: If appointment not found
            InvalidStateTransitionError: If invalid state transition
        """
        # Import orchestrator components
        from jenga.core.orchestration.orchestrator import Orchestrator
        from jenga.adapters.storage.sqlalchemy_adapter import (
            SQLAlchemyWorkflowRepository,
            SQLAlchemyClientRepository
        )
        from jenga.core.decisions.decision_gateway import DecisionGateway

        # Create orchestrator with current db session
        workflow_repo = SQLAlchemyWorkflowRepository(self.db)
        client_repo = SQLAlchemyClientRepository(self.db)

        # Configure decision gateway with config thresholds
        gateway = DecisionGateway(
            high_risk_threshold=self.config.ml.risk_thresholds.high,
            medium_risk_threshold=self.config.ml.risk_thresholds.medium,
            max_cascade_depth=self.config.engine.cascade.max_depth
        )

        orchestrator = Orchestrator(
            workflow_repository=workflow_repo,
            client_repository=client_repo,
            decision_gateway=gateway
        )

        # Execute cancellation through orchestrator
        result = orchestrator.cancel_workflow(
            workflow_id=appointment_id,
            business_id=business_id,
            trigger_cascade=trigger_cascade
        )

        if not result.success:
            if "not found" in (result.error or ""):
                raise ResourceNotFoundError(result.error)
            raise InvalidStateTransitionError(result.error or "Cancellation failed")

        # Log event (orchestrator also logs, but we keep legacy logging for compatibility)
        log_event(
            self.db,
            business_id=business_id,
            event_type="appointment.cancelled",
            event_data={
                "appointment_id": appointment_id,
                "cascade_triggered": trigger_cascade,
                "cascade_moves": result.metadata.get("moves_count", 0) if result.metadata else 0,
                "cascade_depth": result.metadata.get("cascade_depth", 0) if result.metadata else 0
            },
            appointment_id=appointment_id
        )

        # Commit transaction
        self.db.commit()

        moves_count = result.metadata.get("moves_count", 0) if result.metadata else 0
        cascade_depth = result.metadata.get("cascade_depth", 0) if result.metadata else 0
        moved_workflow_ids = result.metadata.get("moved_workflow_ids", []) if result.metadata else []

        # Sync moved appointments back to Google Calendar
        google_sync_result = None
        if moved_workflow_ids and config.features.enable_google_calendar_sync:
            try:
                from calendar_sync import sync_moved_appointments_to_google
                google_sync_result = sync_moved_appointments_to_google(
                    self.db,
                    moved_workflow_ids
                )
                logger.info(
                    f"Google Calendar write-back: {google_sync_result.get('synced', 0)} synced, "
                    f"{google_sync_result.get('skipped', 0)} skipped"
                )
            except Exception as e:
                logger.error(f"Google Calendar write-back failed: {e}")

        logger.info(
            f"Cancelled appointment {appointment_id} via orchestrator "
            f"(cascade: {trigger_cascade}, moves: {moves_count}, depth: {cascade_depth})"
        )

        return {
            "appointment_id": appointment_id,
            "previous_status": "scheduled",  # We don't have this info from orchestrator
            "new_status": "cancelled",
            "cascade_triggered": trigger_cascade,
            "moves_count": moves_count,
            "cascade_depth": cascade_depth,
            "moved_workflow_ids": moved_workflow_ids,
            "google_sync": google_sync_result
        }

    def complete_appointment(
        self,
        appointment_id: int,
        business_id: int
    ) -> Appointment:
        """
        Mark appointment as completed.

        Args:
            appointment_id: Appointment ID
            business_id: Business ID for multi-tenant isolation

        Returns:
            Updated appointment

        Raises:
            ResourceNotFoundError: If appointment not found
            InvalidStateTransitionError: If invalid state transition
        """
        # Get appointment
        appointment = self.db.query(Appointment).filter(
            Appointment.id == appointment_id,
            Appointment.business_id == business_id
        ).first()

        if not appointment:
            raise ResourceNotFoundError(
                f"Appointment {appointment_id} not found for business {business_id}"
            )

        # Validate state transition
        StateTransitionValidator.validate_transition(
            appointment.status,
            AppointmentStatus.COMPLETED
        )

        # Update status
        appointment.status = AppointmentStatus.COMPLETED

        # Update client statistics
        self.client_service.update_client_stats_on_completion(appointment.client)

        # Log event
        log_event(
            self.db,
            business_id=business_id,
            event_type="appointment.completed",
            entity_type="appointment",
            entity_id=appointment.id,
            metadata={
                "client_id": appointment.client_id,
                "appointment_time": appointment.appointment_time.isoformat()
            }
        )

        # Commit transaction
        self.db.commit()
        self.db.refresh(appointment)

        logger.info(f"Completed appointment {appointment_id}")

        return appointment

    def mark_no_show(
        self,
        appointment_id: int,
        business_id: int
    ) -> Appointment:
        """
        Mark appointment as no-show and update client segment if needed.

        Args:
            appointment_id: Appointment ID
            business_id: Business ID for multi-tenant isolation

        Returns:
            Updated appointment

        Raises:
            ResourceNotFoundError: If appointment not found
            InvalidStateTransitionError: If invalid state transition
        """
        # Get appointment
        appointment = self.db.query(Appointment).filter(
            Appointment.id == appointment_id,
            Appointment.business_id == business_id
        ).first()

        if not appointment:
            raise ResourceNotFoundError(
                f"Appointment {appointment_id} not found for business {business_id}"
            )

        # Validate state transition
        StateTransitionValidator.validate_transition(
            appointment.status,
            AppointmentStatus.NO_SHOW
        )

        # Update status
        appointment.status = AppointmentStatus.NO_SHOW

        # Update client statistics (includes segment update if threshold exceeded)
        self.client_service.update_client_stats_on_no_show(appointment.client)

        # Log event
        log_event(
            self.db,
            business_id=business_id,
            event_type="appointment.no_show",
            entity_type="appointment",
            entity_id=appointment.id,
            metadata={
                "client_id": appointment.client_id,
                "appointment_time": appointment.appointment_time.isoformat(),
                "client_no_show_rate": appointment.client.no_show_rate,
                "client_segment": appointment.client.segment.value
            }
        )

        # Commit transaction
        self.db.commit()
        self.db.refresh(appointment)

        logger.info(
            f"Marked appointment {appointment_id} as no-show "
            f"(client no-show rate: {appointment.client.no_show_rate:.3f})"
        )

        return appointment

    def get_appointment(
        self,
        appointment_id: int,
        business_id: int
    ) -> Appointment:
        """
        Get appointment by ID with business isolation.

        Args:
            appointment_id: Appointment ID
            business_id: Business ID for multi-tenant isolation

        Returns:
            Appointment object

        Raises:
            ResourceNotFoundError: If appointment not found
        """
        appointment = self.db.query(Appointment).filter(
            Appointment.id == appointment_id,
            Appointment.business_id == business_id
        ).first()

        if not appointment:
            raise ResourceNotFoundError(
                f"Appointment {appointment_id} not found for business {business_id}"
            )

        return appointment

    def list_appointments(
        self,
        business_id: int,
        status: Optional[AppointmentStatus] = None,
        limit: int = 100,
        offset: int = 0
    ) -> list[Appointment]:
        """
        List appointments for business with optional filtering.

        Args:
            business_id: Business ID for multi-tenant isolation
            status: Optional status filter
            limit: Maximum appointments to return
            offset: Offset for pagination

        Returns:
            List of appointments
        """
        query = self.db.query(Appointment).filter(
            Appointment.business_id == business_id
        )

        if status:
            query = query.filter(Appointment.status == status)

        appointments = query.order_by(
            Appointment.appointment_time.desc()
        ).limit(limit).offset(offset).all()

        return appointments
