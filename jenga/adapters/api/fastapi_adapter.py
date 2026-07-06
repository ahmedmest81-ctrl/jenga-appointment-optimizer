"""
FastAPI Adapter

Bridges FastAPI HTTP layer with the orchestration engine.
This adapter translates HTTP requests into orchestrator operations.

The adapter:
- Accepts API request data
- Converts to orchestrator operations
- Returns results formatted for HTTP responses
- Handles error translation

The API layer handles HTTP concerns; the orchestrator handles business logic.
"""

from typing import Dict, Any, Optional, List
from datetime import datetime
from jenga.core.time_utils import utc_now
from dataclasses import dataclass
from enum import Enum
import logging

# Path setup
import sys
from pathlib import Path
APP_DIR = Path(__file__).parent.parent.parent.parent.resolve()
if str(APP_DIR) not in sys.path:
    sys.path.insert(0, str(APP_DIR))

from sqlalchemy.orm import Session

# Core orchestration
from jenga.core.orchestration.orchestrator import Orchestrator, ExecutionResult
from jenga.core.state.workflow_state import WorkflowStatus
from jenga.core.decisions.decision_gateway import DecisionGateway

# Adapters
from jenga.adapters.storage.sqlalchemy_adapter import (
    SQLAlchemyWorkflowRepository,
    SQLAlchemyClientRepository,
    SQLAlchemyOfferRepository,
)
from jenga.advisory.ml_advisor import MLRiskAdvisor

# Existing models for creation (not yet in orchestrator)
from models import Appointment, Client, Business, AppointmentStatus

logger = logging.getLogger(__name__)


class APIError(Exception):
    """Base API error with status code."""
    def __init__(self, message: str, status_code: int = 400):
        self.message = message
        self.status_code = status_code
        super().__init__(message)


class NotFoundError(APIError):
    """Resource not found."""
    def __init__(self, message: str):
        super().__init__(message, status_code=404)


class ValidationError(APIError):
    """Validation error."""
    def __init__(self, message: str):
        super().__init__(message, status_code=400)


class ConflictError(APIError):
    """Conflict error (e.g., invalid state transition)."""
    def __init__(self, message: str):
        super().__init__(message, status_code=409)


@dataclass
class WorkflowResponse:
    """API response for workflow operations."""
    success: bool
    workflow_id: Optional[int]
    status: Optional[str]
    message: str
    data: Optional[Dict[str, Any]] = None


