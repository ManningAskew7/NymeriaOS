"""Google Business Profile native service tools."""

from __future__ import annotations

import json
import logging
from datetime import datetime
from typing import Annotated, Any, Optional

import httpx
from langchain_core.runnables import RunnableConfig
from langchain_core.tools import InjectedToolArg, tool

from . import auth_cache_utils as auth_utils
from .google_business_profile_auth import GOOGLE_BUSINESS_PROFILE_SCOPES, PROVIDER
from .utils import get_user_id

logger = logging.getLogger(__name__)

_PROFILE_BASE_URL = "https://mybusiness.googleapis.com/v4"
_ACCOUNT_MANAGEMENT_URL = "https://mybusinessaccountmanagement.googleapis.com/v1"
_BUSINESS_INFO_URL = "https://mybusinessbusinessinformation.googleapis.com/v1"
_DEFAULT_LOCATION_READ_MASK = (
    "name,title,storefrontAddress,phoneNumbers,websiteUri,regularHours,"
    "metadata,profile,openInfo,categories"
)
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


def _account_name(value: str) -> str:
    cleaned = value.strip().strip("/")
    if not cleaned:
        raise ValueError("account_name is required.")
    return cleaned if cleaned.startswith("accounts/") else f"accounts/{cleaned}"


def _location_name(value: str) -> str:
    cleaned = value.strip().strip("/")
    if not cleaned:
        raise ValueError("location_name is required.")
    if "/locations/" in cleaned:
        return f"locations/{cleaned.rsplit('/locations/', 1)[1]}"
    return cleaned if cleaned.startswith("locations/") else f"locations/{cleaned}"


def _full_name(value: str, *, account_name: str, location_name: str, collection: str, label: str) -> str:
    cleaned = value.strip().strip("/")
    if not cleaned:
        raise ValueError(f"{label} is required.")
    if cleaned.startswith("accounts/"):
        return cleaned
    account = _account_name(account_name)
    location = _location_name(location_name)
    if cleaned.startswith(f"{collection}/"):
        return f"{account}/{location}/{cleaned}"
    return f"{account}/{location}/{collection}/{cleaned}"


def _date_time_parts(value: str) -> tuple[dict[str, int], dict[str, int] | None]:
    parsed = datetime.fromisoformat(value.strip().replace("Z", "+00:00"))
    date = {"year": parsed.year, "month": parsed.month, "day": parsed.day}
    if "T" not in value:
        return date, None
    return date, {"hours": parsed.hour, "minutes": parsed.minute, "seconds": parsed.second, "nanos": 0}


def _set_nested(body: dict[str, Any], path: tuple[str, ...], value: Any) -> None:
    target = body
    for key in path[:-1]:
        next_value = target.get(key)
        if not isinstance(next_value, dict):
            next_value = {}
            target[key] = next_value
        target = next_value
    target[path[-1]] = value


def _apply_schedule(body: dict[str, Any], start_value: str, end_value: str) -> None:
    if not start_value.strip() and not end_value.strip():
        return
    schedule: dict[str, Any] = {}
    if start_value.strip():
        start_date, start_time = _date_time_parts(start_value)
        schedule["startDate"] = start_date
        if start_time:
            schedule["startTime"] = start_time
    if end_value.strip():
        end_date, end_time = _date_time_parts(end_value)
        schedule["endDate"] = end_date
        if end_time:
            schedule["endTime"] = end_time
    _set_nested(body, ("event", "schedule"), schedule)


def _local_post_body(
    *,
    topic_type: str,
    summary: str,
    language_code: str,
    call_to_action_type: str,
    call_to_action_url: str,
    title: str,
    start: str,
    end: str,
    alert_type: str,
    coupon_code: str,
    redeem_online_url: str,
    terms_conditions: str,
    fields_json: str,
    require_summary: bool,
) -> dict[str, Any]:
    body: dict[str, Any] = {}
    if topic_type.strip():
        body["topicType"] = topic_type.strip().upper()
    if summary.strip():
        body["summary"] = summary
    elif require_summary:
        raise ValueError("summary is required.")
    if language_code.strip():
        body["languageCode"] = language_code.strip()
    if call_to_action_type.strip():
        _set_nested(body, ("callToAction", "actionType"), call_to_action_type.strip().upper())
    if call_to_action_url.strip():
        _set_nested(body, ("callToAction", "url"), call_to_action_url.strip())
    if title.strip():
        _set_nested(body, ("event", "title"), title.strip())
    _apply_schedule(body, start, end)
    if alert_type.strip():
        body["alertType"] = alert_type.strip().upper()
    if coupon_code.strip():
        _set_nested(body, ("offer", "couponCode"), coupon_code.strip())
    if redeem_online_url.strip():
        _set_nested(body, ("offer", "redeemOnlineUrl"), redeem_online_url.strip())
    if terms_conditions.strip():
        _set_nested(body, ("offer", "termsConditions"), terms_conditions)
    body.update(_parse_json(fields_json, expected=dict, label="fields_json") if fields_json.strip() else {})
    return body


