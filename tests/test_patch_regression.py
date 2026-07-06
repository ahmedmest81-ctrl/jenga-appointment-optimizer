"""
Regression tests for the production-safety patch.

One test (or pair) per fixed defect:
1. Cascade auto-move never double-books an occupied slot.
2. accept_offer refuses when the slot was taken during the offer window.
3. accept_offer refuses when the appointment is no longer active.
4. Accepting an offer invalidates sibling offers for the same slot.
5. process_expired_offers marks overdue offers and offers to the NEXT
   candidate (never re-offers the same client).
6. update_time is tenant-scoped at the SQLAlchemy layer.
7. Timezone-aware inputs are normalized to the correct UTC instant.
"""

from dataclasses import replace
from datetime import datetime, timedelta, timezone
from typing import Optional

import pytest

from jenga.core.orchestration.orchestrator import Orchestrator, Offer
from jenga.core.state.workflow_state import WorkflowInstance, WorkflowStatus
from jenga.core.decisions.decision_gateway import DecisionGateway
from jenga.core.time_utils import ensure_naive_utc, utc_now


# ===== In-memory fakes implementing the (new) repository protocols =====

class FakeWorkflowRepo:
    def __init__(self):
        self.workflows = {}
        self._next_id = 1

    def add(self, business_id, client_id, when, risk, duration=60,
            status=WorkflowStatus.SCHEDULED, movable=True):
        wf = WorkflowInstance(
            id=self._next_id, business_id=business_id, client_id=client_id,
            status=status, appointment_time=when, duration_minutes=duration,
            risk_score=risk, is_movable=movable, move_count=0,
        )
        self.workflows[wf.id] = wf
        self._next_id += 1
        return wf

    def get_by_id(self, workflow_id, business_id):
        wf = self.workflows.get(workflow_id)
        if wf is None or wf.business_id != business_id:
            return None
        return wf

    def save(self, workflow):
        if workflow.id == 0:
            workflow = replace(workflow, id=self._next_id)
            self._next_id += 1
        self.workflows[workflow.id] = workflow
        return workflow

    def get_active_workflows(self, business_id, from_time=None, to_time=None):
        return [
            w for w in self.workflows.values()
            if w.business_id == business_id
            and w.status in (WorkflowStatus.SCHEDULED, WorkflowStatus.CONFIRMED)
        ]

    def get_cascade_candidates(self, business_id, after_time, min_risk):
        return [
            w for w in self.workflows.values()
            if w.business_id == business_id
            and w.status in (WorkflowStatus.SCHEDULED, WorkflowStatus.CONFIRMED)
            and w.appointment_time > after_time
            and w.risk_score >= min_risk
        ]

    def get_conflicting_workflows(self, business_id, start_time,
                                  duration_minutes, exclude_workflow_id=None):
        new_end = start_time + timedelta(minutes=duration_minutes)
        out = []
        for w in self.workflows.values():
            if w.business_id != business_id:
                continue
            if w.status not in (WorkflowStatus.SCHEDULED, WorkflowStatus.CONFIRMED):
                continue
            if exclude_workflow_id is not None and w.id == exclude_workflow_id:
                continue
            end = w.appointment_time + timedelta(minutes=w.duration_minutes)
            if w.appointment_time < new_end and end > start_time:
                out.append(w)
        return out

    def update_time(self, workflow_id, new_time, business_id=None):
        w = self.workflows[workflow_id]
        if business_id is not None and w.business_id != business_id:
            raise ValueError("tenant mismatch")
        w = replace(w, appointment_time=new_time, move_count=w.move_count + 1)
        self.workflows[workflow_id] = w
        return w

    def record_cascade(self, **kwargs):
        pass


class FakeClientRepo:
    def get_client_data(self, client_id):
        return {"no_show_rate": 0.5, "cancellation_rate": 0.2,
                "segment": "regular", "total_appointments": 10}

    def update_stats_on_completion(self, client_id): pass
    def update_stats_on_cancellation(self, client_id): pass
    def update_stats_on_no_show(self, client_id): pass


