"""
Calendly Calendar Adapter

Integrates Calendly with Jenga using Personal Access Tokens.

INVARIANTS:
- This adapter translates Calendly ↔ Jenga, nothing more
- It does NOT orchestrate or make decisions
- It does NOT mutate Jenga's internal state
- Calendly is a DATA SOURCE, not the source of truth

Jenga is the system of record. Calendly informs Jenga.

API Reference: https://developer.calendly.com/api-docs
"""

import logging
from datetime import datetime, timedelta
from typing import Optional, Dict, Any, List

import requests

from jenga.adapters.calendar.protocol import (
    CalendarAdapter,
    CalendarEvent,
    CalendarEventType,
    CalendarProvider,
    CalendarConnectionStatus,
    ConnectionResult,
    FetchResult,
)

logger = logging.getLogger(__name__)


# Calendly API configuration
CALENDLY_API_BASE = "https://api.calendly.com"
CALENDLY_API_TIMEOUT = 30  # seconds


class CalendlyAdapter(CalendarAdapter):
    """
    Calendly adapter using Personal Access Token authentication.

    USAGE:
    1. User generates PAT at https://calendly.com/integrations/api_webhooks
    2. Token is passed to Jenga and stored per-business
    3. Adapter uses token to fetch events and event types
    4. Jenga maps Calendly events to its internal WorkflowInstance model

    INVARIANTS:
    - Stateless: No cached data, fresh API calls each time
    - Read-focused: Primarily fetches data from Calendly
    - Translator: Converts Calendly responses to Jenga's CalendarEvent model
    """

    def __init__(self, timeout: int = CALENDLY_API_TIMEOUT):
        """
        Initialize Calendly adapter.

        Args:
            timeout: Request timeout in seconds
        """
        self._timeout = timeout

    @property
    def provider(self) -> CalendarProvider:
        """Return the provider type."""
        return CalendarProvider.CALENDLY

    def _make_request(
        self,
        method: str,
        endpoint: str,
        token: str,
        params: Optional[Dict[str, Any]] = None,
        json_data: Optional[Dict[str, Any]] = None,
    ) -> requests.Response:
        """
        Make authenticated request to Calendly API.

        Args:
            method: HTTP method
            endpoint: API endpoint (without base URL)
            token: Personal Access Token
            params: Query parameters
            json_data: JSON body for POST/PUT

        Returns:
            Response object
        """
        url = f"{CALENDLY_API_BASE}{endpoint}"
        headers = {
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json",
        }

        response = requests.request(
            method=method,
            url=url,
            headers=headers,
            params=params,
            json=json_data,
            timeout=self._timeout,
        )
        return response

    def validate_token(self, token: str) -> ConnectionResult:
        """
        Validate Calendly PAT and retrieve user info.

        Calls /users/me to verify the token works.

        Args:
            token: Calendly Personal Access Token

        Returns:
            ConnectionResult with user info or error
        """
        try:
            response = self._make_request("GET", "/users/me", token)

            if response.status_code == 200:
                data = response.json()
                resource = data.get("resource", {})

                return ConnectionResult(
                    success=True,
                    status=CalendarConnectionStatus.CONNECTED,
                    user_info={
                        "uri": resource.get("uri"),
                        "name": resource.get("name"),
                        "email": resource.get("email"),
                        "slug": resource.get("slug"),
                        "scheduling_url": resource.get("scheduling_url"),
                        "timezone": resource.get("timezone"),
                        "current_organization": resource.get("current_organization"),
                    },
                )
            elif response.status_code == 401:
                return ConnectionResult(
                    success=False,
                    status=CalendarConnectionStatus.INVALID,
                    error="Invalid or expired token",
                )
            else:
                return ConnectionResult(
                    success=False,
                    status=CalendarConnectionStatus.DISCONNECTED,
                    error=f"Calendly API error: {response.status_code}",
                )

        except requests.Timeout:
            return ConnectionResult(
                success=False,
                status=CalendarConnectionStatus.DISCONNECTED,
                error="Calendly API timeout",
            )
        except requests.RequestException as e:
            logger.error(f"Calendly connection error: {e}")
            return ConnectionResult(
                success=False,
                status=CalendarConnectionStatus.DISCONNECTED,
                error=str(e),
            )

    def get_event_types(self, token: str) -> FetchResult:
        """
        Fetch Calendly event types for the authenticated user.

        Args:
            token: Valid Calendly PAT

        Returns:
            FetchResult with list of CalendarEventType
        """
        try:
            # First get user URI
            user_result = self.validate_token(token)
            if not user_result.success:
                return FetchResult(
                    success=False,
                    data=[],
                    error=user_result.error,
                )

            user_uri = user_result.user_info.get("uri")
            if not user_uri:
                return FetchResult(
                    success=False,
                    data=[],
                    error="Could not determine user URI",
                )

            # Fetch event types
            response = self._make_request(
                "GET",
                "/event_types",
                token,
                params={"user": user_uri, "active": "true"},
            )

            if response.status_code != 200:
                return FetchResult(
                    success=False,
                    data=[],
                    error=f"Calendly API error: {response.status_code}",
                    raw_response=response.json() if response.text else None,
                )

            data = response.json()
            event_types = []

            for item in data.get("collection", []):
                event_type = CalendarEventType(
                    external_id=item.get("uri", ""),
                    name=item.get("name", "Unknown"),
                    duration_minutes=item.get("duration", 60),
                    provider=CalendarProvider.CALENDLY,
                    color=item.get("color"),
                    description=item.get("description_plain"),
                )
                event_types.append(event_type)

            logger.info(f"Fetched {len(event_types)} event types from Calendly")

            return FetchResult(
                success=True,
                data=event_types,
                raw_response=data,
            )

        except requests.RequestException as e:
            logger.error(f"Calendly event types fetch error: {e}")
            return FetchResult(
                success=False,
                data=[],
                error=str(e),
            )

    def get_scheduled_events(
        self,
        token: str,
        from_time: Optional[datetime] = None,
        to_time: Optional[datetime] = None,
        event_type_id: Optional[str] = None,
    ) -> FetchResult:
        """
        Fetch scheduled events from Calendly.

        Args:
            token: Valid Calendly PAT
            from_time: Start of time range (default: now)
            to_time: End of time range (default: 30 days out)
            event_type_id: Filter to specific event type URI (optional)

        Returns:
            FetchResult with list of CalendarEvent
        """
        try:
            # Get user URI first
            user_result = self.validate_token(token)
            if not user_result.success:
                return FetchResult(
                    success=False,
                    data=[],
                    error=user_result.error,
                )

            user_uri = user_result.user_info.get("uri")
            if not user_uri:
                return FetchResult(
                    success=False,
                    data=[],
                    error="Could not determine user URI",
                )

            # Set default time range
            if from_time is None:
                from_time = datetime.utcnow()
            if to_time is None:
                to_time = from_time + timedelta(days=30)

            # Build params
            params = {
                "user": user_uri,
                "min_start_time": from_time.isoformat() + "Z",
                "max_start_time": to_time.isoformat() + "Z",
                "status": "active",  # Only non-cancelled events
                "count": 100,  # Max per page
            }

            if event_type_id:
                params["event_type"] = event_type_id

            # Fetch events (with pagination)
            all_events = []
            next_page = None

            while True:
                if next_page:
                    params["page_token"] = next_page

                response = self._make_request(
                    "GET",
                    "/scheduled_events",
                    token,
                    params=params,
                )

                if response.status_code != 200:
                    return FetchResult(
                        success=False,
                        data=all_events,  # Return what we have
                        error=f"Calendly API error: {response.status_code}",
                        raw_response=response.json() if response.text else None,
                    )

                data = response.json()

                for item in data.get("collection", []):
                    event = self._parse_calendly_event(item)
                    if event:
                        all_events.append(event)

                # Check pagination
                pagination = data.get("pagination", {})
                next_page = pagination.get("next_page_token")

                if not next_page:
                    break

            logger.info(f"Fetched {len(all_events)} events from Calendly")

            return FetchResult(
                success=True,
                data=all_events,
            )

        except requests.RequestException as e:
            logger.error(f"Calendly events fetch error: {e}")
            return FetchResult(
                success=False,
                data=[],
                error=str(e),
            )

    def get_event_by_id(self, token: str, event_id: str) -> Optional[CalendarEvent]:
        """
        Fetch a single event by its Calendly URI.

        Args:
            token: Valid Calendly PAT
            event_id: Calendly event URI

        Returns:
            CalendarEvent if found, None otherwise
        """
        try:
            # Calendly event URIs are full URLs like:
            # https://api.calendly.com/scheduled_events/xxx
            # We need to extract the path or use the full URI

            if event_id.startswith("https://"):
                # Full URI, extract path
                endpoint = event_id.replace(CALENDLY_API_BASE, "")
            else:
                # Assume it's just the event ID
                endpoint = f"/scheduled_events/{event_id}"

            response = self._make_request("GET", endpoint, token)

            if response.status_code != 200:
                logger.warning(f"Could not fetch Calendly event {event_id}: {response.status_code}")
                return None

            data = response.json()
            resource = data.get("resource", {})

            return self._parse_calendly_event(resource)

        except requests.RequestException as e:
            logger.error(f"Calendly event fetch error: {e}")
            return None

    def _parse_calendly_event(self, data: Dict[str, Any]) -> Optional[CalendarEvent]:
        """
        Parse Calendly event data into CalendarEvent.

        Args:
            data: Raw Calendly event data

        Returns:
            CalendarEvent or None if parsing fails
        """
        try:
            # Parse times
            start_time_str = data.get("start_time")
            end_time_str = data.get("end_time")

            if not start_time_str or not end_time_str:
                return None

            start_time = datetime.fromisoformat(start_time_str.replace("Z", "+00:00"))
            end_time = datetime.fromisoformat(end_time_str.replace("Z", "+00:00"))

            # Calculate duration
            duration_minutes = int((end_time - start_time).total_seconds() / 60)

            # Extract invitee info (need separate API call for full details)
            # For now, use what's available in the event
            invitees_counter = data.get("invitees_counter", {})
            total_invitees = invitees_counter.get("total", 0)

            # Status
            status = data.get("status", "active")
            is_cancelled = status == "canceled"

            # Event type info
            event_type_uri = data.get("event_type")

            return CalendarEvent(
                external_id=data.get("uri", ""),
                provider=CalendarProvider.CALENDLY,
                start_time=start_time,
                end_time=end_time,
                duration_minutes=duration_minutes,
                event_type_id=event_type_uri,
                event_type_name=data.get("name"),
                location=self._extract_location(data),
                is_cancelled=is_cancelled,
                raw_data=data,
            )

        except (ValueError, KeyError) as e:
            logger.warning(f"Failed to parse Calendly event: {e}")
            return None

    def _extract_location(self, data: Dict[str, Any]) -> Optional[str]:
        """Extract location from Calendly event data."""
        location = data.get("location", {})

        if isinstance(location, dict):
            loc_type = location.get("type")
            if loc_type == "physical":
                return location.get("location")
            elif loc_type == "custom":
                return location.get("location")
            elif loc_type == "outbound_call":
                return "Phone call (outbound)"
            elif loc_type == "inbound_call":
                return "Phone call (inbound)"
            elif loc_type == "google_conference":
                return location.get("join_url", "Google Meet")
            elif loc_type == "zoom":
                return location.get("join_url", "Zoom")
            elif loc_type == "microsoft_teams_conference":
                return location.get("join_url", "Microsoft Teams")
            else:
                return location.get("location") or loc_type
        elif isinstance(location, str):
            return location

        return None

    def get_event_invitees(self, token: str, event_id: str) -> List[Dict[str, Any]]:
        """
        Fetch invitees for a specific event.

        This is a helper method to get full invitee details.

        Args:
            token: Valid Calendly PAT
            event_id: Calendly event URI

        Returns:
            List of invitee dictionaries
        """
        try:
            if event_id.startswith("https://"):
                endpoint = event_id.replace(CALENDLY_API_BASE, "") + "/invitees"
            else:
                endpoint = f"/scheduled_events/{event_id}/invitees"

            response = self._make_request("GET", endpoint, token)

            if response.status_code != 200:
                return []

            data = response.json()
            invitees = []

            for item in data.get("collection", []):
                invitees.append({
                    "uri": item.get("uri"),
                    "email": item.get("email"),
                    "name": item.get("name"),
                    "status": item.get("status"),
                    "timezone": item.get("timezone"),
                    "created_at": item.get("created_at"),
                })

            return invitees

        except requests.RequestException as e:
            logger.error(f"Calendly invitees fetch error: {e}")
            return []

    def cancel_event(self, token: str, event_id: str, reason: Optional[str] = None) -> bool:
        """
        Cancel a Calendly event.

        Note: Calendly cancellation is done through invitee cancellation.

        Args:
            token: Valid Calendly PAT
            event_id: Calendly event URI
            reason: Cancellation reason

        Returns:
            True if cancelled, False otherwise
        """
        logger.warning(
            "Calendly event cancellation not implemented. "
            "Use Calendly's cancel_url from invitee data."
        )
        return False


def create_calendly_adapter(timeout: int = CALENDLY_API_TIMEOUT) -> CalendlyAdapter:
    """
    Factory function to create Calendly adapter.

    Args:
        timeout: Request timeout in seconds

    Returns:
        Configured CalendlyAdapter
    """
    return CalendlyAdapter(timeout=timeout)
