"""
Google Calendar Synchronization

Syncs appointments from Google Calendar to Jenga system.
Supports multi-tenant architecture with business-specific calendars.

Setup Requirements:
1. Create Google Cloud project at https://console.cloud.google.com/
2. Enable Google Calendar API
3. Create OAuth 2.0 credentials (Desktop application)
4. Download credentials.json to App/ directory
5. Run google_calendar_auth.py to generate token.json
6. Enable in config.yaml: features.enable_google_calendar_sync: true
"""

import os
import logging
from datetime import datetime, timedelta
from typing import Optional, List
from pathlib import Path

from sqlalchemy.orm import Session

from models import Appointment, Client, Business, AppointmentStatus
from config_loader import config

logger = logging.getLogger(__name__)

# Path to credentials (relative to this file)
APP_DIR = Path(__file__).parent.resolve()
TOKEN_PATH = APP_DIR / 'token.json'
CREDENTIALS_PATH = APP_DIR / 'credentials.json'

# Need full read/write access for write-back functionality
SCOPES = ['https://www.googleapis.com/auth/calendar.events']


def get_calendar_service():
    """
    Get authenticated Google Calendar service.

    Returns:
        Google Calendar API service object, or None if auth fails
    """
    try:
        from google.oauth2.credentials import Credentials
        from google.auth.transport.requests import Request
        from googleapiclient.discovery import build
    except ImportError:
        logger.error(
            "Google API libraries not installed. "
            "Run: pip install google-auth google-auth-oauthlib google-api-python-client"
        )
        return None

    creds = None

    # Load existing token
    if TOKEN_PATH.exists():
        try:
            creds = Credentials.from_authorized_user_file(str(TOKEN_PATH), SCOPES)
        except Exception as e:
            logger.warning(f"Failed to load token.json: {e}")

    # Check if credentials are valid
    if not creds or not creds.valid:
        if creds and creds.expired and creds.refresh_token:
            try:
                creds.refresh(Request())
                # Save refreshed credentials
                with open(TOKEN_PATH, 'w') as token:
                    token.write(creds.to_json())
            except Exception as e:
                logger.error(f"Failed to refresh credentials: {e}")
                return None
        else:
            logger.error(
                "No valid credentials. Run google_calendar_auth.py first "
                "to authenticate with Google Calendar."
            )
            return None

    try:
        service = build('calendar', 'v3', credentials=creds)
        return service
    except Exception as e:
        logger.error(f"Failed to build calendar service: {e}")
        return None


def read_google_calendar(db: Session, business_id: Optional[int] = None) -> dict:
    """
    Read events from Google Calendar and sync to Jenga.

    Args:
        db: Database session
        business_id: Optional business ID to sync for (syncs all if None)

    Returns:
        dict with sync results
    """
    results = {
        "synced": 0,
        "skipped": 0,
        "errors": 0,
        "events_found": 0
    }

    # Check if calendar sync is enabled
    if not config.features.enable_google_calendar_sync:
        logger.info("Google Calendar sync is disabled in configuration")
        return results

    # Get calendar service
    service = get_calendar_service()
    if not service:
        logger.error("Could not initialize Google Calendar service")
        return results

    try:
        # Get events from now to configured window
        now = datetime.utcnow().isoformat() + 'Z'
        time_max = (
            datetime.utcnow() +
            timedelta(days=config.engine.appointment_window_days)
        ).isoformat() + 'Z'

        events_result = service.events().list(
            calendarId='primary',
            timeMin=now,
            timeMax=time_max,
            maxResults=100,
            singleEvents=True,
            orderBy='startTime'
        ).execute()

        events = events_result.get('items', [])
        results["events_found"] = len(events)

        if not events:
            logger.info("No upcoming events found in Google Calendar")
            return results

        logger.info(f"Found {len(events)} events in Google Calendar")

        # Get business(es) to sync for
        if business_id:
            businesses = db.query(Business).filter(
                Business.id == business_id,
                Business.is_active == True
            ).all()
        else:
            # Sync for all active businesses (first one for now - multi-calendar later)
            businesses = db.query(Business).filter(Business.is_active == True).limit(1).all()

        if not businesses:
            logger.warning("No active business found for calendar sync")
            return results

        business = businesses[0]

        for event in events:
            try:
                sync_result = _sync_single_event(db, event, business)
                if sync_result == "synced":
                    results["synced"] += 1
                elif sync_result == "skipped":
                    results["skipped"] += 1
                else:
                    results["errors"] += 1
            except Exception as e:
                logger.error(f"Error syncing event {event.get('id')}: {e}")
                results["errors"] += 1

        db.commit()
        logger.info(
            f"Calendar sync complete: {results['synced']} synced, "
            f"{results['skipped']} skipped, {results['errors']} errors"
        )

    except Exception as e:
        logger.error(f"Error reading Google Calendar: {e}", exc_info=True)
        results["errors"] += 1

    return results


