"""
Calendar Integration Router

API endpoints for connecting and managing calendar integrations.

INVARIANTS:
- Calendar adapters are data sources, not orchestrators
- Jenga is the system of record
- Calendars inform Jenga, they don't control it
- No business logic in endpoints - delegate to adapters/services

These endpoints handle:
- Connecting calendar providers (Calendly, etc.)
- Fetching calendar data (event types, scheduled events)
- Managing integration status
- Syncing calendar events to Jenga (future)
"""

import logging
from datetime import datetime
from typing import Optional, List
from pydantic import BaseModel, Field

from fastapi import APIRouter, Depends, HTTPException, status, Query
from sqlalchemy.orm import Session

import sys
from pathlib import Path
APP_DIR = Path(__file__).parent.parent.resolve()
if str(APP_DIR) not in sys.path:
    sys.path.insert(0, str(APP_DIR))

from database import get_db
from models import Business, CalendarIntegration as CalendarIntegrationModel

# Calendar adapters
from jenga.adapters.calendar.protocol import (
    CalendarProvider,
    CalendarConnectionStatus,
)
from jenga.adapters.calendar.calendly_adapter import CalendlyAdapter
from jenga.adapters.calendar.storage import CalendarIntegrationRepository

logger = logging.getLogger(__name__)


# ===== Pydantic Schemas =====

class CalendarConnectRequest(BaseModel):
    """Request to connect a calendar provider."""
    provider: str = Field(..., description="Calendar provider (calendly, google, outlook)")
    access_token: str = Field(..., description="Access token or Personal Access Token")


class CalendarConnectResponse(BaseModel):
    """Response after connecting a calendar."""
    success: bool
    provider: str
    status: str
    user_email: Optional[str] = None
    user_name: Optional[str] = None
    scheduling_url: Optional[str] = None
    message: str


class CalendarIntegrationResponse(BaseModel):
    """Calendar integration status."""
    id: int
    provider: str
    status: str
    external_user_email: Optional[str]
    external_user_name: Optional[str]
    scheduling_url: Optional[str]
    sync_enabled: bool
    last_sync_at: Optional[datetime]
    connected_at: Optional[datetime]


class CalendarEventTypeResponse(BaseModel):
    """Calendar event type."""
    external_id: str
    name: str
    duration_minutes: int
    provider: str
    color: Optional[str] = None
    description: Optional[str] = None


class CalendarEventResponse(BaseModel):
    """Calendar event."""
    external_id: str
    provider: str
    start_time: datetime
    end_time: datetime
    duration_minutes: int
    invitee_email: Optional[str] = None
    invitee_name: Optional[str] = None
    event_type_name: Optional[str] = None
    location: Optional[str] = None
    is_cancelled: bool = False


class CalendarDisconnectResponse(BaseModel):
    """Response after disconnecting a calendar."""
    success: bool
    provider: str
    message: str


# ===== Router =====

router = APIRouter(prefix="/calendar", tags=["Calendar Integration"])


# ===== Helper Functions =====

def get_adapter_for_provider(provider: str) -> CalendlyAdapter:
    """
    Get the appropriate adapter for a calendar provider.

    Currently only supports Calendly. Will be extended for Google/Outlook.
    """
    provider_lower = provider.lower()

    if provider_lower == "calendly":
        return CalendlyAdapter()
    else:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"Unsupported calendar provider: {provider}. Supported: calendly"
        )


def get_calendar_provider(provider: str) -> CalendarProvider:
    """Convert string to CalendarProvider enum."""
    provider_lower = provider.lower()

    if provider_lower == "calendly":
        return CalendarProvider.CALENDLY
    elif provider_lower == "google":
        return CalendarProvider.GOOGLE
    elif provider_lower == "outlook":
        return CalendarProvider.OUTLOOK
    else:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"Unsupported calendar provider: {provider}"
        )


async def verify_api_key_dependency(
    x_api_key: str = Query(..., alias="X-API-Key"),
    db: Session = Depends(get_db)
) -> Business:
    """
    Verify business API key.

    Note: This is a simplified version. In production, use Header() instead of Query().
    """
    from fastapi import Header
    # Re-import to use proper header
    pass


# ===== Endpoints =====