def _profile_request(
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
        GOOGLE_BUSINESS_PROFILE_SCOPES,
        account_id=account_id,
        provider_display_name="Google Business Profile",
    )
    if not creds or not getattr(creds, "token", None):
        return False, (
            "No authenticated Google Business Profile account. "
            "Use google_business_profile_auth_start to authenticate."
        )

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
        except Exception:
            pass
        return False, f"Google Business Profile API error ({status}): {message}"
    except Exception as e:
        logger.error("Google Business Profile request failed", exc_info=True)
        return False, f"Request failed: {e}"


@tool
def google_business_profile_list_profile_accounts(
    page_size: int = 20,
    page_token: str = "",
    account_id: Optional[str] = None,
    config: Annotated[RunnableConfig, InjectedToolArg] = None,
) -> str:
    """List Google Business Profile accounts visible to the authenticated account."""
    user_id = get_user_id(config)
    try:
        params: dict[str, Any] = {"pageSize": _limit(page_size, default=20, max_value=100)}
        if page_token.strip():
            params["pageToken"] = page_token.strip()
        success, result = _profile_request(
            user_id=user_id,
            method="GET",
            url=f"{_ACCOUNT_MANAGEMENT_URL}/accounts",
            params=params,
            account_id=account_id,
        )
        if not success:
            return f"[Error]: {result}"
        return _dump_json(result.get("accounts", result) if isinstance(result, dict) else result)
    except Exception as e:
        logger.error("google_business_profile_list_profile_accounts failed", exc_info=True)
        return f"[Error]: Google Business Profile account list failed: {e}"


@tool
def google_business_profile_list_locations(
    account_name: str,
    read_mask: str = _DEFAULT_LOCATION_READ_MASK,
    page_size: int = 100,
    page_token: str = "",
    account_id: Optional[str] = None,
    config: Annotated[RunnableConfig, InjectedToolArg] = None,
) -> str:
    """List business locations for a Google Business Profile account."""
    user_id = get_user_id(config)
    try:
        account = _account_name(account_name)
        params: dict[str, Any] = {
            "readMask": read_mask.strip() or _DEFAULT_LOCATION_READ_MASK,
            "pageSize": _limit(page_size, default=100, max_value=100),
        }
        if page_token.strip():
            params["pageToken"] = page_token.strip()
        success, result = _profile_request(
            user_id=user_id,
            method="GET",
            url=f"{_BUSINESS_INFO_URL}/{account}/locations",
            params=params,
            account_id=account_id,
        )
        if not success:
            return f"[Error]: {result}"
        return _dump_json(result.get("locations", result) if isinstance(result, dict) else result)
    except Exception as e:
        logger.error("google_business_profile_list_locations failed", exc_info=True)
        return f"[Error]: Google Business Profile location list failed: {e}"


@tool
def google_business_profile_list_reviews(
    account_name: str,
    location_name: str,
    page_size: int = 50,
    page_token: str = "",
    order_by: str = "",
    account_id: Optional[str] = None,
    config: Annotated[RunnableConfig, InjectedToolArg] = None,
) -> str:
    """List customer reviews for a Google Business Profile location."""
    user_id = get_user_id(config)
    try:
        account = _account_name(account_name)
        location = _location_name(location_name)
        params: dict[str, Any] = {"pageSize": _limit(page_size, default=50, max_value=50)}
        if page_token.strip():
            params["pageToken"] = page_token.strip()
        if order_by.strip():
            params["orderBy"] = order_by.strip()
        success, result = _profile_request(
            user_id=user_id,
            method="GET",
            url=f"{_PROFILE_BASE_URL}/{account}/{location}/reviews",
            params=params,
            account_id=account_id,
        )
        if not success:
            return f"[Error]: {result}"
        return _dump_json(result.get("reviews", result) if isinstance(result, dict) else result)
    except Exception as e:
        logger.error("google_business_profile_list_reviews failed", exc_info=True)
        return f"[Error]: Google Business Profile review list failed: {e}"