def _sync_single_event(db: Session, event: dict, business: Business) -> str:
    """
    Sync a single calendar event to Jenga appointment.

    Args:
        db: Database session
        event: Google Calendar event dict
        business: Business to create appointment for

    Returns:
        "synced", "skipped", or "error"
    """
    event_id = event.get('id')
    summary = event.get('summary', 'Calendar Event')

    # Get event time
    start = event.get('start', {})
    start_time_str = start.get('dateTime') or start.get('date')

    if not start_time_str:
        logger.warning(f"Event {event_id} has no start time, skipping")
        return "skipped"

    # Parse datetime
    try:
        if 'T' in start_time_str:
            # DateTime format
            start_time = datetime.fromisoformat(start_time_str.replace('Z', '+00:00'))
        else:
            # Date only (all-day event)
            start_time = datetime.fromisoformat(start_time_str)
            start_time = start_time.replace(hour=9, minute=0)  # Default to 9 AM
    except ValueError as e:
        logger.warning(f"Could not parse start time for event {event_id}: {e}")
        return "error"

    # Make timezone-naive for SQLite compatibility
    if start_time.tzinfo:
        start_time = start_time.replace(tzinfo=None)

    # Check if appointment with this external_id already exists
    existing = db.query(Appointment).filter(
        Appointment.business_id == business.id,
        Appointment.external_id == event_id
    ).first()

    if existing:
        # Check if we need to update the time
        if existing.appointment_time != start_time:
            existing.appointment_time = start_time
            existing.updated_at = datetime.utcnow()
            logger.info(f"Updated appointment time for event {event_id}")
            return "synced"
        return "skipped"

    # Get or create client based on event attendees/creator
    attendees = event.get('attendees', [])
    creator = event.get('creator', {})

    # Use first attendee or creator email
    client_email = None
    client_name = summary  # Default to event summary

    if attendees:
        attendee = attendees[0]
        client_email = attendee.get('email')
        client_name = attendee.get('displayName', client_email or summary)
    elif creator:
        client_email = creator.get('email')
        client_name = creator.get('displayName', client_email or summary)

    # Find or create client
    client = None
    if client_email:
        client = db.query(Client).filter(
            Client.business_id == business.id,
            Client.email == client_email
        ).first()

    if not client:
        # Create a new client for this calendar event
        client = Client(
            business_id=business.id,
            external_id=f"gcal_{event_id[:20]}",
            name=client_name[:255] if client_name else "Calendar Client",
            email=client_email,
            segment="regular"
        )
        db.add(client)
        db.flush()  # Get client ID
        logger.info(f"Created client for calendar event: {client_name}")

    # Calculate duration from event end time
    end = event.get('end', {})
    end_time_str = end.get('dateTime') or end.get('date')
    duration_minutes = 60  # Default

    if end_time_str:
        try:
            if 'T' in end_time_str:
                end_time = datetime.fromisoformat(end_time_str.replace('Z', '+00:00'))
                if end_time.tzinfo:
                    end_time = end_time.replace(tzinfo=None)
                duration_minutes = int((end_time - start_time).total_seconds() / 60)
                duration_minutes = max(15, min(duration_minutes, 480))  # Clamp to valid range
        except ValueError:
            pass

    # Create appointment
    appointment = Appointment(
        business_id=business.id,
        client_id=client.id,
        external_id=event_id,
        appointment_time=start_time,
        duration_minutes=duration_minutes,
        appointment_type="calendar_sync",
        status=AppointmentStatus.SCHEDULED,
        is_movable=True,
        no_show_risk=0.3,  # Default risk for calendar events
        ml_model_version=config.ml.version
    )

    db.add(appointment)
    logger.info(
        f"Created appointment from calendar: {summary} at {start_time} "
        f"(duration: {duration_minutes} min)"
    )

    return "synced"


def sync_calendar_for_business(db: Session, business_id: int) -> dict:
    """
    Manually trigger calendar sync for a specific business.

    Args:
        db: Database session
        business_id: Business ID to sync for

    Returns:
        dict with sync results
    """
    return read_google_calendar(db, business_id=business_id)


# ===== Write-Back Functions =====

