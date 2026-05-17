"""Google Analytics native service tools."""

from __future__ import annotations

import json
import logging
from typing import Annotated, Any, Optional

import httpx
from langchain_core.runnables import RunnableConfig
from langchain_core.tools import InjectedToolArg, tool

from . import auth_cache_utils as auth_utils
from .google_analytics_auth import GOOGLE_ANALYTICS_SCOPES, PROVIDER
from .utils import get_user_id

logger = logging.getLogger(__name__)

_ANALYTICS_DATA_URL = "https://analyticsdata.googleapis.com"
_ANALYTICS_ADMIN_URL = "https://analyticsadmin.googleapis.com"
_MAX_JSON_CHARS = 80_000


def _dump_json(data: Any, *, max_chars: int = _MAX_JSON_CHARS) -> str:
    text = json.dumps(data, indent=2, ensure_ascii=False, default=str)
    if len(text) <= max_chars:
        return text
    return text[:max_chars] + f"\n...[truncated {len(text) - max_chars} chars]"


def _parse_json(value: str, *, expected: type, label: str) -> Any:
    if not value.strip():
        return {} if expected is dict else []
    try:
        parsed = json.loads(value)
    except json.JSONDecodeError as e:
        raise ValueError(f"{label} must be valid JSON: {e}") from e
    if not isinstance(parsed, expected):
        raise ValueError(f"{label} must be a JSON {expected.__name__}.")
    return parsed


def _limit(value: int, *, default: int = 50, max_value: int = 1000) -> int:
    try:
        return max(1, min(max_value, int(value)))
    except Exception:
        return default


def _property_id(value: str) -> str:
    cleaned = value.strip().removeprefix("properties/").strip("/")
    if not cleaned:
        raise ValueError("property_id is required.")
    return cleaned


def _csv_names(value: str, *, label: str) -> list[dict[str, str]]:
    names = [part.strip() for part in value.split(",") if part.strip()]
    if not names:
        raise ValueError(f"{label} is required.")
    return [{"name": name} for name in names]


def _analytics_request(
    *,
    user_id: str,
    method: str,
    url: str,
    account_id: Optional[str],
    params: Optional[dict[str, Any]] = None,
    json_body: Optional[dict[str, Any]] = None,
) -> tuple[bool, Any]:
    creds = auth_utils.get_google_credentials(
        user_id,
        PROVIDER,
        GOOGLE_ANALYTICS_SCOPES,
        account_id=account_id,
        provider_display_name="Google Analytics",
    )
    if not creds or not getattr(creds, "token", None):
        return False, "No authenticated Google Analytics account. Use google_analytics_auth_start to authenticate."

    headers = {
        "Accept": "application/json",
        "Authorization": f"Bearer {creds.token}",
    }
    if json_body is not None:
        headers["Content-Type"] = "application/json"

    try:
        with httpx.Client(timeout=30.0) as client:
            response = client.request(method, url, headers=headers, params=params, json=json_body)
            response.raise_for_status()
            if not response.content:
                return True, {}
            return True, response.json()
    except httpx.HTTPStatusError as e:
        status = e.response.status_code
        message = e.response.text
        try:
            payload = e.response.json()
            message = payload.get("error", {}).get("message", message)
        except Exception:  # noqa: BLE001
            logger.debug("Failed to parse Google Analytics error response", exc_info=True)
        return False, f"Google Analytics API error ({status}): {message}"
    except Exception as e:
        logger.error("Google Analytics request failed", exc_info=True)
        return False, f"Request failed: {e}"


@tool
def google_analytics_list_account_summaries(
    page_size: int = 50,
    page_token: str = "",
    account_id: Optional[str] = None,
    config: Annotated[RunnableConfig, InjectedToolArg] = None,
) -> str:
    """List Google Analytics accounts and GA4 properties visible to the account."""
    user_id = get_user_id(config)
    try:
        params: dict[str, Any] = {"pageSize": _limit(page_size, default=50, max_value=200)}
        if page_token.strip():
            params["pageToken"] = page_token.strip()
        success, result = _analytics_request(
            user_id=user_id,
            method="GET",
            url=f"{_ANALYTICS_ADMIN_URL}/v1beta/accountSummaries",
            params=params,
            account_id=account_id,
        )
        if not success:
            return f"[Error]: {result}"
        return _dump_json(result.get("accountSummaries", result) if isinstance(result, dict) else result)
    except Exception as e:
        logger.error("google_analytics_list_account_summaries failed", exc_info=True)
        return f"[Error]: Google Analytics account summary list failed: {e}"


