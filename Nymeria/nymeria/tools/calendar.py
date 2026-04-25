"""Google Calendar tools — native Python implementation.

Uses google-api-python-client for direct Google Calendar API calls.
Authentication is handled by calendar_auth.py (OAuth 2.0 authorization
code flow with localhost redirect + manual fallback).

Optional tools — enable per-thread via thread config.
"""

import json
import logging
import time
from datetime import datetime, timezone
from typing import Annotated, Any, Callable, Optional

from langchain_core.runnables import RunnableConfig
from langchain_core.tools import InjectedToolArg, tool

from .calendar_auth import (
    CALENDAR_AUTH_TOOLS,
    GOOGLE_SCOPES,
    load_token_cache,
    save_token_cache,
)
from .utils import get_user_id

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Credential management
# ---------------------------------------------------------------------------

def get_credentials(user_id: str, account_id: Optional[str] = None):
    """
    Get valid Google OAuth credentials for a Nymeria user, refreshing the
    access token if expired.

    Mirrors get_access_token() in outlook_email.py — checks expiry with a
    60-second buffer and auto-refreshes via the stored refresh token.

    Returns:
        google.oauth2.credentials.Credentials if available, None otherwise.
    """
    # Lazy imports — these packages are optional dependencies
    try:
        from google.auth.exceptions import RefreshError
        from google.auth.transport.requests import Request as GoogleAuthRequest
        from google.oauth2.credentials import Credentials
    except ImportError:
        logger.error(
            "google-auth packages not installed. "
            "Run: pip install google-api-python-client google-auth-oauthlib"
        )
        return None

    cache = load_token_cache(user_id)
    accounts = cache.get("accounts", {})

    if not accounts:
        return None

    if account_id:
        account = accounts.get(account_id)
        aid = account_id
    else:
        aid, account = next(iter(accounts.items()), (None, None))

    if not account:
        return None

    # Build Credentials object from stored data
    creds = Credentials(
        token=account.get("access_token"),
        refresh_token=account.get("refresh_token"),
        token_uri=account.get("token_uri", "https://oauth2.googleapis.com/token"),
        client_id=account.get("client_id"),
        client_secret=account.get("client_secret"),
        scopes=account.get("scopes", list(GOOGLE_SCOPES)),
    )

    # Check expiry (60-second buffer, same as Outlook pattern)
    expires_at = account.get("expires_at", 0)
    if time.time() < expires_at - 60:
        return creds

    # Token expired — refresh it
    if not creds.refresh_token:
        logger.warning("Google token expired and no refresh token available.")
        return None

    try:
        creds.refresh(GoogleAuthRequest())
        # Update cache with new token and expiry
        account["access_token"] = creds.token
        account["expires_at"] = (
            creds.expiry.timestamp() if creds.expiry else time.time() + 3600
        )
        if creds.refresh_token:
            account["refresh_token"] = creds.refresh_token
        cache["accounts"][aid] = account
        save_token_cache(user_id, cache)
        return creds
    except RefreshError as e:
        logger.warning(f"Refresh token revoked or expired: {e}. User must re-authenticate.")
        return None
    except Exception as e:
        logger.error(f"Google token refresh failed: {e}")
        return None


# ---------------------------------------------------------------------------
# Central request helper
# ---------------------------------------------------------------------------