class FakeOfferRepo:
    def __init__(self):
        self.offers = {}

    def create_offer(self, offer):
        self.offers[offer.offer_id] = offer
        return offer

    def get_offer(self, offer_id, business_id):
        o = self.offers.get(offer_id)
        if o is None or o.business_id != business_id:
            return None
        return o

    def get_active_offer_for_workflow(self, workflow_id, business_id):
        for o in self.offers.values():
            if (o.workflow_id == workflow_id and o.business_id == business_id
                    and o.status == "offered"):
                return o
        return None

    def get_active_offers_for_slot(self, slot_time, business_id):
        return [o for o in self.offers.values()
                if o.to_time == slot_time and o.business_id == business_id
                and o.status == "offered"]

    def update_offer_status(self, offer_id, status, responded_at=None):
        o = self.offers[offer_id]
        o = replace(o, status=status)
        self.offers[offer_id] = o
        return o

    def count_offers_for_slot(self, slot_time, business_id, trigger_workflow_id):
        return len([o for o in self.offers.values()
                    if o.business_id == business_id
                    and o.trigger_workflow_id == trigger_workflow_id])

    def get_expired_offers(self, business_id, current_time):
        return [o for o in self.offers.values()
                if o.business_id == business_id and o.status == "offered"
                and o.expires_at < current_time]

    def get_offers_for_trigger(self, trigger_workflow_id, business_id):
        return [o for o in self.offers.values()
                if o.business_id == business_id
                and o.trigger_workflow_id == trigger_workflow_id]


def make_orchestrator(offers=True, consent=None, windows=None):
    wrepo, crepo = FakeWorkflowRepo(), FakeClientRepo()
    orepo = FakeOfferRepo() if offers else None
    orch = Orchestrator(
        workflow_repository=wrepo,
        client_repository=crepo,
        decision_gateway=DecisionGateway(max_cascade_depth=5),
        offer_repository=orepo,
        time_window_config=windows or {},
        consent_config=consent or {},
    )
    return orch, wrepo, orepo


NOW = datetime(2026, 7, 6, 8, 0, 0)  # naive UTC baseline for tests


# ===== 1. Cascade double-booking guard =====

def test_cascade_skips_candidate_when_target_slot_is_occupied():
    orch, wrepo, _ = make_orchestrator(offers=False)

    # Cancelled appointment tomorrow 09:00 (SHORT_TERM -> auto-move).
    cancelled = wrepo.add(1, 100, NOW + timedelta(days=1, hours=1), 0.2)
    # Candidate two days later at 15:00, high risk, movable.
    candidate_time = (NOW + timedelta(days=3)).replace(hour=15, minute=0)
    wrepo.add(1, 200, candidate_time, 0.9)
    # BLOCKER: another appointment already sits at 15:00 on the open day.
    blocker_time = (NOW + timedelta(days=1)).replace(hour=15, minute=0)
    blocker = wrepo.add(1, 300, blocker_time, 0.1, movable=False)

    result = orch.cancel_workflow(cancelled.id, business_id=1)
    assert result.success

    # The candidate must NOT have been moved onto the blocker.
    moved = result.metadata.get("moved_workflow_ids", [])
    for wid in moved:
        w = wrepo.workflows[wid]
        conflicts = wrepo.get_conflicting_workflows(
            1, w.appointment_time, w.duration_minutes, exclude_workflow_id=w.id)
        assert conflicts == [], f"workflow {wid} double-booked onto {conflicts}"
    # Blocker untouched.
    assert wrepo.workflows[blocker.id].appointment_time == blocker_time


def test_cascade_moves_candidate_when_slot_is_free():
    orch, wrepo, _ = make_orchestrator(offers=False)
    cancelled = wrepo.add(1, 100, NOW + timedelta(days=1, hours=1), 0.2)
    candidate_time = (NOW + timedelta(days=3)).replace(hour=15, minute=0)
    candidate = wrepo.add(1, 200, candidate_time, 0.9)

    result = orch.cancel_workflow(cancelled.id, business_id=1)
    assert result.success
    assert candidate.id in result.metadata.get("moved_workflow_ids", [])
    moved = wrepo.workflows[candidate.id]
    assert moved.appointment_time.date() == (NOW + timedelta(days=1)).date()
    assert moved.appointment_time.time() == candidate_time.time()


# ===== 2 + 3 + 4. accept_offer hardening =====

