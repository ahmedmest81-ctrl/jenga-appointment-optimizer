"""
Custom Exception Hierarchy for Jenga Appointment System

Provides structured, type-safe exceptions for different error categories.
Enables proper error handling and HTTP status code mapping in API layer.
"""


class JengaError(Exception):
    """Base exception for all Jenga system errors"""
    pass


# ===== Validation Errors =====

class ValidationError(JengaError):
    """Base class for all validation errors (HTTP 400)"""
    pass


class InvalidStateTransitionError(ValidationError):
    """
    Raised when an invalid appointment state transition is attempted.

    Example: Trying to complete a CANCELLED appointment
    """
    pass


class PastAppointmentError(ValidationError):
    """
    Raised when attempting to create an appointment in the past.

    Example: Appointment time < current time + min_advance_hours
    """
    pass


class OutOfWindowError(ValidationError):
    """
    Raised when appointment exceeds business booking window.

    Example: Trying to book 400 days in advance when window is 365 days
    """
    pass


class OverlapError(ValidationError):
    """
    Raised when appointment overlaps with existing appointment.

    Example: Same provider has appointment 2-3pm, trying to book 2:30-3:30pm
    """
    pass


class DurationError(ValidationError):
    """
    Raised when appointment duration is invalid.

    Example: Duration < 15 minutes or > 480 minutes
    """
    pass


class TemporalError(ValidationError):
    """
    Raised for general temporal validation failures.

    Example: Invalid datetime format, timezone issues
    """
    pass


class FormatError(ValidationError):
    """
    Raised when data format is invalid.

    Example: Invalid email format, phone number format
    """
    pass


# ===== Business Logic Errors =====

class BusinessLogicError(JengaError):
    """Base class for business logic errors (HTTP 400 or 422)"""
    pass


class CascadeError(BusinessLogicError):
    """
    Raised when cascade optimization fails.

    Example: Maximum depth exceeded, no candidates found
    """
    pass


class NotificationError(BusinessLogicError):
    """
    Raised when notification fails to send.

    Example: All channels exhausted, invalid recipient
    """
    pass


class RiskCalculationError(BusinessLogicError):
    """
    Raised when risk score calculation fails.

    Example: Invalid appointment data, missing client history
    """
    pass


class SegmentUpdateError(BusinessLogicError):
    """
    Raised when client segment update fails.

    Example: Invalid segment value, business rule violation
    """
    pass


# ===== Data Integrity Errors =====

class DataIntegrityError(JengaError):
    """
    Raised when data integrity constraint is violated (HTTP 409 or 500).

    Example: Duplicate external_id, orphaned records
    """
    pass


class RateCalculationError(DataIntegrityError):
    """
    Raised when rate calculation encounters invalid data.

    Example: Negative appointment counts, rates > 1.0
    """
    pass


# ===== Configuration Errors =====

class ConfigurationError(JengaError):
    """
    Raised when configuration is invalid (startup failure).

    Example: Weights don't sum to 1.0, invalid thresholds
    """
    pass


# ===== Resource Errors =====

class ResourceNotFoundError(JengaError):
    """
    Raised when requested resource doesn't exist (HTTP 404).

    Example: Appointment ID not found, client not found
    """
    pass


class ResourceAlreadyExistsError(JengaError):
    """
    Raised when resource already exists (HTTP 409).

    Example: Duplicate external_id for same business
    """
    pass


# ===== Authorization Errors =====

class AuthorizationError(JengaError):
    """
    Raised when authorization fails (HTTP 401 or 403).

    Example: Invalid API key, wrong business access
    """
    pass


class InvalidAPIKeyError(AuthorizationError):
    """
    Raised when API key is invalid or missing (HTTP 401).
    """
    pass


class WrongBusinessError(AuthorizationError):
    """
    Raised when trying to access resource from different business (HTTP 403).
    """
    pass


# ===== External Service Errors =====

class ExternalServiceError(JengaError):
    """
    Base class for external service failures (HTTP 502 or 503).

    Example: Twilio API down, SendGrid rate limit
    """
    pass


class SMSServiceError(ExternalServiceError):
    """Raised when SMS service (Twilio) fails"""
    pass


class EmailServiceError(ExternalServiceError):
    """Raised when email service (SendGrid) fails"""
    pass


class CalendarServiceError(ExternalServiceError):
    """Raised when calendar service (Google Calendar) fails"""
    pass


# ===== Helper Functions =====

def get_http_status_code(exception: Exception) -> int:
    """
    Map exception to HTTP status code for API responses.

    Args:
        exception: Exception instance

    Returns:
        HTTP status code (400, 401, 403, 404, 409, 422, 500, 502, 503)
    """
    if isinstance(exception, ValidationError):
        return 400  # Bad Request

    if isinstance(exception, OverlapError):
        return 409  # Conflict

    if isinstance(exception, ResourceNotFoundError):
        return 404  # Not Found

    if isinstance(exception, (ResourceAlreadyExistsError, DataIntegrityError)):
        return 409  # Conflict

    if isinstance(exception, InvalidAPIKeyError):
        return 401  # Unauthorized

    if isinstance(exception, WrongBusinessError):
        return 403  # Forbidden

    if isinstance(exception, BusinessLogicError):
        return 422  # Unprocessable Entity

    if isinstance(exception, ExternalServiceError):
        if isinstance(exception, (SMSServiceError, EmailServiceError)):
            return 503  # Service Unavailable
        return 502  # Bad Gateway

    if isinstance(exception, ConfigurationError):
        return 500  # Internal Server Error (shouldn't reach API)

    # Default to 500 for unknown errors
    return 500


def to_error_response(exception: Exception) -> dict:
    """
    Convert exception to API error response format.

    Args:
        exception: Exception instance

    Returns:
        Dictionary with error details
    """
    return {
        "error": {
            "type": exception.__class__.__name__,
            "message": str(exception),
            "status_code": get_http_status_code(exception)
        }
    }