@tool
def google_business_profile_get_review(
    review_name: str,
    account_name: str = "",
    location_name: str = "",
    account_id: Optional[str] = None,
    config: Annotated[RunnableConfig, InjectedToolArg] = None,
) -> str:
    """Get one Google Business Profile review."""
    user_id = get_user_id(config)
    try:
        name = _full_name(
            review_name,
            account_name=account_name,
            location_name=location_name,
            collection="reviews",
            label="review_name",
        )
        success, result = _profile_request(
            user_id=user_id,
            method="GET",
            url=f"{_PROFILE_BASE_URL}/{name}",
            account_id=account_id,
        )
        return _dump_json(result) if success else f"[Error]: {result}"
    except Exception as e:
        logger.error("google_business_profile_get_review failed", exc_info=True)
        return f"[Error]: Google Business Profile review lookup failed: {e}"


@tool
def google_business_profile_reply_to_review(
    review_name: str,
    comment: str,
    account_name: str = "",
    location_name: str = "",
    account_id: Optional[str] = None,
    config: Annotated[RunnableConfig, InjectedToolArg] = None,
) -> str:
    """Reply to a Google Business Profile review."""
    user_id = get_user_id(config)
    if not comment.strip():
        return "[Error]: comment is required."
    try:
        name = _full_name(
            review_name,
            account_name=account_name,
            location_name=location_name,
            collection="reviews",
            label="review_name",
        )
        success, result = _profile_request(
            user_id=user_id,
            method="PUT",
            url=f"{_PROFILE_BASE_URL}/{name}/reply",
            json_body={"comment": comment},
            account_id=account_id,
        )
        return _dump_json(result) if success else f"[Error]: {result}"
    except Exception as e:
        logger.error("google_business_profile_reply_to_review failed", exc_info=True)
        return f"[Error]: Google Business Profile review reply failed: {e}"


@tool
def google_business_profile_delete_review_reply(
    review_name: str,
    account_name: str = "",
    location_name: str = "",
    account_id: Optional[str] = None,
    config: Annotated[RunnableConfig, InjectedToolArg] = None,
) -> str:
    """Delete the authenticated business reply on a Google Business Profile review."""
    user_id = get_user_id(config)
    try:
        name = _full_name(
            review_name,
            account_name=account_name,
            location_name=location_name,
            collection="reviews",
            label="review_name",
        )
        success, result = _profile_request(
            user_id=user_id,
            method="DELETE",
            url=f"{_PROFILE_BASE_URL}/{name}/reply",
            account_id=account_id,
        )
        return _dump_json({"status": "deleted", "review": name}) if success else f"[Error]: {result}"
    except Exception as e:
        logger.error("google_business_profile_delete_review_reply failed", exc_info=True)
        return f"[Error]: Google Business Profile review reply deletion failed: {e}"


@tool
def google_business_profile_list_posts(
    account_name: str,
    location_name: str,
    page_size: int = 100,
    page_token: str = "",
    account_id: Optional[str] = None,
    config: Annotated[RunnableConfig, InjectedToolArg] = None,
) -> str:
    """List local posts for a Google Business Profile location."""
    user_id = get_user_id(config)
    try:
        account = _account_name(account_name)
        location = _location_name(location_name)
        params: dict[str, Any] = {"pageSize": _limit(page_size, default=100, max_value=100)}
        if page_token.strip():
            params["pageToken"] = page_token.strip()
        success, result = _profile_request(
            user_id=user_id,
            method="GET",
            url=f"{_PROFILE_BASE_URL}/{account}/{location}/localPosts",
            params=params,
            account_id=account_id,
        )
        if not success:
            return f"[Error]: {result}"
        return _dump_json(result.get("localPosts", result) if isinstance(result, dict) else result)
    except Exception as e:
        logger.error("google_business_profile_list_posts failed", exc_info=True)
        return f"[Error]: Google Business Profile post list failed: {e}"


@tool
def google_business_profile_get_post(
    post_name: str,
    account_name: str = "",
    location_name: str = "",
    account_id: Optional[str] = None,
    config: Annotated[RunnableConfig, InjectedToolArg] = None,
) -> str:
    """Get one Google Business Profile local post."""
    user_id = get_user_id(config)
    try:
        name = _full_name(
            post_name,
            account_name=account_name,
            location_name=location_name,
            collection="localPosts",
            label="post_name",
        )
        success, result = _profile_request(
            user_id=user_id,
            method="GET",
            url=f"{_PROFILE_BASE_URL}/{name}",
            account_id=account_id,
        )
        return _dump_json(result) if success else f"[Error]: {result}"
    except Exception as e:
        logger.error("google_business_profile_get_post failed", exc_info=True)
        return f"[Error]: Google Business Profile post lookup failed: {e}"