@router.post("/connect", response_model=CalendarConnectResponse)
async def connect_calendar(
    request: CalendarConnectRequest,
    business_id: int = Query(..., description="Business ID"),
    db: Session = Depends(get_db)
):
    """
    Connect a calendar provider to a business.

    For Calendly:
    1. User generates PAT at https://calendly.com/integrations/api_webhooks
    2. User provides PAT to this endpoint
    3. Jenga validates token and stores connection

    INVARIANTS:
    - Token is validated before storing
    - One connection per business per provider
    - Jenga remains the system of record
    """
    try:
        # Get provider enum
        provider = get_calendar_provider(request.provider)

        # Get adapter
        adapter = get_adapter_for_provider(request.provider)

        # Validate token
        logger.info(f"Validating {request.provider} token for business {business_id}")
        result = adapter.validate_token(request.access_token)

        if not result.success:
            logger.warning(f"Token validation failed for business {business_id}: {result.error}")
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail=f"Token validation failed: {result.error}"
            )

        # Store integration
        repo = CalendarIntegrationRepository(db)
        integration = repo.create_or_update(
            business_id=business_id,
            provider=provider,
            access_token=request.access_token,
            status=CalendarConnectionStatus.CONNECTED,
            external_user_id=result.user_info.get("uri"),
            external_user_email=result.user_info.get("email"),
            external_user_name=result.user_info.get("name"),
            scheduling_url=result.user_info.get("scheduling_url"),
        )

        db.commit()

        logger.info(
            f"Connected {request.provider} for business {business_id} "
            f"(user: {result.user_info.get('email')})"
        )

        return CalendarConnectResponse(
            success=True,
            provider=request.provider,
            status="connected",
            user_email=result.user_info.get("email"),
            user_name=result.user_info.get("name"),
            scheduling_url=result.user_info.get("scheduling_url"),
            message=f"Successfully connected {request.provider}"
        )

    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Error connecting calendar: {e}", exc_info=True)
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Error connecting calendar: {str(e)}"
        )


@router.get("/integrations", response_model=List[CalendarIntegrationResponse])
async def list_integrations(
    business_id: int = Query(..., description="Business ID"),
    db: Session = Depends(get_db)
):
    """
    List all calendar integrations for a business.

    Returns status of all connected (and disconnected) calendar providers.
    """
    repo = CalendarIntegrationRepository(db)
    integrations = repo.get_all_by_business(business_id)

    return [
        CalendarIntegrationResponse(
            id=i.id,
            provider=i.provider.value,
            status=i.status.value,
            external_user_email=i.external_user_email,
            external_user_name=i.external_user_name,
            scheduling_url=i.scheduling_url,
            sync_enabled=i.sync_enabled,
            last_sync_at=i.last_sync_at,
            connected_at=i.connected_at,
        )
        for i in integrations
    ]


@router.get("/integrations/{provider}", response_model=CalendarIntegrationResponse)
async def get_integration(
    provider: str,
    business_id: int = Query(..., description="Business ID"),
    db: Session = Depends(get_db)
):
    """
    Get calendar integration status for a specific provider.
    """
    provider_enum = get_calendar_provider(provider)
    repo = CalendarIntegrationRepository(db)
    integration = repo.get_by_business_and_provider(business_id, provider_enum)

    if not integration:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"No {provider} integration found for business {business_id}"
        )

    return CalendarIntegrationResponse(
        id=integration.id,
        provider=integration.provider.value,
        status=integration.status.value,
        external_user_email=integration.external_user_email,
        external_user_name=integration.external_user_name,
        scheduling_url=integration.scheduling_url,
        sync_enabled=integration.sync_enabled,
        last_sync_at=integration.last_sync_at,
        connected_at=integration.connected_at,
    )


@router.delete("/integrations/{provider}", response_model=CalendarDisconnectResponse)
async def disconnect_calendar(
    provider: str,
    business_id: int = Query(..., description="Business ID"),
    db: Session = Depends(get_db)
):
    """
    Disconnect a calendar provider.

    This removes the stored credentials but does NOT affect data in Jenga.
    Appointments that were created from calendar events remain in Jenga.
    """
    provider_enum = get_calendar_provider(provider)
    repo = CalendarIntegrationRepository(db)

    success = repo.disconnect(business_id, provider_enum)

    if not success:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"No {provider} integration found for business {business_id}"
        )

    db.commit()

    logger.info(f"Disconnected {provider} for business {business_id}")

    return CalendarDisconnectResponse(
        success=True,
        provider=provider,
        message=f"Successfully disconnected {provider}"
    )