def _calendar_request(
    user_id: str,
    operation: Callable,
    account_id: Optional[str] = None,
) -> tuple[bool, Any]:
    """
    Execute a Google Calendar API operation with auth handling.

    Args:
        user_id: Nymeria account making the request.
        operation: A callable that takes a ``service`` object and returns
                   the API result, e.g. ``lambda s: s.calendarList().list().execute()``.
        account_id: Optional account ID to use.

    Returns:
        ``(success, result)`` — on failure *result* is an error message string.
    """
    try:
        from googleapiclient.discovery import build
        from googleapiclient.errors import HttpError
    except ImportError:
        return False, (
            "google-api-python-client not installed. "
            "Run: pip install google-api-python-client google-auth-oauthlib"
        )

    creds = get_credentials(user_id, account_id)
    if not creds:
        return False, "No authenticated Google account. Use calendar_auth_start to authenticate."

    try:
        service = build("calendar", "v3", credentials=creds)
        result = operation(service)
        return True, result
    except HttpError as e:
        try:
            error_details = json.loads(e.content.decode()) if e.content else {}
            msg = error_details.get("error", {}).get("message", str(e))
        except Exception:
            msg = str(e)
        return False, f"Google Calendar API error ({e.resp.status}): {msg}"
    except Exception as e:
        logger.error(f"Calendar request failed: {e}", exc_info=True)
        return False, f"Request failed: {str(e)}"


# ---------------------------------------------------------------------------
# Formatting helpers
# ---------------------------------------------------------------------------

def _format_event_summary(evt: dict) -> str:
    """Format a calendar event as a short summary string."""
    summary = evt.get("summary", "(no title)")
    evt_id = evt.get("id", "")[:12]
    start = evt.get("start", {})
    start_str = start.get("dateTime") or start.get("date", "")
    if start_str and "T" in start_str:
        start_str = start_str[:16].replace("T", " ")
    location = evt.get("location", "")
    loc_str = f" @ {location}" if location else ""
    status = evt.get("status", "")
    status_str = f" ({status})" if status and status != "confirmed" else ""
    return f"- [{start_str}] **{summary}**{loc_str}{status_str}\n  ID: `{evt_id}...`"


def _format_event_detail(evt: dict) -> str:
    """Format a calendar event with full detail."""
    lines = ["[Success]: Event details\n"]

    summary = evt.get("summary", "(no title)")
    lines.append(f"**Title:** {summary}")

    start = evt.get("start", {})
    end = evt.get("end", {})
    start_str = start.get("dateTime") or start.get("date", "")
    end_str = end.get("dateTime") or end.get("date", "")
    lines.append(f"**Start:** {start_str}")
    lines.append(f"**End:** {end_str}")

    if evt.get("location"):
        lines.append(f"**Location:** {evt['location']}")
    if evt.get("description"):
        desc = evt["description"][:500]
        lines.append(f"**Description:** {desc}")

    status = evt.get("status", "")
    if status:
        lines.append(f"**Status:** {status}")

    organizer = evt.get("organizer", {})
    if organizer:
        org_str = organizer.get("email", "")
        if organizer.get("displayName"):
            org_str = f"{organizer['displayName']} ({org_str})"
        lines.append(f"**Organizer:** {org_str}")

    attendees = evt.get("attendees", [])
    if attendees:
        att_lines = []
        for att in attendees[:10]:
            att_email = att.get("email", "")
            att_name = att.get("displayName", "")
            att_status = att.get("responseStatus", "needsAction")
            att_str = f"{att_name} <{att_email}>" if att_name else att_email
            att_lines.append(f"  - {att_str} ({att_status})")
        lines.append("**Attendees:**")
        lines.extend(att_lines)
        if len(attendees) > 10:
            lines.append(f"  ... and {len(attendees) - 10} more")

    lines.append(f"\n**Event ID:** `{evt.get('id', '')}`")
    if evt.get("htmlLink"):
        lines.append(f"**Link:** {evt['htmlLink']}")

    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Calendar tools
# ---------------------------------------------------------------------------

