"""
Jenga API - Appointment Optimization Engine

FastAPI application providing RESTful API for:
- Multi-tenant business management
- Client management with behavioral tracking
- Appointment operations with state validation
- Risk-based optimization and cascade scheduling
- Analytics and insights

ARCHITECTURAL TRANSFORMATION:
This file now uses clean service layer architecture:
- BEFORE: Business logic embedded directly in endpoint functions (50+ lines per endpoint)
- AFTER: Thin controller pattern - endpoints delegate to service layer (5-10 lines per endpoint)

Benefits:
- Business logic is testable without HTTP layer
- Reusable across multiple interfaces (API, CLI, webhooks)
- Clear separation of concerns (API layer vs business logic)
- Single responsibility per layer
"""

# Ensure App directory is in Python path (fixes --reload subprocess issue)
import sys
from pathlib import Path
APP_DIR = Path(__file__).parent.resolve()
if str(APP_DIR) not in sys.path:
    sys.path.insert(0, str(APP_DIR))

from fastapi import FastAPI, Depends, HTTPException, Header, status
from fastapi.middleware.cors import CORSMiddleware
from sqlalchemy.orm import Session
from typing import List, Optional
from datetime import datetime
import logging

# Database
from database import SessionLocal, get_db, init_db

# Models
from models import Business, Client, Appointment, AppointmentStatus, ClientSegment, CalendarIntegration

# Configuration (new centralized config)
from config_loader import config

# Services (new service layer)
from services.appointment_service import AppointmentService
from services.client_service import ClientService
from services.cascade_service import CascadeService

# Schemas (extracted Pydantic models with validators)
from schemas import (
    BusinessCreate, BusinessResponse,
    ClientCreate, ClientResponse,
    AppointmentCreate, AppointmentResponse,
    CancellationRequest, CascadeResponse
)

# Exceptions (custom exception hierarchy)
from exceptions import (
    ValidationError,
    InvalidStateTransitionError,
    ResourceNotFoundError,
    OverlapError
)

# Utilities
from utils import generate_api_key

# Scheduler
from scheduler import scheduler

# ML Engine
from ml import MLEngineV2

# Routers (modular API endpoints)
from routers.calendar import router as calendar_router

# Notification scheduler (event-driven)
from jenga.adapters.notifications import init_notification_scheduler

# Configure logging
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s'
)
logger = logging.getLogger(__name__)


# Initialize FastAPI
app = FastAPI(
    title="Jenga - Appointment Optimization Engine",
    description=(
        "Portfolio prototype for no-show risk scoring and cancellation-slot "
        "recovery in appointment-based businesses."
    ),
    version="2.0.0",
    contact={"name": "Ahmed Mest"},
    license_info={"name": "MIT"}
)

# CORS
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# Include routers
app.include_router(calendar_router, prefix=config.api.prefix)


# ===== Authentication =====

async def verify_api_key(
    x_api_key: str = Header(..., alias=config.api.key_header),
    db: Session = Depends(get_db)
) -> Business:
    """
    Verify business API key for multi-tenant isolation.

    Args:
        x_api_key: API key from header
        db: Database session

    Returns:
        Business object if valid

    Raises:
        HTTPException: If API key invalid or business inactive
    """
    business = db.query(Business).filter(
        Business.api_key == x_api_key,
        Business.is_active == True
    ).first()

    if not business:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid or inactive API key"
        )

    return business


# ===== Lifecycle Events =====

@app.on_event("startup")
async def startup_event():
    """Initialize database, scheduler, and notification system."""
    logger.info("Starting Jenga API v2.0 (refactored architecture)")
    init_db()
    scheduler.start()

    # Initialize event-driven notification scheduler
    init_notification_scheduler()

    logger.info(
        f"Jenga API started successfully (config: {config.system.environment}, "
        f"ML v{config.ml.version})"
    )


@app.on_event("shutdown")
async def shutdown_event():
    """Shutdown scheduler gracefully."""
    logger.info("Shutting down Jenga API")
    scheduler.shutdown()
    logger.info("Jenga API shutdown complete")


# ===== Health Check =====

@app.get("/health")
async def health_check():
    """
    Health check endpoint.

    Returns system status and version information.
    """
    return {
        "status": "healthy",
        "service": "jenga",
        "version": "2.0.0",
        "architecture": "service-layer",
        "ml_version": config.ml.version,
        "environment": config.system.environment
    }


