"""
Canonical time handling for Jenga.

CONVENTION: All datetimes inside the engine and the database are NAIVE UTC.

Why naive UTC rather than aware everywhere: the existing storage layer
(SQLite in dev) strips tzinfo, and mixing aware and naive datetimes raises
TypeError on comparison. Normalizing every boundary input to naive UTC keeps
all internal comparisons consistent, removes the deprecated
``datetime.utcnow()`` calls, and - crucially - makes aware inputs (e.g. a
Pydantic-parsed "2026-07-05T14:00:00+02:00" from the API) land at the CORRECT
UTC instant instead of being compared as if they were UTC wall-clock times.

Follow-up (schema change, out of scope here): a ``Business.timezone`` column
so slots can be presented and classified in local business time.
"""

from datetime import datetime, timezone


def utc_now() -> datetime:
    """Current UTC time as a naive datetime (canonical engine representation)."""
    return datetime.now(timezone.utc).replace(tzinfo=None)


def ensure_naive_utc(dt: datetime) -> datetime:
    """Coerce any datetime to the canonical naive-UTC representation.

    - Aware datetimes are converted to UTC, then stripped of tzinfo.
    - Naive datetimes are assumed to already be UTC and returned unchanged.
    """
    if dt.tzinfo is not None:
        return dt.astimezone(timezone.utc).replace(tzinfo=None)
    return dt
