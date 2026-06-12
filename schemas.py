"""
Pydantic Schemas for API Request/Response Validation

Enhanced with validators for:
- Temporal constraints
- Format validation
- Business rules
- Data integrity
"""

from pydantic import BaseModel, Field, field_validator, model_validator
from typing import Optional
from datetime import datetime
from models import ClientSegment, AppointmentStatus
from config_loader import config
import re


# ===== Business Schemas =====

class BusinessCreate(BaseModel):
    """Schema for creating a business"""
    name: str = Field(..., min_length=2, max_length=255)
    appointment_window_days: int = Field(default=30, ge=7, le=180)
    timezone: str = Field(default="UTC")

    @field_validator('name')
    @classmethod
    def validate_name_not_empty(cls, v):
        """Ensure name is not just whitespace"""
        if not v or not v.strip():
            raise ValueError("Business name cannot be empty")
        return v.strip()


class BusinessResponse(BaseModel):
    """Schema for business response"""
    id: int
    name: str
    api_key: str
    appointment_window_days: int
    timezone: str
    is_active: bool

    class Config:
        from_attributes = True


# ===== Client Schemas =====

class ClientCreate(BaseModel):
    """Schema for creating a client"""
    external_id: str = Field(..., min_length=1, max_length=255)
    name: str = Field(..., min_length=2, max_length=255)
    email: Optional[str] = Field(None, max_length=255)
    phone: Optional[str] = Field(None, max_length=50)
    segment: ClientSegment = ClientSegment.REGULAR
    is_flexible: bool = True

    @field_validator('name')
    @classmethod
    def validate_name_not_empty(cls, v):
        """Ensure name is not just whitespace"""
        if not v or not v.strip():
            raise ValueError("Client name cannot be empty")
        return v.strip()

    @field_validator('email')
    @classmethod
    def validate_email_format(cls, v):
        """Validate email format"""
        if v is None or v == "":
            return v

        # Basic email regex
        email_regex = r'^[a-zA-Z0-9._%+-]+@[a-zA-Z0-9.-]+\.[a-zA-Z]{2,}$'
        if not re.match(email_regex, v):
            raise ValueError(f"Invalid email format: {v}")
        return v.lower()

    @field_validator('phone')
    @classmethod
    def validate_phone_format(cls, v):
        """Validate phone format (E.164)"""
        if v is None or v == "":
            return v

        # Remove common formatting characters
        phone_cleaned = re.sub(r'[\s\-\(\)\.]', '', v)

        # Basic E.164 format: +[country][number], 7-15 digits
        phone_regex = r'^\+?[1-9]\d{6,14}$'
        if not re.match(phone_regex, phone_cleaned):
            raise ValueError(
                f"Invalid phone format: {v}. "
                "Expected E.164 format (e.g., +12345678900)"
            )
        return phone_cleaned

    @model_validator(mode='after')
    def validate_contact_info(self):
        """Ensure at least email or phone is provided"""
        if config.validation.client.require_email_or_phone:
            if not self.email and not self.phone:
                raise ValueError("Must provide at least email or phone")
        return self


class ClientResponse(BaseModel):
    """Schema for client response"""
    id: int
    external_id: str
    name: str
    email: Optional[str]
    phone: Optional[str]
    segment: ClientSegment
    no_show_rate: float
    cancellation_rate: float
    is_flexible: bool
    total_appointments: int

    class Config:
        from_attributes = True


# ===== Appointment Schemas =====