# ===== Business Endpoints =====

@app.post(f"{config.api.prefix}/businesses", response_model=BusinessResponse)
async def create_business(
    business_data: BusinessCreate,
    db: Session = Depends(get_db)
):
    """
    Create new business account with API key generation.

    THIN CONTROLLER PATTERN:
    - Minimal logic in endpoint
    - Direct database operations (simple CRUD, no business rules)
    - No service layer needed for simple resource creation
    """
    business = Business(
        name=business_data.name,
        api_key=generate_api_key(),
        appointment_window_days=business_data.appointment_window_days,
        timezone=business_data.timezone
    )

    db.add(business)
    db.commit()
    db.refresh(business)

    logger.info(f"Created business: {business.id}")
    return business


@app.get(f"{config.api.prefix}/business", response_model=BusinessResponse)
async def get_business(
    business: Business = Depends(verify_api_key)
):
    """Get current business information."""
    return business


# ===== Client Endpoints =====

@app.post(f"{config.api.prefix}/clients", response_model=ClientResponse)
async def create_client(
    client_data: ClientCreate,
    business: Business = Depends(verify_api_key),
    db: Session = Depends(get_db)
):
    """
    Create or update client.

    SIMPLE CRUD - No service layer needed.
    Client statistics updated by AppointmentService during appointment operations.
    """
    # Check if client exists
    existing = db.query(Client).filter(
        Client.business_id == business.id,
        Client.external_id == client_data.external_id
    ).first()

    if existing:
        # Update existing
        existing.name = client_data.name
        existing.email = client_data.email
        existing.phone = client_data.phone
        existing.segment = client_data.segment
        existing.is_flexible = client_data.is_flexible
        db.commit()
        db.refresh(existing)
        logger.info(f"Updated client: {existing.id} for business {business.id}")
        return existing

    # Create new
    client = Client(
        business_id=business.id,
        external_id=client_data.external_id,
        name=client_data.name,
        email=client_data.email,
        phone=client_data.phone,
        segment=client_data.segment,
        is_flexible=client_data.is_flexible
    )

    db.add(client)
    db.commit()
    db.refresh(client)

    logger.info(f"Created client: {client.id} for business {business.id}")
    return client


@app.get(f"{config.api.prefix}/clients", response_model=List[ClientResponse])
async def list_clients(
    business: Business = Depends(verify_api_key),
    db: Session = Depends(get_db),
    limit: int = 100,
    offset: int = 0
):
    """List clients for business with pagination."""
    clients = db.query(Client).filter(
        Client.business_id == business.id
    ).limit(limit).offset(offset).all()

    return clients


# ===== Appointment Endpoints (SERVICE LAYER PATTERN) =====

