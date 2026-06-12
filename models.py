from sqlalchemy import (
    Column, Integer, String, Float, Boolean, DateTime, 
    ForeignKey, Text, JSON, Index, Enum as SQLEnum
)
from sqlalchemy.orm import relationship
from sqlalchemy.dialects.postgresql import UUID
from uuid import uuid4
from sqlalchemy.sql import func
from datetime import datetime
from enum import Enum
from database import Base


class AppointmentStatus(str, Enum):
    SCHEDULED = "scheduled"
    CONFIRMED = "confirmed"
    CANCELLED = "cancelled"
    COMPLETED = "completed"
    NO_SHOW = "no_show"


class ClientSegment(str, Enum):
    VIP = "vip"
    REGULAR = "regular"
    NEW = "new"
    HIGH_RISK = "high_risk"


class NotificationChannel(str, Enum):
    SMS = "sms"
    EMAIL = "email"
    WHATSAPP = "whatsapp"


class NotificationType(str, Enum):
    REMINDER_7_DAY = "reminder_7_day"
    REMINDER_48_HOUR = "reminder_48_hour"
    AUTO_CONFIRM = "auto_confirm"
    SHIFT_OFFER = "shift_offer"
    CANCELLATION_CONFIRM = "cancellation_confirm"
    EARLIER_SLOT_AVAILABLE = "earlier_slot_available"
    SLOT_AVAILABLE_WISHLIST = "slot_available_wishlist"


class OfferStatus(str, Enum):
    """
    Status of a shift offer.

    Lifecycle: OFFERED → (ACCEPTED | DECLINED | EXPIRED)
    """
    OFFERED = "offered"      # Offer sent, awaiting response
    ACCEPTED = "accepted"    # Patient accepted the earlier slot
    DECLINED = "declined"    # Patient declined
    EXPIRED = "expired"      # Offer timed out without response


class Business(Base):
    __tablename__ = "businesses"
    
    id = Column(Integer, primary_key=True, index=True)
    name = Column(String(255), nullable=False)
    api_key = Column(String(255), unique=True, nullable=False, index=True)
    appointment_window_days = Column(Integer, default=30)
    timezone = Column(String(50), default="UTC")
    is_active = Column(Boolean, default=True)
    created_at = Column(DateTime(timezone=True), server_default=func.now())
    updated_at = Column(DateTime(timezone=True), onupdate=func.now())
    
    clients = relationship("Client", back_populates="business")
    appointments = relationship("Appointment", back_populates="business")


class Client(Base):
    __tablename__ = "clients"
    
    id = Column(Integer, primary_key=True, index=True)
    business_id = Column(Integer, ForeignKey("businesses.id"), nullable=False)
    external_id = Column(String(255), nullable=False)
    name = Column(String(255), nullable=False)
    email = Column(String(255))
    phone = Column(String(50))
    segment = Column(SQLEnum(ClientSegment), default=ClientSegment.REGULAR)
    
    total_appointments = Column(Integer, default=0)
    completed_appointments = Column(Integer, default=0)
    cancelled_appointments = Column(Integer, default=0)
    no_show_appointments = Column(Integer, default=0)
    
    no_show_rate = Column(Float, default=0.0)
    cancellation_rate = Column(Float, default=0.0)
    
    is_flexible = Column(Boolean, default=True)
    created_at = Column(DateTime(timezone=True), server_default=func.now())
    updated_at = Column(DateTime(timezone=True), onupdate=func.now())
    
    business = relationship("Business", back_populates="clients")
    appointments = relationship("Appointment", back_populates="client")
    
    __table_args__ = (
        Index("ix_clients_business_external", "business_id", "external_id", unique=True),
    )


