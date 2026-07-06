"""
Application Context

Central wiring of all Jenga components.
This module assembles the orchestration engine with all adapters.

Usage:
    from jenga.infrastructure.app_context import JengaContext

    # Create context from config
    ctx = JengaContext.from_config(config_dict)

    # Use in request scope
    with ctx.request_scope(db_session) as scope:
        result = scope.orchestrator.cancel_workflow(workflow_id)

The context is cloud-neutral - it wires components but doesn't
depend on any specific cloud, scheduler, or database engine.
"""

from typing import Dict, Any, Optional
from dataclasses import dataclass
from contextlib import contextmanager
import logging

# Path setup
import sys
from pathlib import Path
APP_DIR = Path(__file__).parent.parent.parent.resolve()
if str(APP_DIR) not in sys.path:
    sys.path.insert(0, str(APP_DIR))

# Core components
from jenga.core.orchestration.orchestrator import Orchestrator
from jenga.core.state.workflow_state import TemporalValidator
from jenga.core.decisions.decision_gateway import DecisionGateway
from jenga.core.events.domain_events import EventBus

# Advisory layer
from jenga.advisory.ml_advisor import MLRiskAdvisor, NullAdvisor

# Adapters
from jenga.adapters.storage.sqlalchemy_adapter import (
    SQLAlchemyWorkflowRepository,
    SQLAlchemyClientRepository,
    SQLAlchemyEventLogger,
    SQLAlchemyOfferRepository,
)
from jenga.adapters.api.fastapi_adapter import FastAPIOrchestrationAdapter
from jenga.adapters.scheduler.apscheduler_adapter import SchedulerAdapter

logger = logging.getLogger(__name__)


@dataclass
class RequestScope:
    """
    Scoped context for a single request/operation.

    Contains all components wired together for a database session.
    """
    orchestrator: Orchestrator
    workflow_repository: SQLAlchemyWorkflowRepository
    client_repository: SQLAlchemyClientRepository
    event_logger: SQLAlchemyEventLogger
    decision_gateway: DecisionGateway