@tool
def google_analytics_get_metadata(
    property_id: str,
    account_id: Optional[str] = None,
    config: Annotated[RunnableConfig, InjectedToolArg] = None,
) -> str:
    """Get available dimensions and metrics for a GA4 property."""
    user_id = get_user_id(config)
    try:
        prop = _property_id(property_id)
        success, result = _analytics_request(
            user_id=user_id,
            method="GET",
            url=f"{_ANALYTICS_DATA_URL}/v1beta/properties/{prop}/metadata",
            account_id=account_id,
        )
        return _dump_json(result) if success else f"[Error]: {result}"
    except Exception as e:
        logger.error("google_analytics_get_metadata failed", exc_info=True)
        return f"[Error]: Google Analytics metadata lookup failed: {e}"


@tool
def google_analytics_run_report(
    property_id: str,
    metrics: str = "activeUsers",
    dimensions: str = "date",
    start_date: str = "7daysAgo",
    end_date: str = "today",
    limit: int = 100,
    offset: int = 0,
    order_bys_json: str = "",
    filters_json: str = "",
    keep_empty_rows: bool = False,
    account_id: Optional[str] = None,
    config: Annotated[RunnableConfig, InjectedToolArg] = None,
) -> str:
    """Run a GA4 report for selected metrics, dimensions, and date range."""
    user_id = get_user_id(config)
    try:
        prop = _property_id(property_id)
        body: dict[str, Any] = {
            "dateRanges": [{"startDate": start_date.strip() or "7daysAgo", "endDate": end_date.strip() or "today"}],
            "metrics": _csv_names(metrics, label="metrics"),
            "dimensions": _csv_names(dimensions, label="dimensions"),
            "limit": str(_limit(limit, default=100, max_value=100000)),
            "offset": str(max(0, int(offset))),
            "keepEmptyRows": bool(keep_empty_rows),
        }
        if order_bys_json.strip():
            body["orderBys"] = _parse_json(order_bys_json, expected=list, label="order_bys_json")
        if filters_json.strip():
            body.update(_parse_json(filters_json, expected=dict, label="filters_json"))
        success, result = _analytics_request(
            user_id=user_id,
            method="POST",
            url=f"{_ANALYTICS_DATA_URL}/v1beta/properties/{prop}:runReport",
            json_body=body,
            account_id=account_id,
        )
        return _dump_json(result) if success else f"[Error]: {result}"
    except Exception as e:
        logger.error("google_analytics_run_report failed", exc_info=True)
        return f"[Error]: Google Analytics report failed: {e}"


@tool
def google_analytics_run_realtime_report(
    property_id: str,
    metrics: str = "activeUsers",
    dimensions: str = "unifiedScreenName",
    limit: int = 100,
    order_bys_json: str = "",
    filters_json: str = "",
    account_id: Optional[str] = None,
    config: Annotated[RunnableConfig, InjectedToolArg] = None,
) -> str:
    """Run a GA4 realtime report for recent activity."""
    user_id = get_user_id(config)
    try:
        prop = _property_id(property_id)
        body: dict[str, Any] = {
            "metrics": _csv_names(metrics, label="metrics"),
            "dimensions": _csv_names(dimensions, label="dimensions"),
            "limit": str(_limit(limit, default=100, max_value=100000)),
        }
        if order_bys_json.strip():
            body["orderBys"] = _parse_json(order_bys_json, expected=list, label="order_bys_json")
        if filters_json.strip():
            body.update(_parse_json(filters_json, expected=dict, label="filters_json"))
        success, result = _analytics_request(
            user_id=user_id,
            method="POST",
            url=f"{_ANALYTICS_DATA_URL}/v1beta/properties/{prop}:runRealtimeReport",
            json_body=body,
            account_id=account_id,
        )
        return _dump_json(result) if success else f"[Error]: {result}"
    except Exception as e:
        logger.error("google_analytics_run_realtime_report failed", exc_info=True)
        return f"[Error]: Google Analytics realtime report failed: {e}"


GOOGLE_ANALYTICS_SERVICE_TOOLS = [
    google_analytics_list_account_summaries,
    google_analytics_get_metadata,
    google_analytics_run_report,
    google_analytics_run_realtime_report,
]