def _offer(business_id, workflow, to_time, expires_in_hours=24,
           trigger_id=None, offer_id="offer-1"):
    return Offer(
        offer_id=offer_id, business_id=business_id, workflow_id=workflow.id,
        client_id=workflow.client_id, from_time=workflow.appointment_time,
        to_time=to_time, expires_at=NOW + timedelta(hours=expires_in_hours),
        time_window="short_term", trigger_workflow_id=trigger_id,
        priority_score=0.9, status="offered",
    )


def test_accept_offer_refuses_when_slot_taken_meanwhile():
    orch, wrepo, orepo = make_orchestrator()
    slot = (NOW + timedelta(days=1)).replace(hour=9, minute=0)
    candidate = wrepo.add(1, 200, NOW + timedelta(days=5), 0.9)
    offer = orepo.create_offer(_offer(1, candidate, slot))

    # Someone books the slot directly during the offer window.
    wrepo.add(1, 300, slot, 0.1)

    result = orch.accept_offer(offer.offer_id, business_id=1, current_time=NOW)
    assert not result.success
    assert "no longer available" in result.error
    assert orepo.offers[offer.offer_id].status == "expired"
    # Candidate not moved.
    assert wrepo.workflows[candidate.id].appointment_time == NOW + timedelta(days=5)


def test_accept_offer_refuses_when_appointment_no_longer_active():
    orch, wrepo, orepo = make_orchestrator()
    slot = (NOW + timedelta(days=1)).replace(hour=9, minute=0)
    candidate = wrepo.add(1, 200, NOW + timedelta(days=5), 0.9,
                          status=WorkflowStatus.CANCELLED)
    offer = orepo.create_offer(_offer(1, candidate, slot))

    result = orch.accept_offer(offer.offer_id, business_id=1, current_time=NOW)
    assert not result.success
    assert "no longer active" in result.error
    assert orepo.offers[offer.offer_id].status == "expired"


def test_accept_offer_invalidates_sibling_offers_for_same_slot():
    orch, wrepo, orepo = make_orchestrator()
    slot = (NOW + timedelta(days=1)).replace(hour=9, minute=0)
    a = wrepo.add(1, 200, NOW + timedelta(days=5), 0.9)
    b = wrepo.add(1, 201, NOW + timedelta(days=6), 0.8)
    offer_a = orepo.create_offer(_offer(1, a, slot, offer_id="offer-a"))
    offer_b = orepo.create_offer(_offer(1, b, slot, offer_id="offer-b"))

    result = orch.accept_offer(offer_a.offer_id, business_id=1, current_time=NOW)
    assert result.success
    assert orepo.offers["offer-a"].status == "accepted"
    assert orepo.offers["offer-b"].status == "expired"

    # And the loser can no longer accept into the now-occupied slot.
    late = orch.accept_offer("offer-b", business_id=1, current_time=NOW)
    assert not late.success


# ===== 5. Expiry processing continues to the NEXT candidate =====

def test_process_expired_offers_marks_and_offers_next_candidate():
    # Force offer flow for short-term windows.
    orch, wrepo, orepo = make_orchestrator(
        windows={"short_term_action": "offer"},
    )
    trigger = wrepo.add(1, 100, NOW + timedelta(days=1, hours=1), 0.2)
    first = wrepo.add(1, 200, NOW + timedelta(days=4), 0.95)
    second = wrepo.add(1, 201, NOW + timedelta(days=5), 0.85)

    # Cancellation creates the first offer.
    result = orch.cancel_workflow(trigger.id, business_id=1)
    assert result.success
    assert result.metadata.get("action") == "offer"
    first_offer_id = result.metadata["pending_offer_id"]
    assert orepo.offers[first_offer_id].workflow_id == first.id

    # Time passes beyond expiry; the job runs.
    later = NOW + timedelta(days=1)
    expiry_result = orch.process_expired_offers(business_id=1, current_time=later)
    assert expiry_result.success
    assert expiry_result.metadata["expired_count"] == 1
    assert orepo.offers[first_offer_id].status == "expired"

    # A NEW offer exists for the SECOND candidate - never the first again.
    active = [o for o in orepo.offers.values() if o.status == "offered"]
    assert len(active) == 1
    assert active[0].workflow_id == second.id