@tool
def calendar_list_calendars(account_id: Optional[str] = None, config: Annotated[RunnableConfig, InjectedToolArg] = None) -> str:
    """
    List all available Google calendars for the authenticated account.

    Returns a list of calendars with their IDs, names, and access roles.
    Use this to discover which calendars are available before listing events.

    Args:
        account_id: Google account ID (optional, uses first account if not specified)

    Returns:
        List of calendars with id, summary, and accessRole
    """
    user_id = get_user_id(config)
    logger.info("calendar_list_calendars called")

    success, result = _calendar_request(user_id, lambda s: s.calendarList().list().execute(),
        account_id=account_id,
    )
    if not success:
        return f"[Error]: {result}"

    items = result.get("items", [])
    if not items:
        return "[Info]: No calendars found."

    lines = [f"[Success]: Found {len(items)} calendar(s):\n"]
    for cal in items:
        cal_id = cal.get("id", "")
        summary = cal.get("summary", "(no name)")
        role = cal.get("accessRole", "unknown")
        primary = " (primary)" if cal.get("primary") else ""
        lines.append(f"- **{summary}**{primary}")
        lines.append(f"  ID: `{cal_id}` | Access: {role}")

    return "\n".join(lines)


@tool
def calendar_list_events(
    calendar_id: str = "primary",
    max_results: int = 10,
    time_min: Optional[str] = None,
    time_max: Optional[str] = None,
    account_id: Optional[str] = None,
    config: Annotated[RunnableConfig, InjectedToolArg] = None,
) -> str:
    """
    List events from a Google Calendar.

    Args:
        calendar_id: Calendar ID to list events from. Use "primary" for the main calendar.
        max_results: Maximum number of events to return (default: 10)
        time_min: Start time in ISO 8601 format (e.g., "2024-01-15T00:00:00Z")
        time_max: End time in ISO 8601 format (e.g., "2024-01-31T23:59:59Z")
        account_id: Google account ID (optional, uses first account if not specified)

    Returns:
        List of events with their details (id, summary, start, end, etc.)
    """
    user_id = get_user_id(config)
    logger.info(f"calendar_list_events called: calendar_id={calendar_id}, max_results={max_results}")

    def _op(s):
        kwargs = {
            "calendarId": calendar_id,
            "maxResults": max_results,
            "singleEvents": True,
            "orderBy": "startTime",
        }
        if time_min:
            kwargs["timeMin"] = time_min
        if time_max:
            kwargs["timeMax"] = time_max
        return s.events().list(**kwargs).execute()

    success, result = _calendar_request(_op, account_id=account_id)
    if not success:
        return f"[Error]: {result}"

    events = result.get("items", [])
    if not events:
        return "[Info]: No events found."

    lines = [f"[Success]: Found {len(events)} event(s):\n"]
    for evt in events:
        lines.append(_format_event_summary(evt))

    return "\n".join(lines)


@tool
def calendar_get_event(
    event_id: str,
    calendar_id: str = "primary",
    account_id: Optional[str] = None,
    config: Annotated[RunnableConfig, InjectedToolArg] = None,
) -> str:
    """
    Get detailed information about a specific calendar event.

    Args:
        event_id: The ID of the event to retrieve
        calendar_id: Calendar ID containing the event (default: "primary")
        account_id: Google account ID (optional, uses first account if not specified)

    Returns:
        Full event details including description, attendees, location, etc.
    """
    user_id = get_user_id(config)
    logger.info(f"calendar_get_event called: event_id={event_id}, calendar_id={calendar_id}")

    success, result = _calendar_request(user_id, lambda s: s.events().get(calendarId=calendar_id, eventId=event_id).execute(),
        account_id=account_id,
    )
    if not success:
        return f"[Error]: {result}"

    return _format_event_detail(result)


