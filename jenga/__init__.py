"""
Jenga - Cloud-Neutral Orchestration Engine

A deterministic orchestration kernel for appointment management
with pluggable adapters for scheduling, storage, and integrations.

Core Principles:
- Cloud-neutral by construction
- Deterministic execution
- ML/heuristics are advisory only
- Single ownership of meaning

Architecture:
    jenga/
    ├── core/                   # Cloud-neutral kernel (no external deps)
    │   ├── orchestration/      # Central orchestrator
    │   ├── state/              # Workflow state machine
    │   ├── events/             # Domain event language
    │   └── decisions/          # Decision gateway
    ├── advisory/               # ML layer (optional, fenced)
    ├── adapters/               # External integrations
    │   ├── storage/            # Database adapters
    │   ├── scheduler/          # Time-based triggers
    │   └── api/                # HTTP adapters
    └── infrastructure/         # Application wiring

Usage:
    from jenga.infrastructure import JengaContext, init_context

    # Initialize at startup
    ctx = init_context(config_dict)

    # In request handlers
    with ctx.request_scope(db) as scope:
        result = scope.orchestrator.cancel_workflow(id)

    # Start background scheduler
    ctx.start_scheduler()
"""

__version__ = "2.0.0"

# Core exports
from jenga.core.orchestration.orchestrator import Orchestrator, ExecutionResult
from jenga.core.state.workflow_state import (
    WorkflowInstance,
    WorkflowStatus,
    StateTransitionValidator,
    TemporalValidator,
)
from jenga.core.decisions.decision_gateway import (
    DecisionGateway,
    RiskAssessment,
    RiskLevel,
    CascadeCandidate,
    CascadeDecision,
)
from jenga.core.events.domain_events import (
    EventType,
    DomainEvent,
    EventBus,
)

# Infrastructure exports
from jenga.infrastructure.app_context import (
    JengaContext,
    RequestScope,
    get_context,
    init_context,
)

__all__ = [
    # Version
    "__version__",
    # Core
    "Orchestrator",
    "ExecutionResult",
    "WorkflowInstance",
    "WorkflowStatus",
    "StateTransitionValidator",
    "TemporalValidator",
    "DecisionGateway",
    "RiskAssessment",
    "RiskLevel",
    "CascadeCandidate",
    "CascadeDecision",
    "EventType",
    "DomainEvent",
    "EventBus",
    # Infrastructure
    "JengaContext",
    "RequestScope",
    "get_context",
    "init_context",
]
