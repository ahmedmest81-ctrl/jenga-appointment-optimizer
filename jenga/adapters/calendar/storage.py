"""
Calendar Integration Storage

Repository for calendar integration credentials.

INVARIANTS:
- One integration per business per provider
- Storage handles persistence, not business logic
- Tokens should be encrypted in production (application layer responsibility)
"""

from datetime import datetime
from typing import Optional, List
from sqlalchemy.orm import Session

import sys
from pathlib import Path
APP_DIR = Path(__file__).parent.parent.parent.parent.resolve()
if str(APP_DIR) not in sys.path:
    sys.path.insert(0, str(APP_DIR))

from models import (
    CalendarIntegration,
    CalendarProvider as DBCalendarProvider,
    CalendarConnectionStatus as DBCalendarConnectionStatus,
)
from jenga.adapters.calendar.protocol import (
    CalendarProvider,
    CalendarConnectionStatus,
)


def _to_db_provider(provider: CalendarProvider) -> DBCalendarProvider:
    """Convert adapter provider enum to database enum."""
    mapping = {
        CalendarProvider.CALENDLY: DBCalendarProvider.CALENDLY,
        CalendarProvider.GOOGLE: DBCalendarProvider.GOOGLE,
        CalendarProvider.OUTLOOK: DBCalendarProvider.OUTLOOK,
    }
    return mapping.get(provider, DBCalendarProvider.CALENDLY)


def _from_db_provider(provider: DBCalendarProvider) -> CalendarProvider:
    """Convert database provider enum to adapter enum."""
    mapping = {
        DBCalendarProvider.CALENDLY: CalendarProvider.CALENDLY,
        DBCalendarProvider.GOOGLE: CalendarProvider.GOOGLE,
        DBCalendarProvider.OUTLOOK: CalendarProvider.OUTLOOK,
    }
    return mapping.get(provider, CalendarProvider.CALENDLY)


def _to_db_status(status: CalendarConnectionStatus) -> DBCalendarConnectionStatus:
    """Convert adapter status enum to database enum."""
    mapping = {
        CalendarConnectionStatus.CONNECTED: DBCalendarConnectionStatus.CONNECTED,
        CalendarConnectionStatus.DISCONNECTED: DBCalendarConnectionStatus.DISCONNECTED,
        CalendarConnectionStatus.EXPIRED: DBCalendarConnectionStatus.EXPIRED,
        CalendarConnectionStatus.INVALID: DBCalendarConnectionStatus.INVALID,
    }
    return mapping.get(status, DBCalendarConnectionStatus.DISCONNECTED)


def _from_db_status(status: DBCalendarConnectionStatus) -> CalendarConnectionStatus:
    """Convert database status enum to adapter enum."""
    mapping = {
        DBCalendarConnectionStatus.CONNECTED: CalendarConnectionStatus.CONNECTED,
        DBCalendarConnectionStatus.DISCONNECTED: CalendarConnectionStatus.DISCONNECTED,
        DBCalendarConnectionStatus.EXPIRED: CalendarConnectionStatus.EXPIRED,
        DBCalendarConnectionStatus.INVALID: CalendarConnectionStatus.INVALID,
    }
    return mapping.get(status, CalendarConnectionStatus.DISCONNECTED)