@tool
def calendar_search_events(
    query: str,
    calendar_id: str = "primary",
    max_results: int = 10,
    account_id: Optional[str] = None,
    config: Annotated[RunnableConfig, InjectedToolArg] = None,
) -> str:
    """
    Search for events by text query.

    Searches event titles, descriptions, locations, and attendee names.

    Args:
        query: Search query string
        calendar_id: Calendar ID to search in (default: "primary")
        max_results: Maximum number of results (default: 10)
        account_id: Google account ID (optional, uses first account if not specified)

    Returns:
        List of matching events
    """
    user_id = get_user_id(config)
    logger.info(f"calendar_search_events called: query={query}, calendar_id={calendar_id}")

    success, result = _calendar_request(user_id, lambda s: s.events().list(
            calendarId=calendar_id,
            q=query,
            maxResults=max_results,
            singleEvents=True,
            orderBy="startTime",
        ).execute(),
        account_id=account_id,
    )
    if not success:
        return f"[Error]: {result}"

    events = result.get("items", [])
    if not events:
        return f"[Info]: No events found matching '{query}'."

    lines = [f"[Success]: Found {len(events)} event(s) matching '{query}':\n"]
    for evt in events:
        lines.append(_format_event_summary(evt))

    return "\n".join(lines)


@tool
def calendar_create_event(
    summary: str,
    start_time: str,
    end_time: str,
    calendar_id: str = "primary",
    description: Optional[str] = None,
    location: Optional[str] = None,
    attendees: Optional[str] = None,
    timezone: Optional[str] = None,
    account_id: Optional[str] = None,
    config: Annotated[RunnableConfig, InjectedToolArg] = None,
) -> str:
    """
    Create a new calendar event.

    Args:
        summary: Event title/summary
        start_time: Start time in ISO 8601 format (e.g., "2024-01-15T10:00:00")
                    or date-only for all-day events (e.g., "2024-01-15")
        end_time: End time in ISO 8601 format (e.g., "2024-01-15T11:00:00")
                  or date-only for all-day events (e.g., "2024-01-16")
        calendar_id: Calendar ID to create event in (default: "primary")
        description: Optional event description
        location: Optional event location
        attendees: Optional comma-separated list of attendee email addresses
        timezone: Optional timezone (e.g., "America/New_York"). Defaults to calendar's timezone.
        account_id: Google account ID (optional, uses first account if not specified)

    Returns:
        Created event details including the event ID
    """
    user_id = get_user_id(config)
    logger.info(f"calendar_create_event called: summary={summary}, start={start_time}, end={end_time}")

    def _op(s):
        body: dict[str, Any] = {"summary": summary}

        # Detect all-day events (date-only strings have no "T")
        if "T" in start_time:
            start_obj: dict[str, str] = {"dateTime": start_time}
            end_obj: dict[str, str] = {"dateTime": end_time}
            if timezone:
                start_obj["timeZone"] = timezone
                end_obj["timeZone"] = timezone
            body["start"] = start_obj
            body["end"] = end_obj
        else:
            body["start"] = {"date": start_time}
            body["end"] = {"date": end_time}

        if description:
            body["description"] = description
        if location:
            body["location"] = location
        if attendees:
            body["attendees"] = [
                {"email": e.strip()} for e in attendees.split(",") if e.strip()
            ]

        return s.events().insert(calendarId=calendar_id, body=body).execute()

    success, result = _calendar_request(_op, account_id=account_id)
    if not success:
        return f"[Error]: {result}"

    evt_id = result.get("id", "")
    html_link = result.get("htmlLink", "")
    return f"[Success]: Event created.\n\nID: `{evt_id}`\nLink: {html_link}"


@tool
def calendar_update_event(
    event_id: str,
    calendar_id: str = "primary",
    summary: Optional[str] = None,
    start_time: Optional[str] = None,
    end_time: Optional[str] = None,
    description: Optional[str] = None,
    location: Optional[str] = None,
    account_id: Optional[str] = None,
    config: Annotated[RunnableConfig, InjectedToolArg] = None,
) -> str:
    """
    Update an existing calendar event.

    Only provide the fields you want to change.

    Args:
        event_id: The ID of the event to update
        calendar_id: Calendar ID containing the event (default: "primary")
        summary: New event title/summary
        start_time: New start time in ISO 8601 format
        end_time: New end time in ISO 8601 format
        description: New event description
        location: New event location
        account_id: Google account ID (optional, uses first account if not specified)

    Returns:
        Updated event details
    """
    user_id = get_user_id(config)
    logger.info(f"calendar_update_event called: event_id={event_id}")

    def _op(s):
        body: dict[str, Any] = {}

        if summary:
            body["summary"] = summary
        if description is not None:
            body["description"] = description
        if location is not None:
            body["location"] = location
        if start_time:
            if "T" in start_time:
                body["start"] = {"dateTime": start_time}
            else:
                body["start"] = {"date": start_time}
        if end_time:
            if "T" in end_time:
                body["end"] = {"dateTime": end_time}
            else:
                body["end"] = {"date": end_time}

        return s.events().patch(
            calendarId=calendar_id,
            eventId=event_id,
            body=body,
        ).execute()

    success, result = _calendar_request(_op, account_id=account_id)
    if not success:
        return f"[Error]: {result}"

    return f"[Success]: Event updated.\n\nID: `{result.get('id', event_id)}`"


