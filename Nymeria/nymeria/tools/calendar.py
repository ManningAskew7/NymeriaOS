"""Google Calendar tools.

Wraps the Google Calendar MCP server (https://github.com/nspady/google-calendar-mcp)
using JSON-RPC over stdio communication.

Optional tools — enable per-thread via thread config.
"""

import json
import logging
import os
import subprocess
from typing import Any, Dict, List, Optional

from langchain_core.tools import tool

logger = logging.getLogger(__name__)


def _call_mcp_tool(tool_name: str, arguments: Dict[str, Any]) -> str:
    """
    Call a tool on the Google Calendar MCP server.
    
    Uses JSON-RPC over stdio to communicate with the MCP server.
    
    Args:
        tool_name: Name of the MCP tool to call
        arguments: Arguments to pass to the tool
        
    Returns:
        Result string from the MCP server
    """
    # Check for credentials
    creds_path = os.environ.get("GOOGLE_OAUTH_CREDENTIALS")
    if not creds_path:
        return "[Error]: GOOGLE_OAUTH_CREDENTIALS environment variable not set. Please set it to the path of your Google OAuth credentials file."
    
    if not os.path.exists(creds_path):
        return f"[Error]: Google OAuth credentials file not found at: {creds_path}"
    
    # Build JSON-RPC request
    request = {
        "jsonrpc": "2.0",
        "id": 1,
        "method": "tools/call",
        "params": {
            "name": tool_name,
            "arguments": arguments
        }
    }
    
    try:
        # Start MCP server process
        process = subprocess.Popen(
            ["npx", "@cocal/google-calendar-mcp"],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            env={**os.environ, "GOOGLE_OAUTH_CREDENTIALS": creds_path}
        )
        
        # Send initialization
        init_request = {
            "jsonrpc": "2.0",
            "id": 0,
            "method": "initialize",
            "params": {
                "protocolVersion": "2024-11-05",
                "capabilities": {},
                "clientInfo": {"name": "nymeria", "version": "1.0.0"}
            }
        }
        
        # Send init and tool call
        stdin_data = json.dumps(init_request) + "\n" + json.dumps(request) + "\n"
        stdout, stderr = process.communicate(input=stdin_data, timeout=60)
        
        # Parse response - look for the tool call response (id: 1)
        for line in stdout.strip().split("\n"):
            if not line:
                continue
            try:
                response = json.loads(line)
                if response.get("id") == 1:
                    if "error" in response:
                        error = response["error"]
                        return f"[Error]: {error.get('message', 'Unknown error')}"
                    
                    result = response.get("result", {})
                    content = result.get("content", [])
                    
                    # Extract text content
                    texts = []
                    for item in content:
                        if item.get("type") == "text":
                            texts.append(item.get("text", ""))
                    
                    if texts:
                        return "[Success]:\n" + "\n".join(texts)
                    return "[Success]: Operation completed (no content returned)"
                    
            except json.JSONDecodeError:
                continue
        
        # If we didn't find a response, check stderr
        if stderr:
            logger.warning(f"MCP stderr: {stderr}")
        
        return "[Error]: No valid response from MCP server"
        
    except subprocess.TimeoutExpired:
        process.kill()
        return "[Error]: MCP server request timed out after 60 seconds"
    except FileNotFoundError:
        return "[Error]: npx not found. Please ensure Node.js and npm are installed."
    except Exception as e:
        logger.error(f"MCP call failed: {e}")
        return f"[Error]: {str(e)}"


@tool
def calendar_list_calendars() -> str:
    """
    List all available Google calendars for the authenticated account.
    
    Returns a list of calendars with their IDs, names, and access roles.
    Use this to discover which calendars are available before listing events.
    
    Returns:
        List of calendars with id, summary, and accessRole
    """
    logger.info("calendar_list_calendars called")
    return _call_mcp_tool("list-calendars", {})


@tool
def calendar_list_events(
    calendar_id: str = "primary",
    max_results: int = 10,
    time_min: Optional[str] = None,
    time_max: Optional[str] = None,
) -> str:
    """
    List events from a Google Calendar.
    
    Args:
        calendar_id: Calendar ID to list events from. Use "primary" for the main calendar.
        max_results: Maximum number of events to return (default: 10)
        time_min: Start time in ISO 8601 format (e.g., "2024-01-15T00:00:00Z")
        time_max: End time in ISO 8601 format (e.g., "2024-01-31T23:59:59Z")
    
    Returns:
        List of events with their details (id, summary, start, end, etc.)
    """
    logger.info(f"calendar_list_events called: calendar_id={calendar_id}, max_results={max_results}")
    
    args = {
        "calendarId": calendar_id,
        "maxResults": max_results,
    }
    if time_min:
        args["timeMin"] = time_min
    if time_max:
        args["timeMax"] = time_max
    
    return _call_mcp_tool("list-events", args)


@tool
def calendar_get_event(
    event_id: str,
    calendar_id: str = "primary",
) -> str:
    """
    Get detailed information about a specific calendar event.
    
    Args:
        event_id: The ID of the event to retrieve
        calendar_id: Calendar ID containing the event (default: "primary")
    
    Returns:
        Full event details including description, attendees, location, etc.
    """
    logger.info(f"calendar_get_event called: event_id={event_id}, calendar_id={calendar_id}")
    return _call_mcp_tool("get-event", {"eventId": event_id, "calendarId": calendar_id})