class Appointment(Base):
    __tablename__ = "appointments"
    
    id = Column(Integer, primary_key=True, index=True)
    business_id = Column(Integer, ForeignKey("businesses.id"), nullable=False)
    client_id = Column(Integer, ForeignKey("clients.id"), nullable=False)
    external_id = Column(String(255), nullable=False)
    
    appointment_time = Column(DateTime(timezone=True), nullable=False, index=True)
    duration_minutes = Column(Integer, default=60)
    appointment_type = Column(String(100))
    provider_id = Column(String(255))
    
    status = Column(SQLEnum(AppointmentStatus), default=AppointmentStatus.SCHEDULED, index=True)
    
    no_show_risk = Column(Float, default=0.5)
    ml_model_version = Column(String(50))
    risk_calculated_at = Column(DateTime(timezone=True))
    
    is_movable = Column(Boolean, default=True)
    move_count = Column(Integer, default=0)
    last_moved_at = Column(DateTime(timezone=True))
    
    reminder_7_day_sent = Column(Boolean, default=False)
    reminder_48_hour_sent = Column(Boolean, default=False)
    auto_confirmed = Column(Boolean, default=False)
    
    created_at = Column(DateTime(timezone=True), server_default=func.now())
    updated_at = Column(DateTime(timezone=True), onupdate=func.now())
    
    business = relationship("Business", back_populates="appointments")
    client = relationship("Client", back_populates="appointments")
    notifications = relationship("NotificationLog", back_populates="appointment")
    
    __table_args__ = (
        Index("ix_appointments_business_time", "business_id", "appointment_time"),
        Index("ix_appointments_business_external", "business_id", "external_id", unique=True),
    )


class NotificationLog(Base):
    __tablename__ = "notification_logs"
    
    id = Column(Integer, primary_key=True, index=True)
    business_id = Column(Integer, ForeignKey("businesses.id"), nullable=False)
    appointment_id = Column(Integer, ForeignKey("appointments.id"), nullable=False)
    
    notification_type = Column(SQLEnum(NotificationType), nullable=False)
    channel = Column(SQLEnum(NotificationChannel), nullable=False)
    
    recipient = Column(String(255), nullable=False)
    message = Column(Text)
    
    sent_at = Column(DateTime(timezone=True), server_default=func.now())
    delivered = Column(Boolean, default=False)
    failed = Column(Boolean, default=False)
    error_message = Column(Text)
    
    external_id = Column(String(255))
    message_metadata = Column(JSON)
    
    appointment = relationship("Appointment", back_populates="notifications")
    
    __table_args__ = (
        Index("ix_notifications_appointment_type", "appointment_id", "notification_type"),
    )


class EventLog(Base):
    __tablename__ = "event_logs"
    
    id = Column(Integer, primary_key=True, index=True)
    business_id = Column(Integer, ForeignKey("businesses.id"), nullable=False)
    appointment_id = Column(Integer, ForeignKey("appointments.id"), nullable=True)
    
    event_type = Column(String(100), nullable=False, index=True)
    event_data = Column(JSON)
    
    created_at = Column(DateTime(timezone=True), server_default=func.now(), index=True)
    
    __table_args__ = (
        Index("ix_events_business_type_time", "business_id", "event_type", "created_at"),
    )


class CascadeHistory(Base):
    __tablename__ = "cascade_history"
    
    id = Column(Integer, primary_key=True, index=True)
    business_id = Column(Integer, ForeignKey("businesses.id"), nullable=False)
    trigger_appointment_id = Column(Integer, ForeignKey("appointments.id"), nullable=False)
    
    moved_appointment_id = Column(Integer, ForeignKey("appointments.id"), nullable=False)
    from_time = Column(DateTime(timezone=True), nullable=False)
    to_time = Column(DateTime(timezone=True), nullable=False)
    
    cascade_depth = Column(Integer, default=0)
    selection_score = Column(Float)
    
    created_at = Column(DateTime(timezone=True), server_default=func.now())
    
    __table_args__ = (
        Index("ix_cascade_business_trigger", "business_id", "trigger_appointment_id"),
    )

    