@tool
def calendar_delete_event(
    event_id: str,
    calendar_id: str = "primary",
    account_id: Optional[str] = None,
    config: Annotated[RunnableConfig, InjectedToolArg] = None,
) -> str:
    """
    Delete a calendar event.

    Args:
        event_id: The ID of the event to delete
        calendar_id: Calendar ID containing the event (default: "primary")
        account_id: Google account ID (optional, uses first account if not specified)

    Returns:
        Confirmation of deletion
    """
    user_id = get_user_id(config)
    logger.info(f"calendar_delete_event called: event_id={event_id}, calendar_id={calendar_id}")

    success, result = _calendar_request(user_id, lambda s: s.events().delete(calendarId=calendar_id, eventId=event_id).execute(),
        account_id=account_id,
    )
    if not success:
        return f"[Error]: {result}"

    return f"[Success]: Event `{event_id}` deleted."


@tool
def calendar_respond_to_event(
    event_id: str,
    response: str,
    calendar_id: str = "primary",
    account_id: Optional[str] = None,
    config: Annotated[RunnableConfig, InjectedToolArg] = None,
) -> str:
    """
    Respond to a calendar event invitation.

    Args:
        event_id: The ID of the event to respond to
        response: Response status - one of: "accepted", "declined", "tentative"
        calendar_id: Calendar ID containing the event (default: "primary")
        account_id: Google account ID (optional, uses first account if not specified)

    Returns:
        Confirmation of response
    """
    user_id = get_user_id(config)
    logger.info(f"calendar_respond_to_event called: event_id={event_id}, response={response}")

    valid_responses = ["accepted", "declined", "tentative"]
    if response.lower() not in valid_responses:
        return f"[Error]: Invalid response. Must be one of: {', '.join(valid_responses)}"

    def _op(s):
        # Fetch the event to get attendee list
        evt = s.events().get(calendarId=calendar_id, eventId=event_id).execute()
        attendees = evt.get("attendees", [])

        # Find self in attendees and update response status
        updated = False
        for att in attendees:
            if att.get("self"):
                att["responseStatus"] = response.lower()
                updated = True
                break

        if not updated:
            # Try to find ourselves by calendar owner email
            cal = s.calendars().get(calendarId=calendar_id).execute()
            self_email = cal.get("id", "")
            for att in attendees:
                if att.get("email", "").lower() == self_email.lower():
                    att["responseStatus"] = response.lower()
                    updated = True
                    break

        if not updated:
            raise ValueError(
                "Could not find your email in the event's attendee list. "
                "You may not be invited to this event."
            )

        return s.events().patch(
            calendarId=calendar_id,
            eventId=event_id,
            body={"attendees": attendees},
        ).execute()

    success, result = _calendar_request(_op, account_id=account_id)
    if not success:
        return f"[Error]: {result}"

    return f"[Success]: Responded '{response}' to event `{event_id}`."