def test_process_expired_offers_noop_when_nothing_expired():
    orch, wrepo, orepo = make_orchestrator()
    result = orch.process_expired_offers(business_id=1, current_time=NOW)
    assert result.success
    assert result.metadata["expired_count"] == 0


# ===== 6. Tenant-scoped update_time at the SQLAlchemy layer =====

def test_sqlalchemy_update_time_is_tenant_scoped():
    from sqlalchemy import create_engine
    from sqlalchemy.orm import sessionmaker
    from models import Base, Appointment, AppointmentStatus, Business, Client
    from jenga.adapters.storage.sqlalchemy_adapter import SQLAlchemyWorkflowRepository

    engine = create_engine("sqlite://")
    Base.metadata.create_all(engine)
    db = sessionmaker(bind=engine)()

    b1 = Business(name="A", api_key="k1")
    b2 = Business(name="B", api_key="k2")
    db.add_all([b1, b2]); db.flush()
    c = Client(business_id=b1.id, external_id="c-1", name="X"); db.add(c); db.flush()
    appt = Appointment(
        business_id=b1.id, client_id=c.id, external_id="a-1",
        appointment_time=NOW + timedelta(days=2),
        duration_minutes=60, status=AppointmentStatus.SCHEDULED,
    )
    db.add(appt); db.flush()

    repo = SQLAlchemyWorkflowRepository(db)
    # Correct tenant: works.
    updated = repo.update_time(appt.id, NOW + timedelta(days=1), business_id=b1.id)
    assert updated.move_count == 1
    # Wrong tenant: MUST fail, not silently mutate.
    with pytest.raises(ValueError):
        repo.update_time(appt.id, NOW + timedelta(days=3), business_id=b2.id)


def test_sqlalchemy_conflict_query_detects_overlap():
    from sqlalchemy import create_engine
    from sqlalchemy.orm import sessionmaker
    from models import Base, Appointment, AppointmentStatus, Business, Client
    from jenga.adapters.storage.sqlalchemy_adapter import SQLAlchemyWorkflowRepository

    engine = create_engine("sqlite://")
    Base.metadata.create_all(engine)
    db = sessionmaker(bind=engine)()
    b = Business(name="A", api_key="k"); db.add(b); db.flush()
    c = Client(business_id=b.id, external_id="c-1", name="X"); db.add(c); db.flush()
    base_time = NOW + timedelta(days=2)
    db.add(Appointment(business_id=b.id, client_id=c.id, external_id="a-1",
                       appointment_time=base_time, duration_minutes=60,
                       status=AppointmentStatus.SCHEDULED))
    db.flush()

    repo = SQLAlchemyWorkflowRepository(db)
    # Overlapping start inside existing appointment.
    assert repo.get_conflicting_workflows(b.id, base_time + timedelta(minutes=30), 60)
    # Back-to-back is NOT a conflict.
    assert not repo.get_conflicting_workflows(b.id, base_time + timedelta(minutes=60), 60)
    # Cancelled appointments never conflict.
    db.query(Appointment).update({"status": AppointmentStatus.CANCELLED})
    db.flush()
    assert not repo.get_conflicting_workflows(b.id, base_time, 60)


# ===== 7. Timezone coercion =====

def test_aware_datetimes_normalize_to_correct_utc_instant():
    # 14:00 Vienna summer time == 12:00 UTC.
    vienna = timezone(timedelta(hours=2))
    aware = datetime(2026, 7, 6, 14, 0, tzinfo=vienna)
    assert ensure_naive_utc(aware) == datetime(2026, 7, 6, 12, 0)
    # Naive passes through unchanged.
    naive = datetime(2026, 7, 6, 14, 0)
    assert ensure_naive_utc(naive) == naive
    # utc_now returns naive.
    assert utc_now().tzinfo is None


def test_create_workflow_accepts_aware_datetime():
    orch, wrepo, _ = make_orchestrator()
    vienna = timezone(timedelta(hours=2))
    aware_time = (NOW + timedelta(days=2)).replace(tzinfo=vienna)
    result = orch.create_workflow(
        business_id=1, client_id=100, appointment_time=aware_time,
        duration_minutes=60, current_time=NOW,
    )
    assert result.success
    stored = result.workflow.appointment_time
    assert stored.tzinfo is None
    assert stored == ensure_naive_utc(aware_time)