@app.post(f"{config.api.prefix}/appointments", response_model=AppointmentResponse)
async def create_appointment(
    appointment_data: AppointmentCreate,
    business: Business = Depends(verify_api_key),
    db: Session = Depends(get_db)
):
    """
    Create new appointment with validation and risk calculation.

    ARCHITECTURAL TRANSFORMATION:
    BEFORE (50+ lines in endpoint):
    - Parse datetime manually
    - Query client with raw SQLAlchemy
    - Create appointment object
    - Instantiate EngineCore with hardcoded values
    - Calculate risk score
    - Update client stats manually
    - Handle commit/rollback
    - 50+ lines of business logic in API layer

    AFTER (10 lines - thin controller):
    - Delegate to AppointmentService
    - Service handles all business logic
    - Clear error handling with custom exceptions
    - Testable without HTTP layer
    """
    try:
        # Initialize service layer
        appointment_service = AppointmentService(db)

        # Delegate to service (ALL business logic in service layer)
        appointment = appointment_service.create_appointment(
            business_id=business.id,
            client_external_id=appointment_data.client_external_id,
            external_id=appointment_data.external_id,
            appointment_time_str=appointment_data.appointment_time,
            duration_minutes=appointment_data.duration_minutes,
            appointment_type=appointment_data.appointment_type,
            provider_id=appointment_data.provider_id,
            is_movable=appointment_data.is_movable
        )

        logger.info(
            f"Created appointment {appointment.id} via API "
            f"(risk: {appointment.no_show_risk:.3f})"
        )
        return appointment

    except ResourceNotFoundError as e:
        raise HTTPException(status_code=404, detail=str(e))
    except ValidationError as e:
        raise HTTPException(status_code=400, detail=str(e))
    except OverlapError as e:
        raise HTTPException(status_code=409, detail=str(e))
    except Exception as e:
        logger.error(f"Error creating appointment: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail="Internal server error")


@app.get(f"{config.api.prefix}/appointments", response_model=List[AppointmentResponse])
async def list_appointments(
    business: Business = Depends(verify_api_key),
    db: Session = Depends(get_db),
    status_filter: Optional[AppointmentStatus] = None,
    limit: int = 100,
    offset: int = 0
):
    """List appointments for business with optional status filter."""
    appointment_service = AppointmentService(db)
    appointments = appointment_service.list_appointments(
        business_id=business.id,
        status=status_filter,
        limit=limit,
        offset=offset
    )
    return appointments


@app.get(f"{config.api.prefix}/appointments/{{appointment_id}}", response_model=AppointmentResponse)
async def get_appointment(
    appointment_id: int,
    business: Business = Depends(verify_api_key),
    db: Session = Depends(get_db)
):
    """Get specific appointment."""
    try:
        appointment_service = AppointmentService(db)
        appointment = appointment_service.get_appointment(
            appointment_id=appointment_id,
            business_id=business.id
        )
        return appointment

    except ResourceNotFoundError as e:
        raise HTTPException(status_code=404, detail=str(e))


@app.post(f"{config.api.prefix}/appointments/{{appointment_id}}/cancel", response_model=CascadeResponse)
async def cancel_appointment(
    appointment_id: int,
    cancellation: CancellationRequest,
    business: Business = Depends(verify_api_key),
    db: Session = Depends(get_db)
):
    """
    Cancel appointment and trigger cascade optimization.

    ARCHITECTURAL TRANSFORMATION:
    BEFORE (40+ lines in endpoint):
    - Query appointment with raw SQL
    - Manual state validation (only checked SCHEDULED)
    - Instantiate EngineCore with wrong db parameter (SessionLocal class, not instance!)
    - Call handle_cancellation
    - Manually update client stats with DIVISION BY ZERO BUG
    - Manual commit
    - Complex error handling

    AFTER (15 lines - thin controller):
    - Delegate to AppointmentService
    - Service handles state validation (prevents 8+ invalid transitions)
    - Service uses CascadeService for cascade logic
    - Service uses ClientService for safe rate calculations (no division by zero)
    - Clear exception mapping
    """
    try:
        # Initialize service layer
        appointment_service = AppointmentService(db)

        # Delegate to service (handles validation, cascade, stats updates)
        result = appointment_service.cancel_appointment(
            appointment_id=appointment_id,
            business_id=business.id,
            trigger_cascade=config.features.enable_cascade_optimization
        )

        logger.info(
            f"Cancelled appointment {appointment_id} via API "
            f"(moves: {result.get('moves_count', 0)})"
        )

        return CascadeResponse(
            success=True,
            moves_count=result.get("moves_count", 0),
            message=f"Appointment cancelled. {result.get('moves_count', 0)} optimizations made."
        )

    except ResourceNotFoundError as e:
        raise HTTPException(status_code=404, detail=str(e))
    except InvalidStateTransitionError as e:
        raise HTTPException(status_code=400, detail=str(e))
    except Exception as e:
        logger.error(f"Error cancelling appointment: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail="Internal server error")


@app.post(f"{config.api.prefix}/appointments/{{appointment_id}}/complete")
async def complete_appointment(
    appointment_id: int,
    business: Business = Depends(verify_api_key),
    db: Session = Depends(get_db)
):
    """
    Mark appointment as completed.

    ARCHITECTURAL TRANSFORMATION:
    BEFORE (20+ lines):
    - Raw SQL query
    - No state validation (could complete CANCELLED appointments!)
    - Manual stats update
    - Manual commit

    AFTER (10 lines):
    - Service layer handles state validation
    - Prevents invalid transitions (e.g., CANCELLED → COMPLETED)
    - Safe statistics updates
    """
    try:
        appointment_service = AppointmentService(db)
        appointment = appointment_service.complete_appointment(
            appointment_id=appointment_id,
            business_id=business.id
        )

        return {
            "success": True,
            "message": "Appointment completed",
            "appointment_id": appointment.id
        }

    except ResourceNotFoundError as e:
        raise HTTPException(status_code=404, detail=str(e))
    except InvalidStateTransitionError as e:
        raise HTTPException(status_code=400, detail=str(e))


@app.post(f"{config.api.prefix}/appointments/{{appointment_id}}/no-show")
async def mark_no_show(
    appointment_id: int,
    business: Business = Depends(verify_api_key),
    db: Session = Depends(get_db)
):
    """
    Mark appointment as no-show.

    ARCHITECTURAL TRANSFORMATION:
    BEFORE (30+ lines):
    - Raw SQL query
    - No state validation
    - Manual stats update with DIVISION BY ZERO BUG (lines 464-466)
    - Hardcoded segment threshold (0.3)
    - Manual commit

    AFTER (10 lines):
    - Service layer handles state validation
    - Safe rate calculation (no division by zero)
    - Config-driven threshold (not hardcoded)
    - Automatic segment update if threshold exceeded
    """
    try:
        appointment_service = AppointmentService(db)
        appointment = appointment_service.mark_no_show(
            appointment_id=appointment_id,
            business_id=business.id
        )

        return {
            "success": True,
            "message": "Appointment marked as no-show",
            "appointment_id": appointment.id,
            "client_no_show_rate": appointment.client.no_show_rate,
            "client_segment": appointment.client.segment.value
        }

    except ResourceNotFoundError as e:
        raise HTTPException(status_code=404, detail=str(e))
    except InvalidStateTransitionError as e:
        raise HTTPException(status_code=400, detail=str(e))


# ===== Analytics Endpoints =====

@app.get(f"{config.api.prefix}/analytics/risk-distribution")
async def get_risk_distribution(
    business: Business = Depends(verify_api_key),
    db: Session = Depends(get_db)
):
    """
    Get risk score distribution for scheduled appointments.

    Uses config-driven thresholds (not hardcoded).
    """
    appointments = db.query(Appointment).filter(
        Appointment.business_id == business.id,
        Appointment.status.in_([AppointmentStatus.SCHEDULED, AppointmentStatus.CONFIRMED])
    ).all()

    # Use config thresholds (not hardcoded)
    medium_threshold = config.ml.risk_thresholds.medium
    high_threshold = config.ml.risk_thresholds.high

    low_risk = sum(1 for a in appointments if a.no_show_risk < medium_threshold)
    medium_risk = sum(
        1 for a in appointments
        if medium_threshold <= a.no_show_risk < high_threshold
    )
    high_risk = sum(1 for a in appointments if a.no_show_risk >= high_threshold)

    return {
        "total_appointments": len(appointments),
        "low_risk": low_risk,
        "medium_risk": medium_risk,
        "high_risk": high_risk,
        "average_risk": (
            sum(a.no_show_risk for a in appointments) / len(appointments)
            if appointments else 0.0
        ),
        "thresholds": {
            "medium": medium_threshold,
            "high": high_threshold
        }
    }


@app.get(f"{config.api.prefix}/analytics/high-risk-appointments")
async def get_high_risk_appointments(
    business: Business = Depends(verify_api_key),
    db: Session = Depends(get_db),
    days_ahead: Optional[int] = None
):
    """
    Identify high-risk appointments for proactive optimization.

    USES SERVICE LAYER for business logic (not raw queries).
    """
    cascade_service = CascadeService(db)
    risky_appointments = cascade_service.identify_risky_appointments(
        business_id=business.id,
        days_ahead=days_ahead
    )

    return {
        "high_risk_count": len(risky_appointments),
        "appointments": [
            {
                "id": appt.id,
                "client_id": appt.client_id,
                "appointment_time": appt.appointment_time.isoformat(),
                "risk_score": appt.no_show_risk,
                "is_movable": appt.is_movable
            }
            for appt in risky_appointments[:20]  # Top 20
        ]
    }


# ===== Manual Optimization Trigger =====

@app.post(f"{config.api.prefix}/optimize")
async def trigger_optimization(
    business: Business = Depends(verify_api_key),
    db: Session = Depends(get_db)
):
    """
    Manually trigger schedule optimization for business.

    USES SERVICE LAYER (not EngineCore directly).
    """
    try:
        cascade_service = CascadeService(db)
        result = cascade_service.optimize_schedule(business.id)

        return {
            "success": True,
            "message": "Optimization complete",
            "results": result
        }

    except Exception as e:
        logger.error(f"Error during optimization: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail=str(e))


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(
        app,
        host="0.0.0.0",
        port=config.api.port,
        log_level=config.system.log_level.lower()
    )