@tool
def calendar_get_freebusy(
    time_min: str,
    time_max: str,
    calendars: Optional[str] = None,
    account_id: Optional[str] = None,
    config: Annotated[RunnableConfig, InjectedToolArg] = None,
) -> str:
    """
    Get free/busy information for calendars.

    Useful for finding available time slots for scheduling.

    Args:
        time_min: Start of time range in ISO 8601 format
        time_max: End of time range in ISO 8601 format
        calendars: Optional comma-separated list of calendar IDs. Defaults to primary calendar.
        account_id: Google account ID (optional, uses first account if not specified)

    Returns:
        Free/busy information showing busy time ranges
    """
    user_id = get_user_id(config)
    logger.info(f"calendar_get_freebusy called: time_min={time_min}, time_max={time_max}")

    def _op(s):
        cal_ids = [c.strip() for c in calendars.split(",")] if calendars else ["primary"]
        body = {
            "timeMin": time_min,
            "timeMax": time_max,
            "items": [{"id": c} for c in cal_ids],
        }
        return s.freebusy().query(body=body).execute()

    success, result = _calendar_request(_op, account_id=account_id)
    if not success:
        return f"[Error]: {result}"

    calendars_result = result.get("calendars", {})
    lines = [f"[Success]: Free/busy information from {time_min} to {time_max}:\n"]

    for cal_id, info in calendars_result.items():
        busy = info.get("busy", [])
        errors = info.get("errors", [])
        lines.append(f"**Calendar: {cal_id}**")
        if errors:
            for err in errors:
                lines.append(f"  Error: {err.get('reason', 'unknown')}")
        elif not busy:
            lines.append("  Free (no busy periods)")
        else:
            for period in busy:
                start = period.get("start", "")
                end = period.get("end", "")
                lines.append(f"  Busy: {start} -> {end}")
        lines.append("")

    return "\n".join(lines)


@tool
def calendar_get_current_time(config: Annotated[RunnableConfig, InjectedToolArg] = None) -> str:
    """
    Get the current time in ISO 8601 format.

    Useful for calculating relative times for event scheduling.
    No Google API call is made — returns the local system time.

    Returns:
        Current time as ISO 8601 string with timezone offset.
    """
    user_id = get_user_id(config)
    logger.info("calendar_get_current_time called")
    now = datetime.now(timezone.utc).astimezone()
    return f"[Success]: Current time: {now.isoformat()}"


@tool
def calendar_list_colors(account_id: Optional[str] = None, config: Annotated[RunnableConfig, InjectedToolArg] = None) -> str:
    """
    List available calendar and event colors.

    Returns the color palette that can be used when creating or updating events.

    Args:
        account_id: Google account ID (optional, uses first account if not specified)

    Returns:
        Available color IDs and their corresponding colors
    """
    user_id = get_user_id(config)
    logger.info("calendar_list_colors called")

    success, result = _calendar_request(user_id, lambda s: s.colors().get().execute(),
        account_id=account_id,
    )
    if not success:
        return f"[Error]: {result}"

    event_colors = result.get("event", {})
    calendar_colors = result.get("calendar", {})

    lines = ["[Success]: Available Google Calendar colors:\n"]
    lines.append("**Event colors:**")
    for color_id, info in event_colors.items():
        bg = info.get("background", "")
        fg = info.get("foreground", "")
        lines.append(f"  ID `{color_id}`: background={bg}, foreground={fg}")

    lines.append("\n**Calendar colors:**")
    for color_id, info in calendar_colors.items():
        bg = info.get("background", "")
        lines.append(f"  ID `{color_id}`: background={bg}")

    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Export — auth tools + API tools combined
# ---------------------------------------------------------------------------

CALENDAR_TOOLS = CALENDAR_AUTH_TOOLS + [
    calendar_list_calendars,
    calendar_list_events,
    calendar_get_event,
    calendar_search_events,
    calendar_create_event,
    calendar_update_event,
    calendar_delete_event,
    calendar_respond_to_event,
    calendar_get_freebusy,
    calendar_get_current_time,
    calendar_list_colors,
]
