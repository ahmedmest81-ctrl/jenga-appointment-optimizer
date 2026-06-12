from datetime import datetime, timedelta
from typing import Optional
import pytz
from config import settings


def get_current_time(timezone: str = "UTC") -> datetime:
    """Get current time in specified timezone."""
    tz = pytz.timezone(timezone)
    return datetime.now(tz)


def parse_datetime(dt_str: str, timezone: str = "UTC") -> datetime:
    """Parse datetime string to timezone-aware datetime."""
    dt = datetime.fromisoformat(dt_str)
    if dt.tzinfo is None:
        tz = pytz.timezone(timezone)
        dt = tz.localize(dt)
    return dt


def hours_until(target_time: datetime, from_time: Optional[datetime] = None) -> float:
    """Calculate hours until target time."""
    if from_time is None:
        from_time = datetime.now(target_time.tzinfo)
    delta = target_time - from_time
    return delta.total_seconds() / 3600


def days_until(target_time: datetime, from_time: Optional[datetime] = None) -> int:
    """Calculate days until target time."""
    if from_time is None:
        from_time = datetime.now(target_time.tzinfo)
    delta = target_time - from_time
    return delta.days


def is_within_hours(target_time: datetime, hours: int) -> bool:
    """Check if target time is within specified hours from now."""
    return 0 <= hours_until(target_time) <= hours


def format_appointment_time(dt: datetime, timezone: str = "UTC") -> str:
    """Format appointment time for display."""
    tz = pytz.timezone(timezone)
    local_time = dt.astimezone(tz)
    return local_time.strftime("%Y-%m-%d %H:%M %Z")


def get_time_window(
    window_days: Optional[int] = None,
    timezone: str = "UTC"
) -> tuple[datetime, datetime]:
    """Get start and end of optimization window."""
    if window_days is None:
        window_days = settings.APPOINTMENT_WINDOW_DAYS
    
    tz = pytz.timezone(timezone)
    now = datetime.now(tz)
    start = now
    end = now + timedelta(days=window_days)
    
    return start, end


def calculate_client_metrics(client) -> dict:
    """Calculate client behavioral metrics."""
    total = client.total_appointments
    
    if total == 0:
        return {
            "no_show_rate": 0.0,
            "cancellation_rate": 0.0,
            "completion_rate": 0.0
        }
    
    return {
        "no_show_rate": client.no_show_appointments / total,
        "cancellation_rate": client.cancelled_appointments / total,
        "completion_rate": client.completed_appointments / total
    }


def safe_rate_calculation(numerator: int, denominator: int) -> float:
    """
    Calculate rate with zero-division protection.

    Handles division by zero gracefully and caps rates at 1.0.
    Used for client statistics (no-show rate, cancellation rate, etc.)

    Args:
        numerator: Number of occurrences (e.g., no-shows, cancellations)
        denominator: Total count (e.g., total appointments)

    Returns:
        Rate as float between 0.0 and 1.0

    Examples:
        >>> safe_rate_calculation(5, 10)
        0.5
        >>> safe_rate_calculation(3, 0)  # Division by zero
        0.0
        >>> safe_rate_calculation(15, 10)  # Impossible rate (data error)
        1.0
    """
    if denominator == 0:
        return 0.0

    # Calculate rate and cap at 1.0 (handles data inconsistencies)
    rate = numerator / denominator
    return min(rate, 1.0)


def generate_api_key() -> str:
    """Generate secure API key for business."""
    import secrets
    return f"jenga_{''.join(secrets.choice('abcdefghijklmnopqrstuvwxyz0123456789') for _ in range(32))}"