@tool
def calendar_search_events(
    query: str,
    calendar_id: str = "primary",
    max_results: int = 10,
) -> str:
    """
    Search for events by text query.
    
    Searches event titles, descriptions, locations, and attendee names.
    
    Args:
        query: Search query string
        calendar_id: Calendar ID to search in (default: "primary")
        max_results: Maximum number of results (default: 10)
    
    Returns:
        List of matching events
    """
    logger.info(f"calendar_search_events called: query={query}, calendar_id={calendar_id}")
    return _call_mcp_tool("search-events", {
        "q": query,
        "calendarId": calendar_id,
        "maxResults": max_results,
    })


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
) -> str:
    """
    Create a new calendar event.
    
    Args:
        summary: Event title/summary
        start_time: Start time in ISO 8601 format (e.g., "2024-01-15T10:00:00")
        end_time: End time in ISO 8601 format (e.g., "2024-01-15T11:00:00")
        calendar_id: Calendar ID to create event in (default: "primary")
        description: Optional event description
        location: Optional event location
        attendees: Optional comma-separated list of attendee email addresses
        timezone: Optional timezone (e.g., "America/New_York"). Defaults to calendar's timezone.
    
    Returns:
        Created event details including the event ID
    """
    logger.info(f"calendar_create_event called: summary={summary}, start={start_time}, end={end_time}")
    
    args = {
        "calendarId": calendar_id,
        "summary": summary,
        "start": start_time,
        "end": end_time,
    }
    
    if description:
        args["description"] = description
    if location:
        args["location"] = location
    if attendees:
        args["attendees"] = [email.strip() for email in attendees.split(",")]
    if timezone:
        args["timeZone"] = timezone
    
    return _call_mcp_tool("create-event", args)


@tool
def calendar_update_event(
    event_id: str,
    calendar_id: str = "primary",
    summary: Optional[str] = None,
    start_time: Optional[str] = None,
    end_time: Optional[str] = None,
    description: Optional[str] = None,
    location: Optional[str] = None,
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
    
    Returns:
        Updated event details
    """
    logger.info(f"calendar_update_event called: event_id={event_id}")
    
    args = {
        "eventId": event_id,
        "calendarId": calendar_id,
    }
    
    if summary:
        args["summary"] = summary
    if start_time:
        args["start"] = start_time
    if end_time:
        args["end"] = end_time
    if description:
        args["description"] = description
    if location:
        args["location"] = location
    
    return _call_mcp_tool("update-event", args)


@tool
def calendar_delete_event(
    event_id: str,
    calendar_id: str = "primary",
) -> str:
    """
    Delete a calendar event.
    
    Args:
        event_id: The ID of the event to delete
        calendar_id: Calendar ID containing the event (default: "primary")
    
    Returns:
        Confirmation of deletion
    """
    logger.info(f"calendar_delete_event called: event_id={event_id}, calendar_id={calendar_id}")
    return _call_mcp_tool("delete-event", {"eventId": event_id, "calendarId": calendar_id})


@tool
def calendar_respond_to_event(
    event_id: str,
    response: str,
    calendar_id: str = "primary",
) -> str:
    """
    Respond to a calendar event invitation.
    
    Args:
        event_id: The ID of the event to respond to
        response: Response status - one of: "accepted", "declined", "tentative"
        calendar_id: Calendar ID containing the event (default: "primary")
    
    Returns:
        Confirmation of response
    """
    logger.info(f"calendar_respond_to_event called: event_id={event_id}, response={response}")
    
    valid_responses = ["accepted", "declined", "tentative"]
    if response.lower() not in valid_responses:
        return f"[Error]: Invalid response. Must be one of: {', '.join(valid_responses)}"
    
    return _call_mcp_tool("respond-to-event", {
        "eventId": event_id,
        "calendarId": calendar_id,
        "response": response.lower(),
    })


@tool
def calendar_get_freebusy(
    time_min: str,
    time_max: str,
    calendars: Optional[str] = None,
) -> str:
    """
    Get free/busy information for calendars.
    
    Useful for finding available time slots for scheduling.
    
    Args:
        time_min: Start of time range in ISO 8601 format
        time_max: End of time range in ISO 8601 format
        calendars: Optional comma-separated list of calendar IDs. Defaults to primary calendar.
    
    Returns:
        Free/busy information showing busy time ranges
    """
    logger.info(f"calendar_get_freebusy called: time_min={time_min}, time_max={time_max}")
    
    args = {
        "timeMin": time_min,
        "timeMax": time_max,
    }
    
    if calendars:
        args["items"] = [{"id": cal.strip()} for cal in calendars.split(",")]
    
    return _call_mcp_tool("get-freebusy", args)


@tool
def calendar_get_current_time() -> str:
    """
    Get the current time from the MCP server.
    
    Useful for calculating relative times for event scheduling.
    
    Returns:
        Current time in ISO 8601 format
    """
    logger.info("calendar_get_current_time called")
    return _call_mcp_tool("get-current-time", {})


@tool
def calendar_list_colors() -> str:
    """
    List available calendar and event colors.
    
    Returns the color palette that can be used when creating or updating events.
    
    Returns:
        Available color IDs and their corresponding colors
    """
    logger.info("calendar_list_colors called")
    return _call_mcp_tool("list-colors", {})


@tool
def calendar_manage_accounts(action: str) -> str:
    """
    Manage Google account authentication.
    
    Args:
        action: One of:
            - "list": List authenticated accounts
            - "add": Add a new Google account (will provide auth URL)
            - "remove": Remove an authenticated account
    
    Returns:
        Account information or authentication instructions
    """
    logger.info(f"calendar_manage_accounts called: action={action}")
    
    valid_actions = ["list", "add", "remove"]
    if action.lower() not in valid_actions:
        return f"[Error]: Invalid action. Must be one of: {', '.join(valid_actions)}"
    
    return _call_mcp_tool("manage-accounts", {"action": action.lower()})


# Export the tools list
CALENDAR_TOOLS = [
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
    calendar_manage_accounts,
]