@router.get("/event-types", response_model=List[CalendarEventTypeResponse])
async def get_event_types(
    provider: str = Query(..., description="Calendar provider"),
    business_id: int = Query(..., description="Business ID"),
    db: Session = Depends(get_db)
):
    """
    Fetch event types from the connected calendar.

    For Calendly: Returns the user's configured event types.
    For Google/Outlook: Returns the user's calendars.

    Requires a connected calendar integration.
    """
    provider_enum = get_calendar_provider(provider)
    repo = CalendarIntegrationRepository(db)
    integration = repo.get_by_business_and_provider(business_id, provider_enum)

    if not integration or integration.status.value != "connected":
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"No connected {provider} integration found. Connect first."
        )

    adapter = get_adapter_for_provider(provider)
    result = adapter.get_event_types(integration.access_token)

    if not result.success:
        # Token might be expired
        repo.update_status(business_id, provider_enum, CalendarConnectionStatus.EXPIRED, result.error)
        db.commit()

        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail=f"Failed to fetch event types: {result.error}"
        )

    return [
        CalendarEventTypeResponse(
            external_id=et.external_id,
            name=et.name,
            duration_minutes=et.duration_minutes,
            provider=et.provider.value,
            color=et.color,
            description=et.description,
        )
        for et in result.data
    ]


@router.get("/events", response_model=List[CalendarEventResponse])
async def get_scheduled_events(
    provider: str = Query(..., description="Calendar provider"),
    business_id: int = Query(..., description="Business ID"),
    from_time: Optional[datetime] = Query(None, description="Start of time range"),
    to_time: Optional[datetime] = Query(None, description="End of time range"),
    event_type_id: Optional[str] = Query(None, description="Filter by event type"),
    db: Session = Depends(get_db)
):
    """
    Fetch scheduled events from the connected calendar.

    Returns events from the external calendar. These are NOT yet Jenga workflows.
    Use the sync endpoint to import these as Jenga appointments.

    INVARIANTS:
    - This is read-only - does NOT create Jenga appointments
    - Jenga is the system of record, calendar is just a data source
    """
    provider_enum = get_calendar_provider(provider)
    repo = CalendarIntegrationRepository(db)
    integration = repo.get_by_business_and_provider(business_id, provider_enum)

    if not integration or integration.status.value != "connected":
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"No connected {provider} integration found. Connect first."
        )

    adapter = get_adapter_for_provider(provider)
    result = adapter.get_scheduled_events(
        token=integration.access_token,
        from_time=from_time,
        to_time=to_time,
        event_type_id=event_type_id,
    )

    if not result.success:
        # Token might be expired
        repo.update_status(business_id, provider_enum, CalendarConnectionStatus.EXPIRED, result.error)
        db.commit()

        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail=f"Failed to fetch events: {result.error}"
        )

    # Update last sync time
    repo.update_sync_time(business_id, provider_enum)
    db.commit()

    return [
        CalendarEventResponse(
            external_id=evt.external_id,
            provider=evt.provider.value,
            start_time=evt.start_time,
            end_time=evt.end_time,
            duration_minutes=evt.duration_minutes,
            invitee_email=evt.invitee_email,
            invitee_name=evt.invitee_name,
            event_type_name=evt.event_type_name,
            location=evt.location,
            is_cancelled=evt.is_cancelled,
        )
        for evt in result.data
    ]


@router.post("/validate-token")
async def validate_token(
    provider: str = Query(..., description="Calendar provider"),
    access_token: str = Query(..., description="Token to validate"),
):
    """
    Validate a calendar token without storing it.

    Useful for testing tokens before connecting.
    """
    adapter = get_adapter_for_provider(provider)
    result = adapter.validate_token(access_token)

    if not result.success:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail=f"Token validation failed: {result.error}"
        )

    return {
        "valid": True,
        "provider": provider,
        "user_email": result.user_info.get("email"),
        "user_name": result.user_info.get("name"),
    }