class FastAPIOrchestrationAdapter:
    """
    Adapter bridging FastAPI requests with the orchestration engine.

    This class:
    - Creates orchestrator instances per-request (with db session)
    - Translates API requests to orchestrator operations
    - Converts orchestrator results to API responses
    - Maps orchestrator errors to HTTP errors
    """

    def __init__(self, config: Dict[str, Any]):
        """
        Initialize adapter with configuration.

        Args:
            config: Application configuration
        """
        self._config = config
        self._ml_config = config.get("ml", {})
        self._engine_config = config.get("engine", {})
        self._features = config.get("features", {})

        # Time window config
        self._time_window_config = self._engine_config.get("time_windows", {})
        self._consent_config = self._engine_config.get("consent", {})

    def _create_orchestrator(self, db: Session) -> Orchestrator:
        """Create orchestrator for a database session."""
        workflow_repo = SQLAlchemyWorkflowRepository(db)
        client_repo = SQLAlchemyClientRepository(db)
        offer_repo = SQLAlchemyOfferRepository(db)

        # Create advisory layer if enabled
        advisor = None
        if self._features.get("enable_ml_predictions", True):
            advisor = MLRiskAdvisor(self._ml_config)

        gateway = DecisionGateway(
            risk_advisor=advisor,
            high_risk_threshold=self._ml_config.get("risk_thresholds", {}).get("high", 0.7),
            medium_risk_threshold=self._ml_config.get("risk_thresholds", {}).get("medium", 0.4),
            max_cascade_depth=self._engine_config.get("cascade", {}).get("max_depth", 10)
        )

        return Orchestrator(
            workflow_repository=workflow_repo,
            client_repository=client_repo,
            decision_gateway=gateway,
            offer_repository=offer_repo,
            time_window_config=self._time_window_config,
            consent_config=self._consent_config
        )

    def cancel_workflow(
        self,
        db: Session,
        workflow_id: int,
        business_id: int,
        trigger_cascade: bool = True
    ) -> WorkflowResponse:
        """
        Cancel a workflow via the orchestrator.

        Args:
            db: Database session
            workflow_id: Workflow to cancel
            business_id: Business ID for authorization
            trigger_cascade: Whether to trigger cascade optimization

        Returns:
            WorkflowResponse with result

        Raises:
            NotFoundError: If workflow not found
            ConflictError: If invalid state transition
        """
        orchestrator = self._create_orchestrator(db)

        # Verify business owns this workflow
        workflow = orchestrator._workflow_repository.get(workflow_id)
        if workflow is None:
            raise NotFoundError(f"Workflow {workflow_id} not found")
        if workflow.business_id != business_id:
            raise NotFoundError(f"Workflow {workflow_id} not found")

        # Execute cancellation
        result = orchestrator.cancel_workflow(
            workflow_id=workflow_id,
            trigger_cascade=trigger_cascade
        )

        if not result.success:
            if "not found" in result.message.lower():
                raise NotFoundError(result.message)
            elif "cannot transition" in result.message.lower():
                raise ConflictError(result.message)
            else:
                raise APIError(result.message)

        db.commit()

        return WorkflowResponse(
            success=True,
            workflow_id=workflow_id,
            status=WorkflowStatus.CANCELLED.value,
            message="Workflow cancelled successfully",
            data={
                "cascade_triggered": trigger_cascade,
                "events": result.events
            }
        )

    def complete_workflow(
        self,
        db: Session,
        workflow_id: int,
        business_id: int
    ) -> WorkflowResponse:
        """
        Mark workflow as completed.

        Args:
            db: Database session
            workflow_id: Workflow to complete
            business_id: Business ID for authorization

        Returns:
            WorkflowResponse with result
        """
        orchestrator = self._create_orchestrator(db)

        # Verify business owns this workflow
        workflow = orchestrator._workflow_repository.get(workflow_id)
        if workflow is None:
            raise NotFoundError(f"Workflow {workflow_id} not found")
        if workflow.business_id != business_id:
            raise NotFoundError(f"Workflow {workflow_id} not found")

        result = orchestrator.complete_workflow(workflow_id=workflow_id)

        if not result.success:
            if "cannot transition" in result.message.lower():
                raise ConflictError(result.message)
            else:
                raise APIError(result.message)

        db.commit()

        return WorkflowResponse(
            success=True,
            workflow_id=workflow_id,
            status=WorkflowStatus.COMPLETED.value,
            message="Workflow completed successfully",
            data={"events": result.events}
        )

    def mark_no_show(
        self,
        db: Session,
        workflow_id: int,
        business_id: int
    ) -> WorkflowResponse:
        """
        Mark workflow as no-show.

        Args:
            db: Database session
            workflow_id: Workflow to mark
            business_id: Business ID for authorization

        Returns:
            WorkflowResponse with result
        """
        orchestrator = self._create_orchestrator(db)

        # Verify business owns this workflow
        workflow = orchestrator._workflow_repository.get(workflow_id)
        if workflow is None:
            raise NotFoundError(f"Workflow {workflow_id} not found")
        if workflow.business_id != business_id:
            raise NotFoundError(f"Workflow {workflow_id} not found")

        result = orchestrator.mark_no_show(workflow_id=workflow_id)

        if not result.success:
            if "cannot transition" in result.message.lower():
                raise ConflictError(result.message)
            else:
                raise APIError(result.message)

        db.commit()

        return WorkflowResponse(
            success=True,
            workflow_id=workflow_id,
            status=WorkflowStatus.NO_SHOW.value,
            message="Workflow marked as no-show",
            data={"events": result.events}
        )

    def confirm_workflow(
        self,
        db: Session,
        workflow_id: int,
        business_id: int,
        auto: bool = False
    ) -> WorkflowResponse:
        """
        Confirm a workflow.

        Args:
            db: Database session
            workflow_id: Workflow to confirm
            business_id: Business ID for authorization
            auto: Whether this is an auto-confirmation

        Returns:
            WorkflowResponse with result
        """
        orchestrator = self._create_orchestrator(db)

        # Verify business owns this workflow
        workflow = orchestrator._workflow_repository.get(workflow_id)
        if workflow is None:
            raise NotFoundError(f"Workflow {workflow_id} not found")
        if workflow.business_id != business_id:
            raise NotFoundError(f"Workflow {workflow_id} not found")

        result = orchestrator.confirm_workflow(
            workflow_id=workflow_id,
            auto=auto
        )

        if not result.success:
            if "cannot transition" in result.message.lower():
                raise ConflictError(result.message)
            else:
                raise APIError(result.message)

        db.commit()

        return WorkflowResponse(
            success=True,
            workflow_id=workflow_id,
            status=WorkflowStatus.CONFIRMED.value,
            message="Workflow confirmed",
            data={"auto": auto, "events": result.events}
        )

    def run_optimization(
        self,
        db: Session,
        business_id: int
    ) -> WorkflowResponse:
        """
        Trigger optimization for a business.

        Args:
            db: Database session
            business_id: Business to optimize

        Returns:
            WorkflowResponse with optimization results
        """
        orchestrator = self._create_orchestrator(db)

        # Recalculate risks first
        risk_result = orchestrator.recalculate_risk_scores(business_id)

        # Run optimization
        opt_result = orchestrator.run_optimization(business_id)

        db.commit()

        return WorkflowResponse(
            success=opt_result.success,
            workflow_id=None,
            status=None,
            message="Optimization complete",
            data={
                "risks_updated": risk_result.data.get("updated_count", 0) if risk_result.success else 0,
                "candidates_evaluated": opt_result.data.get("candidates_count", 0) if opt_result.success else 0,
                "optimization_events": opt_result.events
            }
        )

    def get_risk_assessment(
        self,
        db: Session,
        workflow_id: int,
        business_id: int
    ) -> Dict[str, Any]:
        """
        Get risk assessment for a workflow.

        Args:
            db: Database session
            workflow_id: Workflow to assess
            business_id: Business ID for authorization

        Returns:
            Risk assessment data
        """
        orchestrator = self._create_orchestrator(db)

        workflow = orchestrator._workflow_repository.get(workflow_id)
        if workflow is None:
            raise NotFoundError(f"Workflow {workflow_id} not found")
        if workflow.business_id != business_id:
            raise NotFoundError(f"Workflow {workflow_id} not found")

        # Get client data
        client_data = orchestrator._client_repository.get_client_data(workflow.client_id)

        # Get risk assessment from gateway
        if orchestrator._decision_gateway:
            assessment = orchestrator._decision_gateway.assess_workflow_risk(
                workflow, client_data
            )
            return {
                "workflow_id": workflow_id,
                "risk_score": assessment.risk_score,
                "risk_level": assessment.risk_level.value,
                "confidence": assessment.confidence,
                "factors": assessment.factors,
                "model_version": assessment.model_version,
                "calculated_at": assessment.calculated_at.isoformat()
            }
        else:
            return {
                "workflow_id": workflow_id,
                "risk_score": workflow.risk_score,
                "risk_level": "medium",
                "confidence": 0.0,
                "factors": {},
                "model_version": "none",
                "calculated_at": utc_now().isoformat()
            }

    # ===== Offer Handling Methods =====

    def accept_offer(
        self,
        db: Session,
        offer_id: str,
        business_id: int
    ) -> WorkflowResponse:
        """
        Accept a shift offer - moves appointment to earlier slot.

        Args:
            db: Database session
            offer_id: Offer ID (UUID)
            business_id: Business ID for authorization

        Returns:
            WorkflowResponse with result

        Raises:
            NotFoundError: If offer not found
            ConflictError: If offer expired or already responded
        """
        orchestrator = self._create_orchestrator(db)

        result = orchestrator.accept_offer(
            offer_id=offer_id,
            business_id=business_id
        )

        if not result.success:
            error_msg = result.error or "Offer acceptance failed"
            if "not found" in error_msg.lower():
                raise NotFoundError(error_msg)
            elif "expired" in error_msg.lower() or "not active" in error_msg.lower():
                raise ConflictError(error_msg)
            else:
                raise APIError(error_msg)

        db.commit()

        return WorkflowResponse(
            success=True,
            workflow_id=result.workflow.id if result.workflow else None,
            status=result.workflow.status.value if result.workflow else None,
            message="Offer accepted - appointment moved",
            data={
                "offer_id": offer_id,
                "from_time": result.metadata.get("from_time"),
                "to_time": result.metadata.get("to_time"),
                "events_count": len(result.events)
            }
        )

    def decline_offer(
        self,
        db: Session,
        offer_id: str,
        business_id: int
    ) -> WorkflowResponse:
        """
        Decline a shift offer.

        Args:
            db: Database session
            offer_id: Offer ID (UUID)
            business_id: Business ID for authorization

        Returns:
            WorkflowResponse with result
        """
        orchestrator = self._create_orchestrator(db)

        result = orchestrator.decline_offer(
            offer_id=offer_id,
            business_id=business_id
        )

        if not result.success:
            error_msg = result.error or "Offer decline failed"
            if "not found" in error_msg.lower():
                raise NotFoundError(error_msg)
            elif "not active" in error_msg.lower():
                raise ConflictError(error_msg)
            else:
                raise APIError(error_msg)

        db.commit()

        return WorkflowResponse(
            success=True,
            workflow_id=None,
            status=None,
            message="Offer declined",
            data={
                "offer_id": offer_id,
                "attempts_remaining": result.metadata.get("attempts_remaining", 0),
                "next_action": result.metadata.get("next_action")
            }
        )

    def get_offer(
        self,
        db: Session,
        offer_id: str,
        business_id: int
    ) -> Dict[str, Any]:
        """
        Get offer details.

        Args:
            db: Database session
            offer_id: Offer ID (UUID)
            business_id: Business ID for authorization

        Returns:
            Offer data dictionary

        Raises:
            NotFoundError: If offer not found
        """
        from jenga.adapters.storage.sqlalchemy_adapter import SQLAlchemyOfferRepository

        offer_repo = SQLAlchemyOfferRepository(db)
        offer = offer_repo.get_offer(offer_id, business_id)

        if offer is None:
            raise NotFoundError(f"Offer {offer_id} not found")

        return {
            "offer_id": offer.offer_id,
            "workflow_id": offer.workflow_id,
            "client_id": offer.client_id,
            "business_id": offer.business_id,
            "from_time": offer.from_time.isoformat(),
            "to_time": offer.to_time.isoformat(),
            "expires_at": offer.expires_at.isoformat(),
            "time_window": offer.time_window.value,
            "status": offer.status,
            "priority_score": offer.priority_score,
            "trigger_workflow_id": offer.trigger_workflow_id
        }

    def get_active_offers(
        self,
        db: Session,
        business_id: int,
        workflow_id: Optional[int] = None
    ) -> List[Dict[str, Any]]:
        """
        Get active offers for a business or specific workflow.

        Args:
            db: Database session
            business_id: Business ID
            workflow_id: Optional workflow ID to filter

        Returns:
            List of offer data dictionaries
        """
        from jenga.adapters.storage.sqlalchemy_adapter import SQLAlchemyOfferRepository
        from models import ShiftOffer, OfferStatus

        offer_repo = SQLAlchemyOfferRepository(db)

        if workflow_id:
            offer = offer_repo.get_active_offer_for_workflow(workflow_id, business_id)
            if offer:
                return [{
                    "offer_id": offer.offer_id,
                    "workflow_id": offer.workflow_id,
                    "client_id": offer.client_id,
                    "from_time": offer.from_time.isoformat(),
                    "to_time": offer.to_time.isoformat(),
                    "expires_at": offer.expires_at.isoformat(),
                    "time_window": offer.time_window.value,
                    "status": offer.status,
                    "priority_score": offer.priority_score
                }]
            return []

        # Get all active offers for business
        active_offers = db.query(ShiftOffer).filter(
            ShiftOffer.business_id == business_id,
            ShiftOffer.status == OfferStatus.OFFERED
        ).all()

        return [{
            "offer_id": str(o.id),
            "workflow_id": o.appointment_id,
            "client_id": o.client_id,
            "from_time": o.from_time.isoformat(),
            "to_time": o.to_time.isoformat(),
            "expires_at": o.expires_at.isoformat(),
            "time_window": o.time_window,
            "status": o.status.value if o.status else "offered",
            "priority_score": o.priority_score
        } for o in active_offers]

    def expire_offers(
        self,
        db: Session,
        business_id: int
    ) -> WorkflowResponse:
        """
        Expire all past-due offers for a business.

        Should be called periodically by scheduler.

        Args:
            db: Database session
            business_id: Business ID

        Returns:
            WorkflowResponse with expiration count
        """
        orchestrator = self._create_orchestrator(db)

        result = orchestrator.expire_offers(business_id=business_id)

        db.commit()

        return WorkflowResponse(
            success=True,
            workflow_id=None,
            status=None,
            message=f"Expired {result.metadata.get('expired_count', 0)} offers",
            data={
                "expired_count": result.metadata.get("expired_count", 0),
                "events_count": len(result.events)
            }
        )


def create_api_adapter(config: Dict[str, Any]) -> FastAPIOrchestrationAdapter:
    """
    Factory function to create API adapter.

    Args:
        config: Application configuration

    Returns:
        Configured FastAPIOrchestrationAdapter
    """
    return FastAPIOrchestrationAdapter(config)