def update_google_calendar_event(
    external_id: str,
    new_start_time: datetime,
    duration_minutes: int = 60,
    calendar_id: str = 'primary'
) -> dict:
    """
    Update a Google Calendar event with a new time.

    This is called when Jenga moves an appointment (e.g., via cascade).

    Args:
        external_id: The Google Calendar event ID
        new_start_time: New start time for the event
        duration_minutes: Duration of the event in minutes
        calendar_id: Calendar ID (default: 'primary')

    Returns:
        dict with update results
    """
    result = {
        "success": False,
        "event_id": external_id,
        "error": None
    }

    # Get calendar service
    service = get_calendar_service()
    if not service:
        result["error"] = "Could not initialize Google Calendar service"
        logger.error(result["error"])
        return result

    try:
        # First, get the existing event to preserve other fields
        event = service.events().get(
            calendarId=calendar_id,
            eventId=external_id
        ).execute()

        if not event:
            result["error"] = f"Event {external_id} not found in Google Calendar"
            logger.error(result["error"])
            return result

        # Calculate new end time
        new_end_time = new_start_time + timedelta(minutes=duration_minutes)

        # Update the event times
        # Preserve timezone if original event had one
        original_start = event.get('start', {})
        has_timezone = 'dateTime' in original_start and 'timeZone' in original_start

        if has_timezone:
            timezone = original_start.get('timeZone', 'UTC')
            event['start'] = {
                'dateTime': new_start_time.isoformat(),
                'timeZone': timezone
            }
            event['end'] = {
                'dateTime': new_end_time.isoformat(),
                'timeZone': timezone
            }
        else:
            # Use UTC format
            event['start'] = {
                'dateTime': new_start_time.strftime('%Y-%m-%dT%H:%M:%S') + 'Z'
            }
            event['end'] = {
                'dateTime': new_end_time.strftime('%Y-%m-%dT%H:%M:%S') + 'Z'
            }

        # Update the event in Google Calendar
        updated_event = service.events().update(
            calendarId=calendar_id,
            eventId=external_id,
            body=event
        ).execute()

        result["success"] = True
        result["updated_time"] = new_start_time.isoformat()
        result["google_link"] = updated_event.get('htmlLink')

        logger.info(
            f"Updated Google Calendar event {external_id}: "
            f"new time = {new_start_time}"
        )

        return result

    except Exception as e:
        result["error"] = str(e)
        logger.error(f"Failed to update Google Calendar event {external_id}: {e}")
        return result


def sync_appointment_to_google(appointment: Appointment) -> dict:
    """
    Sync a Jenga appointment back to Google Calendar.

    This updates the Google Calendar event to match Jenga's data.

    Args:
        appointment: The Jenga appointment to sync

    Returns:
        dict with sync results
    """
    if not appointment.external_id:
        return {
            "success": False,
            "error": "Appointment has no external_id (not from Google Calendar)"
        }

    # Check if external_id looks like a Google Calendar ID
    # Calendly IDs start with 'https://api.calendly.com'
    if appointment.external_id.startswith('https://'):
        return {
            "success": False,
            "error": "Appointment is from Calendly, not Google Calendar directly"
        }

    return update_google_calendar_event(
        external_id=appointment.external_id,
        new_start_time=appointment.appointment_time,
        duration_minutes=appointment.duration_minutes
    )


def sync_moved_appointments_to_google(db: Session, appointment_ids: List[int]) -> dict:
    """
    Sync multiple moved appointments back to Google Calendar.

    Called after cascade moves appointments.

    Args:
        db: Database session
        appointment_ids: List of appointment IDs that were moved

    Returns:
        dict with sync results for each appointment
    """
    results = {
        "total": len(appointment_ids),
        "synced": 0,
        "skipped": 0,
        "errors": 0,
        "details": []
    }

    for apt_id in appointment_ids:
        appointment = db.query(Appointment).filter(Appointment.id == apt_id).first()

        if not appointment:
            results["errors"] += 1
            results["details"].append({
                "appointment_id": apt_id,
                "success": False,
                "error": "Appointment not found"
            })
            continue

        sync_result = sync_appointment_to_google(appointment)

        if sync_result.get("success"):
            results["synced"] += 1
        elif "Calendly" in sync_result.get("error", ""):
            results["skipped"] += 1  # Calendly appointments can't be synced this way
        else:
            results["errors"] += 1

        results["details"].append({
            "appointment_id": apt_id,
            **sync_result
        })

    logger.info(
        f"Google Calendar write-back complete: "
        f"{results['synced']} synced, {results['skipped']} skipped, {results['errors']} errors"
    )

    return results
