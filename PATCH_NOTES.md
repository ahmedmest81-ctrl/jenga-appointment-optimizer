# Jenga patch - production safety: double-booking guards, offer lifecycle, tenancy, time

Full suite after patch: **19 passed** (8 existing + 11 new regression tests).
End-to-end dentist-clinic simulation: all checks pass. FastAPI app boots.

## 1. Double-booking guards (`orchestrator.py`, `sqlalchemy_adapter.py`)

New repository method `get_conflicting_workflows(business_id, start_time,
duration_minutes, exclude_workflow_id)` (overlap rule: existing.start <
new.end AND existing.end > new.start; end computed in Python for SQLite
portability). The orchestrator's new `_slot_has_conflict` guard now runs on
**every path that changes an appointment's time**:

- **Cascade auto-move**: an occupied target slot skips the candidate and
  continues to the next, instead of writing on top of an existing booking.
- **Offer creation**: a slot taken since the cancellation is never offered.
- **Offer acceptance**: a slot filled during the offer window fails the
  acceptance and invalidates the offer.

## 2. Offer lifecycle completed (`orchestrator.py`, `apscheduler_adapter.py`)

Previously nothing processed expired offers - `offer_timeout_action:
next_candidate` was dead configuration and an ignored offer stranded the
freed slot forever.

- New `Orchestrator.process_expired_offers(business_id)`: marks overdue
  offers expired, emits `OfferExpiredEvent`, and (when configured) re-runs
  the cascade for the trigger slot **excluding every workflow already
  offered that slot**, so the next-best candidate is offered and the same
  client is never re-offered. New repo method `get_offers_for_trigger`.
- New scheduler job `process_offer_expiry` (every 5 min, configurable via
  `scheduler.offer_expiry_check_minutes`, feature flag
  `features.enable_offer_expiry`).

## 3. accept_offer hardening (`orchestrator.py`)

- **State guard**: the appointment must still be SCHEDULED/CONFIRMED; offers
  on cancelled/completed/no-show appointments are invalidated, not executed.
- **Sibling invalidation**: accepting an offer expires all other active
  offers for the same slot (with events), closing the two-clients-accept race.

## 4. Tenant-scoped mutation (`sqlalchemy_adapter.py`, protocol)

`update_time` now REQUIRES `business_id` and filters on it - the repository
can no longer mutate another tenant's appointment regardless of caller bugs.
Protocol, both orchestrator call sites, and all test fakes updated.

## 5. Canonical time handling (new `jenga/core/time_utils.py`)

Convention: **naive UTC everywhere inside the engine and DB**, with
`ensure_naive_utc()` coercion at every boundary. Aware inputs (e.g. Pydantic
"14:00+02:00" from the API) now land at the **correct UTC instant** instead
of raising TypeErrors or being misread as UTC wall-clock. All deprecated
`datetime.utcnow()` calls replaced (kernel + live API layer + event
timestamps). Follow-up noted: `Business.timezone` column already exists but
is unused - local-time presentation/classification is the next step.

## 6. Wiring bugs fixed (`app_context.py`, `apscheduler_adapter.py`)

- `request_scope` and the scheduler's `_create_orchestrator` never passed an
  offer repository, `time_window_config`, or `consent_config` - **the entire
  offer flow was silently disabled in the live API** and ran only in the
  simulation. Now wired.
- `_trigger_daily_optimization` read `result.data`, which does not exist on
  `ExecutionResult` (it's `metadata`) - the nightly job crashed with
  AttributeError on first real run. Fixed.
- `requirements.txt` and `pyproject.toml`: added `pytz` (imported
  unconditionally by `utils.py` but undeclared - fresh installs could not
  boot).

## 7. Misc

- `DecisionGateway.max_cascade_depth` public property (removed private
  `_max_cascade_depth` reach-in from the orchestrator).
- In-memory fakes in `tests/test_orchestrator.py` and the dentist simulation
  updated to the new repository protocols.

## New tests (`tests/test_patch_regression.py`)

11 tests: cascade skips occupied slots / moves into free ones; acceptance
refused on taken slot; acceptance refused on inactive appointment; sibling
offers invalidated on acceptance (and loser cannot accept afterwards); expiry
marks offers and offers the NEXT candidate; expiry no-op when nothing
expired; SQL-level tenant scoping (wrong tenant raises); SQL-level overlap
detection (incl. back-to-back is not a conflict, cancelled never conflicts);
timezone coercion unit tests; aware-datetime workflow creation.

## Recommended follow-ups (not in this patch)

- Hash API keys (currently plaintext in DB, matched by SQL equality).
- `Business.timezone`-aware slot classification and presentation.
- DB-level uniqueness/locking for slot assignment under true concurrency
  (current guards close the logic races; serializable transactions or a
  unique slot index close the last write-race window).
- Delete the deprecated legacy `CascadeService` path once the API fully
  routes through the orchestrator.