class JengaContext:
    """
    Application context for Jenga orchestration engine.

    Wires all components together based on configuration.
    Creates scoped contexts for request handling.

    This is the main entry point for using Jenga:

        ctx = JengaContext.from_config(config)

        # For API requests
        with ctx.request_scope(db) as scope:
            scope.orchestrator.cancel_workflow(id)

        # For scheduler
        ctx.scheduler.start()
    """

    def __init__(
        self,
        config: Dict[str, Any],
        enable_ml: bool = True,
        enable_scheduler: bool = True
    ):
        """
        Initialize application context.

        Args:
            config: Application configuration
            enable_ml: Whether to enable ML advisory layer
            enable_scheduler: Whether to enable scheduler
        """
        self._config = config
        self._enable_ml = enable_ml
        self._enable_scheduler = enable_scheduler

        # Extract config sections
        self._ml_config = config.get("ml", {})
        self._engine_config = config.get("engine", {})
        self._features = config.get("features", {})
        self._scheduler_config = config.get("scheduler", {})

        # Create event bus (shared across scopes)
        self._event_bus = EventBus()

        # Create temporal validator (shared, stateless)
        self._temporal_validator = TemporalValidator(
            min_advance_hours=self._engine_config.get("min_advance_hours", 1),
            max_future_days=self._engine_config.get("appointment_window_days", 365),
            min_duration_minutes=self._engine_config.get("min_duration_minutes", 15),
            max_duration_minutes=self._engine_config.get("max_duration_minutes", 480)
        )

        # Create ML advisor (shared, stateless)
        self._ml_advisor = None
        if enable_ml and self._features.get("enable_ml_predictions", True):
            self._ml_advisor = MLRiskAdvisor(self._ml_config)
            logger.info(f"ML advisor enabled (version: {self._ml_config.get('version', 'v2.0')})")
        else:
            self._ml_advisor = NullAdvisor()
            logger.info("ML advisor disabled, using NullAdvisor")

        # Create API adapter (shared)
        self._api_adapter = FastAPIOrchestrationAdapter(config)

        # Create scheduler adapter (optional)
        self._scheduler = None
        if enable_scheduler:
            self._scheduler = SchedulerAdapter(config)
            logger.info("Scheduler adapter created")

        logger.info("Jenga context initialized")

    @classmethod
    def from_config(cls, config: Dict[str, Any]) -> 'JengaContext':
        """
        Create context from configuration dictionary.

        Args:
            config: Application configuration

        Returns:
            Configured JengaContext
        """
        features = config.get("features", {})
        return cls(
            config=config,
            enable_ml=features.get("enable_ml_predictions", True),
            enable_scheduler=True
        )

    @property
    def config(self) -> Dict[str, Any]:
        """Get configuration."""
        return self._config

    @property
    def event_bus(self) -> EventBus:
        """Get shared event bus."""
        return self._event_bus

    @property
    def api_adapter(self) -> FastAPIOrchestrationAdapter:
        """Get API adapter for HTTP handlers."""
        return self._api_adapter

    @property
    def scheduler(self) -> Optional[SchedulerAdapter]:
        """Get scheduler adapter."""
        return self._scheduler

    @contextmanager
    def request_scope(self, db):
        """
        Create a scoped context for a database session.

        Use this for request handling:

            with ctx.request_scope(db) as scope:
                scope.orchestrator.cancel_workflow(id)

        Args:
            db: SQLAlchemy database session

        Yields:
            RequestScope with wired components
        """
        # Create repositories for this session
        workflow_repo = SQLAlchemyWorkflowRepository(db)
        client_repo = SQLAlchemyClientRepository(db)
        event_logger = SQLAlchemyEventLogger(db)
        offer_repo = SQLAlchemyOfferRepository(db)

        # Create decision gateway
        gateway = DecisionGateway(
            risk_advisor=self._ml_advisor,
            high_risk_threshold=self._ml_config.get("risk_thresholds", {}).get("high", 0.7),
            medium_risk_threshold=self._ml_config.get("risk_thresholds", {}).get("medium", 0.4),
            max_cascade_depth=self._engine_config.get("max_cascade_depth", 10)
        )

        # Create orchestrator.
        # NOTE: offer_repository, time_window_config and consent_config were
        # previously NOT wired here, which silently disabled the entire offer
        # flow in the live API (orchestrator fell back to auto-move defaults).
        orchestrator = Orchestrator(
            workflow_repository=workflow_repo,
            client_repository=client_repo,
            decision_gateway=gateway,
            temporal_validator=self._temporal_validator,
            offer_repository=offer_repo,
            time_window_config=self._config.get("time_windows", {}),
            consent_config=self._config.get("consent", {})
        )

        scope = RequestScope(
            orchestrator=orchestrator,
            workflow_repository=workflow_repo,
            client_repository=client_repo,
            event_logger=event_logger,
            decision_gateway=gateway
        )

        yield scope

    def start_scheduler(self) -> None:
        """Start the background scheduler."""
        if self._scheduler:
            self._scheduler.start()
            logger.info("Scheduler started")
        else:
            logger.warning("Scheduler not enabled")

    def stop_scheduler(self) -> None:
        """Stop the background scheduler."""
        if self._scheduler:
            self._scheduler.shutdown()
            logger.info("Scheduler stopped")


# Global context (initialized on first use)
_context: Optional[JengaContext] = None


def get_context() -> Optional[JengaContext]:
    """Get the global application context."""
    return _context


def init_context(config: Dict[str, Any]) -> JengaContext:
    """
    Initialize the global application context.

    Should be called once at application startup.

    Args:
        config: Application configuration

    Returns:
        Initialized JengaContext
    """
    global _context
    _context = JengaContext.from_config(config)
    return _context