class CalendarIntegrationRepository:
    """
    Repository for calendar integration storage.

    Handles CRUD operations for calendar integrations.
    Does NOT handle token validation or calendar API calls.
    """

    def __init__(self, db: Session):
        self._db = db

    def get_by_business_and_provider(
        self,
        business_id: int,
        provider: CalendarProvider,
    ) -> Optional[CalendarIntegration]:
        """
        Get calendar integration for a business and provider.

        Args:
            business_id: Business ID
            provider: Calendar provider

        Returns:
            CalendarIntegration or None
        """
        return self._db.query(CalendarIntegration).filter(
            CalendarIntegration.business_id == business_id,
            CalendarIntegration.provider == _to_db_provider(provider),
        ).first()

    def get_all_by_business(self, business_id: int) -> List[CalendarIntegration]:
        """
        Get all calendar integrations for a business.

        Args:
            business_id: Business ID

        Returns:
            List of CalendarIntegration
        """
        return self._db.query(CalendarIntegration).filter(
            CalendarIntegration.business_id == business_id,
        ).all()

    def get_connected_by_business(self, business_id: int) -> List[CalendarIntegration]:
        """
        Get all connected calendar integrations for a business.

        Args:
            business_id: Business ID

        Returns:
            List of connected CalendarIntegration
        """
        return self._db.query(CalendarIntegration).filter(
            CalendarIntegration.business_id == business_id,
            CalendarIntegration.status == DBCalendarConnectionStatus.CONNECTED,
        ).all()

    def create_or_update(
        self,
        business_id: int,
        provider: CalendarProvider,
        access_token: str,
        status: CalendarConnectionStatus = CalendarConnectionStatus.CONNECTED,
        external_user_id: Optional[str] = None,
        external_user_email: Optional[str] = None,
        external_user_name: Optional[str] = None,
        scheduling_url: Optional[str] = None,
        refresh_token: Optional[str] = None,
        token_expires_at: Optional[datetime] = None,
    ) -> CalendarIntegration:
        """
        Create or update a calendar integration.

        If integration exists for business+provider, updates it.
        Otherwise creates a new one.

        Args:
            business_id: Business ID
            provider: Calendar provider
            access_token: Access token (PAT or OAuth token)
            status: Connection status
            external_user_id: Provider's user ID
            external_user_email: Provider's user email
            external_user_name: Provider's user name
            scheduling_url: Scheduling URL (for Calendly)
            refresh_token: OAuth refresh token (if applicable)
            token_expires_at: Token expiration time (if applicable)

        Returns:
            Created or updated CalendarIntegration
        """
        integration = self.get_by_business_and_provider(business_id, provider)

        if integration is None:
            integration = CalendarIntegration(
                business_id=business_id,
                provider=_to_db_provider(provider),
            )
            self._db.add(integration)

        # Update fields
        integration.access_token = access_token
        integration.status = _to_db_status(status)
        integration.external_user_id = external_user_id
        integration.external_user_email = external_user_email
        integration.external_user_name = external_user_name
        integration.scheduling_url = scheduling_url
        integration.refresh_token = refresh_token
        integration.token_expires_at = token_expires_at
        integration.connected_at = datetime.utcnow() if status == CalendarConnectionStatus.CONNECTED else None
        integration.sync_error = None

        self._db.flush()
        return integration

    def update_status(
        self,
        business_id: int,
        provider: CalendarProvider,
        status: CalendarConnectionStatus,
        error: Optional[str] = None,
    ) -> Optional[CalendarIntegration]:
        """
        Update integration status.

        Args:
            business_id: Business ID
            provider: Calendar provider
            status: New status
            error: Optional error message

        Returns:
            Updated integration or None if not found
        """
        integration = self.get_by_business_and_provider(business_id, provider)

        if integration is None:
            return None

        integration.status = _to_db_status(status)
        if error:
            integration.sync_error = error

        self._db.flush()
        return integration

    def update_sync_time(
        self,
        business_id: int,
        provider: CalendarProvider,
    ) -> Optional[CalendarIntegration]:
        """
        Update last sync time.

        Args:
            business_id: Business ID
            provider: Calendar provider

        Returns:
            Updated integration or None if not found
        """
        integration = self.get_by_business_and_provider(business_id, provider)

        if integration is None:
            return None

        integration.last_sync_at = datetime.utcnow()
        integration.sync_error = None

        self._db.flush()
        return integration

    def disconnect(
        self,
        business_id: int,
        provider: CalendarProvider,
    ) -> bool:
        """
        Disconnect a calendar integration.

        Clears token and sets status to disconnected.

        Args:
            business_id: Business ID
            provider: Calendar provider

        Returns:
            True if disconnected, False if not found
        """
        integration = self.get_by_business_and_provider(business_id, provider)

        if integration is None:
            return False

        integration.access_token = None
        integration.refresh_token = None
        integration.status = DBCalendarConnectionStatus.DISCONNECTED
        integration.connected_at = None

        self._db.flush()
        return True

    def delete(
        self,
        business_id: int,
        provider: CalendarProvider,
    ) -> bool:
        """
        Delete a calendar integration entirely.

        Args:
            business_id: Business ID
            provider: Calendar provider

        Returns:
            True if deleted, False if not found
        """
        integration = self.get_by_business_and_provider(business_id, provider)

        if integration is None:
            return False

        self._db.delete(integration)
        self._db.flush()
        return True


def create_calendar_repository(db: Session) -> CalendarIntegrationRepository:
    """
    Factory function to create calendar integration repository.

    Args:
        db: Database session

    Returns:
        CalendarIntegrationRepository
    """
    return CalendarIntegrationRepository(db)