class ShiftOffer(Base):
    """
    Represents an offer to move a patient to an earlier appointment slot.

    INVARIANTS:
    - One active offer per appointment at a time
    - Offers are facts: once created, they represent a real offer made
    - Status transitions: OFFERED → (ACCEPTED | DECLINED | EXPIRED)
    - Accepted offers trigger cascade move execution
    """
    __tablename__ = "shift_offers"

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid4)
    business_id = Column(Integer, ForeignKey("businesses.id"), nullable=False)
    appointment_id = Column(Integer, ForeignKey("appointments.id"), nullable=False)
    client_id = Column(Integer, ForeignKey("clients.id"), nullable=False)

    # The slot being offered
    from_time = Column(DateTime(timezone=True), nullable=False)  # Current appointment time
    to_time = Column(DateTime(timezone=True), nullable=False)    # Offered earlier slot

    # Status lifecycle
    status = Column(SQLEnum(OfferStatus), default=OfferStatus.OFFERED, nullable=False)

    # Timing
    expires_at = Column(DateTime(timezone=True), nullable=False)
    responded_at = Column(DateTime(timezone=True), nullable=True)  # When accepted/declined

    # Context
    trigger_workflow_id = Column(Integer, nullable=True)  # The cancelled appointment that triggered this
    time_window = Column(String(20), nullable=True)  # "short_term", "medium_term", "long_term"
    priority_score = Column(Float, nullable=True)  # From DecisionGateway

    # Metadata
    created_at = Column(DateTime(timezone=True), server_default=func.now())
    updated_at = Column(DateTime(timezone=True), onupdate=func.now())

    # Relationships
    business = relationship("Business")
    appointment = relationship("Appointment")
    client = relationship("Client")

    __table_args__ = (
        Index("ix_shift_offers_business_status", "business_id", "status"),
        Index("ix_shift_offers_appointment", "appointment_id"),
        Index("ix_shift_offers_expires", "expires_at"),
    )


class CalendarProvider(str, Enum):
    """Supported external calendar providers."""
    CALENDLY = "calendly"
    GOOGLE = "google"
    OUTLOOK = "outlook"


class CalendarConnectionStatus(str, Enum):
    """Status of calendar connection."""
    CONNECTED = "connected"
    DISCONNECTED = "disconnected"
    EXPIRED = "expired"
    INVALID = "invalid"


class CalendarIntegration(Base):
    """
    Stores calendar integration credentials per business.

    INVARIANTS:
    - One integration per business per provider
    - Token is encrypted at rest (encryption handled by application layer)
    - Jenga is the system of record, not the calendar

    This model stores the connection metadata; the calendar adapter
    uses the token to fetch data from external systems.
    """
    __tablename__ = "calendar_integrations"

    id = Column(Integer, primary_key=True, index=True)
    business_id = Column(Integer, ForeignKey("businesses.id"), nullable=False)

    # Provider info
    provider = Column(SQLEnum(CalendarProvider), nullable=False)
    status = Column(SQLEnum(CalendarConnectionStatus), default=CalendarConnectionStatus.DISCONNECTED)

    # Credentials (should be encrypted in production)
    access_token = Column(Text, nullable=True)
    refresh_token = Column(Text, nullable=True)  # For OAuth providers
    token_expires_at = Column(DateTime(timezone=True), nullable=True)

    # Provider-specific user info
    external_user_id = Column(String(255), nullable=True)  # e.g., Calendly user URI
    external_user_email = Column(String(255), nullable=True)
    external_user_name = Column(String(255), nullable=True)
    scheduling_url = Column(String(500), nullable=True)  # e.g., Calendly scheduling URL

    # Sync settings
    sync_enabled = Column(Boolean, default=True)
    last_sync_at = Column(DateTime(timezone=True), nullable=True)
    sync_error = Column(Text, nullable=True)

    # Metadata
    connected_at = Column(DateTime(timezone=True), nullable=True)
    created_at = Column(DateTime(timezone=True), server_default=func.now())
    updated_at = Column(DateTime(timezone=True), onupdate=func.now())

    # Relationship
    business = relationship("Business", backref="calendar_integrations")

    __table_args__ = (
        Index("ix_calendar_business_provider", "business_id", "provider", unique=True),
    )