@tool
def google_business_profile_create_post(
    account_name: str,
    location_name: str,
    summary: str,
    topic_type: str = "STANDARD",
    language_code: str = "",
    call_to_action_type: str = "",
    call_to_action_url: str = "",
    title: str = "",
    start: str = "",
    end: str = "",
    alert_type: str = "",
    coupon_code: str = "",
    redeem_online_url: str = "",
    terms_conditions: str = "",
    fields_json: str = "",
    account_id: Optional[str] = None,
    config: Annotated[RunnableConfig, InjectedToolArg] = None,
) -> str:
    """Create a Google Business Profile local post."""
    user_id = get_user_id(config)
    try:
        account = _account_name(account_name)
        location = _location_name(location_name)
        body = _local_post_body(
            topic_type=topic_type,
            summary=summary,
            language_code=language_code,
            call_to_action_type=call_to_action_type,
            call_to_action_url=call_to_action_url,
            title=title,
            start=start,
            end=end,
            alert_type=alert_type,
            coupon_code=coupon_code,
            redeem_online_url=redeem_online_url,
            terms_conditions=terms_conditions,
            fields_json=fields_json,
            require_summary=True,
        )
        success, result = _profile_request(
            user_id=user_id,
            method="POST",
            url=f"{_PROFILE_BASE_URL}/{account}/{location}/localPosts",
            json_body=body,
            account_id=account_id,
        )
        return _dump_json(result) if success else f"[Error]: {result}"
    except Exception as e:
        logger.error("google_business_profile_create_post failed", exc_info=True)
        return f"[Error]: Google Business Profile post creation failed: {e}"


@tool
def google_business_profile_update_post(
    post_name: str,
    summary: str = "",
    topic_type: str = "",
    language_code: str = "",
    call_to_action_type: str = "",
    call_to_action_url: str = "",
    title: str = "",
    start: str = "",
    end: str = "",
    alert_type: str = "",
    coupon_code: str = "",
    redeem_online_url: str = "",
    terms_conditions: str = "",
    fields_json: str = "",
    update_mask: str = "",
    account_name: str = "",
    location_name: str = "",
    account_id: Optional[str] = None,
    config: Annotated[RunnableConfig, InjectedToolArg] = None,
) -> str:
    """Update a Google Business Profile local post."""
    user_id = get_user_id(config)
    try:
        name = _full_name(
            post_name,
            account_name=account_name,
            location_name=location_name,
            collection="localPosts",
            label="post_name",
        )
        body = _local_post_body(
            topic_type=topic_type,
            summary=summary,
            language_code=language_code,
            call_to_action_type=call_to_action_type,
            call_to_action_url=call_to_action_url,
            title=title,
            start=start,
            end=end,
            alert_type=alert_type,
            coupon_code=coupon_code,
            redeem_online_url=redeem_online_url,
            terms_conditions=terms_conditions,
            fields_json=fields_json,
            require_summary=False,
        )
        if not body:
            return "[Error]: Provide fields to update or fields_json."
        params = {"updateMask": update_mask.strip() or ",".join(sorted(body))}
        success, result = _profile_request(
            user_id=user_id,
            method="PATCH",
            url=f"{_PROFILE_BASE_URL}/{name}",
            params=params,
            json_body=body,
            account_id=account_id,
        )
        return _dump_json(result) if success else f"[Error]: {result}"
    except Exception as e:
        logger.error("google_business_profile_update_post failed", exc_info=True)
        return f"[Error]: Google Business Profile post update failed: {e}"


@tool
def google_business_profile_delete_post(
    post_name: str,
    account_name: str = "",
    location_name: str = "",
    account_id: Optional[str] = None,
    config: Annotated[RunnableConfig, InjectedToolArg] = None,
) -> str:
    """Delete a Google Business Profile local post."""
    user_id = get_user_id(config)
    try:
        name = _full_name(
            post_name,
            account_name=account_name,
            location_name=location_name,
            collection="localPosts",
            label="post_name",
        )
        success, result = _profile_request(
            user_id=user_id,
            method="DELETE",
            url=f"{_PROFILE_BASE_URL}/{name}",
            account_id=account_id,
        )
        return _dump_json({"status": "deleted", "post": name}) if success else f"[Error]: {result}"
    except Exception as e:
        logger.error("google_business_profile_delete_post failed", exc_info=True)
        return f"[Error]: Google Business Profile post deletion failed: {e}"


GOOGLE_BUSINESS_PROFILE_SERVICE_TOOLS = [
    google_business_profile_list_profile_accounts,
    google_business_profile_list_locations,
    google_business_profile_list_reviews,
    google_business_profile_get_review,
    google_business_profile_reply_to_review,
    google_business_profile_delete_review_reply,
    google_business_profile_list_posts,
    google_business_profile_get_post,
    google_business_profile_create_post,
    google_business_profile_update_post,
    google_business_profile_delete_post,
]
