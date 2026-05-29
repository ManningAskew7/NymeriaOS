"""HTTP Poll trigger source -- fires when a URL's response changes or matches a condition.

Polls any URL at each ticker cycle and compares the response against
the previous state. Supports four trigger modes:
  - change:      fires when the response body hash differs from the last poll
  - status_code: fires when the HTTP status differs from expected
  - contains:    fires when the response contains a specific string
  - always:      fires on every successful poll (useful for data collection)
"""

import hashlib
import logging
from typing import Any, Dict, List

from . import register_source
from .base import BaseTriggerSource
from ...core.time_utils import utc_now

logger = logging.getLogger(__name__)


class HTTPPollSource(BaseTriggerSource):
    """Generic URL polling source with change detection."""

    name = "http_poll"
    description = "Polls a URL and fires when the response changes or matches a condition"
    category = "monitoring"
    icon = "refresh"
    setup_guide = (
        "Monitor any URL for changes or specific conditions.\n\n"
        "**Use cases:**\n"
        "- Detect when a website updates (fire on **change**)\n"
        "- Alert when an API returns an error status (fire on **status_code**)\n"
        "- Watch for specific text appearing on a page (fire on **contains**)\n"
        "- Collect data at regular intervals (fire **always**)\n\n"
        "The source makes an HTTP request every ~30 seconds."
    )
    template_variables = ["url", "status_code", "response_body", "changed_at", "previous_hash"]
    example_config = {
        "url": "https://api.example.com/status",
        "fire_on": "change",
    }

    config_schema: Dict[str, Any] = {
        "url": {
            "type": "string",
            "description": "URL to poll",
            "required": True,
            "placeholder": "https://api.example.com/status",
            "order": 1,
        },
        "method": {
            "type": "string",
            "description": "HTTP method",
            "required": False,
            "default": "GET",
            "enum": ["GET", "POST", "HEAD"],
            "order": 2,
        },
        "headers": {
            "type": "object",
            "description": "Custom request headers (JSON object)",
            "required": False,
            "order": 3,
            "group": "Advanced",
        },
        "fire_on": {
            "type": "string",
            "description": "When to fire the trigger",
            "required": False,
            "default": "change",
            "enum": ["change", "status_code", "contains", "always"],
            "order": 4,
            "group": "Trigger Condition",
        },
        "expected_status": {
            "type": "integer",
            "description": "Expected HTTP status code (fires when status DIFFERS from this)",
            "required": False,
            "default": 200,
            "order": 5,
            "group": "Trigger Condition",
        },
        "contains_text": {
            "type": "string",
            "description": "Text to search for in the response body",
            "required": False,
            "placeholder": "error",
            "order": 6,
            "group": "Trigger Condition",
        },
    }

    def check(self, config: dict, state: dict, user_id: str = "") -> List[dict]:
        url = config["url"]
        method = config.get("method", "GET")
        custom_headers = config.get("headers") or {}

        from ...core.http_policy import (
            HTTPPolicyRedirectLimit,
            HTTPPolicyViolation,
            httpx_request_with_policy,
        )

        try:
            resp, _redirect_chain, _policy = httpx_request_with_policy(
                method,
                url,
                headers=custom_headers,
                timeout=15,
                follow_redirects=True,
            )
            body = resp.text
            status = resp.status_code
        except (HTTPPolicyViolation, HTTPPolicyRedirectLimit) as e:
            logger.error(f"http_poll source: blocked URL {url}: {e}")
            raise
        except Exception as e:
            logger.error(f"http_poll source: request to {url} failed: {e}")
            raise

        body_hash = hashlib.sha256(body.encode()).hexdigest()[:16]
        previous_hash = state.get("response_hash")

        fire_on = config.get("fire_on", "change")
        should_fire = False

        if fire_on == "change":
            if previous_hash is not None and body_hash != previous_hash:
                should_fire = True
        elif fire_on == "status_code":
            expected = config.get("expected_status", 200)
            if status != expected:
                should_fire = True
        elif fire_on == "contains":
            search_text = config.get("contains_text", "")
            if search_text and search_text in body:
                should_fire = True
        elif fire_on == "always":
            should_fire = True

        state["response_hash"] = body_hash
        state["last_status"] = status

        if should_fire:
            return [{
                "url": url,
                "status_code": status,
                "response_body": body[:2000],
                "changed_at": utc_now().isoformat(),
                "previous_hash": previous_hash or "",
            }]
        return []

    def get_sample_event(self, config: dict) -> dict:
        return {
            "url": config.get("url", "https://api.example.com/status"),
            "status_code": 200,
            "response_body": '{"status": "ok", "version": "2.1.0"}',
            "changed_at": utc_now().isoformat(),
            "previous_hash": "a1b2c3d4e5f6g7h8",
        }

    def validate_config(self, config: dict) -> tuple:
        ok, msg = super().validate_config(config)
        if not ok:
            return ok, msg
        url = config.get("url", "")
        if url and not url.startswith(("http://", "https://")):
            return False, "URL must start with http:// or https://"
        if url:
            from ...core.http_policy import validate_http_egress_url

            try:
                # Structural egress check at create time (rejects literal
                # private/loopback/metadata targets and bad schemes without a
                # network call). Full DNS-pinned enforcement runs at request
                # time in check() via httpx_request_with_policy.
                validate_http_egress_url(url, label="Poll URL", resolve_dns=False)
            except ValueError as e:
                return False, str(e)
        return True, "ok"


register_source("http_poll", HTTPPollSource)