class AppointmentCreate(BaseModel):
    """Schema for creating an appointment with comprehensive validation"""
    client_external_id: str = Field(..., min_length=1, max_length=255)
    external_id: str = Field(..., min_length=1, max_length=255)
    appointment_time: str  # ISO8601 datetime string
    duration_minutes: int = Field(default=60)
    appointment_type: Optional[str] = Field(default="routine", max_length=100)
    provider_id: Optional[str] = Field(None, max_length=255)
    is_movable: bool = True

    @field_validator('appointment_time')
    @classmethod
    def validate_appointment_time_format(cls, v):
        """Validate ISO8601 datetime format"""
        try:
            # Try to parse as ISO8601
            datetime.fromisoformat(v.replace('Z', '+00:00'))
        except ValueError:
            raise ValueError(
                f"Invalid datetime format: {v}. "
                "Expected ISO8601 format (e.g., 2024-01-15T14:30:00)"
            )
        return v

    @field_validator('duration_minutes')
    @classmethod
    def validate_duration_bounds(cls, v):
        """Validate duration is within configured bounds"""
        cfg = config.validation.appointment
        if not (cfg.min_duration_minutes <= v <= cfg.max_duration_minutes):
            raise ValueError(
                f"Duration {v} minutes is invalid. "
                f"Must be between {cfg.min_duration_minutes} and {cfg.max_duration_minutes} minutes"
            )
        return v

    @field_validator('appointment_type')
    @classmethod
    def validate_appointment_type(cls, v):
        """Validate appointment type is recognized (optional validation)"""
        if v and v.lower() not in config.ml.appointment_type_risk:
            # Warning only - allow unknown types but use default risk
            pass
        return v.lower() if v else "routine"


class AppointmentResponse(BaseModel):
    """Schema for appointment response"""
    id: int
    external_id: str
    client_id: int
    appointment_time: datetime
    duration_minutes: int
    appointment_type: Optional[str]
    status: AppointmentStatus
    no_show_risk: float
    is_movable: bool
    move_count: int
    provider_id: Optional[str]
    ml_model_version: Optional[str]
    risk_calculated_at: Optional[datetime]

    class Config:
        from_attributes = True


class AppointmentListResponse(BaseModel):
    """Schema for paginated appointment list"""
    total: int
    appointments: list[AppointmentResponse]
    limit: int
    offset: int


# ===== Action Schemas =====

class CancellationRequest(BaseModel):
    """Schema for cancellation request"""
    reason: Optional[str] = Field(None, max_length=500)
    trigger_cascade: bool = Field(default=True)


class CascadeResponse(BaseModel):
    """Schema for cascade operation response"""
    success: bool
    appointment_id: int
    previous_status: str
    new_status: str
    cascade_triggered: bool
    moves_count: int
    message: str


class CompletionResponse(BaseModel):
    """Schema for appointment completion response"""
    success: bool
    appointment_id: int
    message: str


class NoShowResponse(BaseModel):
    """Schema for no-show response"""
    success: bool
    appointment_id: int
    client_no_show_rate: float
    client_segment: str
    message: str


# ===== Analytics Schemas =====

class RiskDistributionResponse(BaseModel):
    """Schema for risk distribution analytics"""
    business_id: int
    total_appointments: int
    high_risk_count: int
    medium_risk_count: int
    low_risk_count: int
    high_risk_percentage: float
    medium_risk_percentage: float
    low_risk_percentage: float


class OptimizationResultResponse(BaseModel):
    """Schema for optimization job results"""
    business_id: int
    timestamp: str
    risk_scores_updated: int
    total_appointments: int
    high_risk_count: int
    medium_risk_count: int
    low_risk_count: int
    high_risk_appointments: list[dict]


# ===== Error Schemas =====

class ErrorDetail(BaseModel):
    """Schema for error details"""
    type: str
    message: str
    status_code: int


class ErrorResponse(BaseModel):
    """Schema for error responses"""
    error: ErrorDetail


# ===== Health Check Schema =====

class HealthResponse(BaseModel):
    """Schema for health check response"""
    status: str
    version: str
    environment: str
    timestamp: str
    database_connected: bool
    scheduler_enabled: bool
    features: dict


# ===== Manual Trigger Schema =====

class ManualTriggerRequest(BaseModel):
    """Schema for manual Jenga optimization trigger"""
    business_id: int
    force: bool = Field(default=False)
    days_ahead: Optional[int] = Field(None, ge=1, le=180)


class ManualTriggerResponse(BaseModel):
    """Schema for manual trigger response"""
    success: bool
    message: str
    optimization_results: Optional[OptimizationResultResponse] = None
