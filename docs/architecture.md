# Architecture

## Design Goal

Jenga separates scheduling decisions from infrastructure. The core decides
whether a workflow may transition, whether a cancellation should trigger a
cascade, and which candidate has priority. Adapters persist those decisions or
translate them to external systems.

## Main Components

### Orchestrator

`jenga/core/orchestration/orchestrator.py` is the preferred entry point for
workflow mutations. It coordinates state validation, advisory risk assessment,
candidate selection, persistence, and domain events.

### State Machine

`WorkflowInstance` is immutable. Legal transitions are:

```text
scheduled -> confirmed | cancelled
confirmed -> completed | no_show | cancelled
cancelled | completed | no_show -> terminal
```

### Decision Gateway

The gateway accepts advisory scores but does not mutate state. This keeps the
risk implementation replaceable and prevents a model from directly executing
business operations.

### Cascade Algorithm

When a future slot is cancelled:

1. Classify urgency as short, medium, or long term.
2. Query movable candidates scheduled on later days.
3. Rank candidates by risk and flexibility.
4. Apply consent policy and either move or offer the slot.
5. Treat the candidate's previous day as the next open capacity bucket.
6. Stop when no candidate remains or `max_cascade_depth` is reached.

The depth limit and moved-ID set guarantee termination.

### Adapters

Repository protocols isolate SQLAlchemy from the core. Calendar and
notification adapters react to requests or emitted events. This allows the
same orchestration logic to run in API, scheduler, simulation, or test
contexts.

## Current Migration

The repository contains a legacy service path and the newer orchestration
kernel. New development should target `jenga/core/`; `services/` remains to
support the existing API while that migration is completed.

## Production Gaps

- database migrations and transactional outbox
- encrypted tenant credentials and managed secret storage
- idempotency and distributed locking
- authorization beyond tenant API keys
- calibrated predictive model and monitoring
- privacy, retention, audit, and regulatory controls
- durable queues for offers and notifications
