"""SearXNG backend health: the engine-failure report and a canary search.

Shared by ``web_search_searxng`` (``tools/web_search_integrations.py``), which
uses the parsing helpers on every page it gets, and the ``nymeria doctor``
SearXNG row (``doctor.py``), which runs :func:`probe_searxng`. It lives in
``core/`` and imports only the standard library at module scope (httpx and the
policy client load inside the probe) because doctor must not load the tools
package: importing it costs about 7 seconds (measured 2026-10-02).

What SearXNG reports about failure (read from, then measured against, the
pinned sidecar image on 2026-10-02):

- A search whose every upstream engine failed is still HTTP 200 with
  ``"results": []``. The failures ride in ``unresponsive_engines`` as
  ``[engine_name, reason]`` pairs, sorted by engine name, with reasons from a
  fixed vocabulary (``timeout``, ``CAPTCHA``, ``too many requests``,
  ``access denied``, ``HTTP connection error``, ...); an engine suspended after
  repeated failures carries a ``Suspended: `` prefix.
- ``/healthz`` answers ``OK`` on such an instance, so only a real search tells
  a dead backend from a healthy one.
- An engine that cannot honour a filter (``time_range``, say) is skipped
  silently, not listed, so zero results with an empty list is a genuine empty.
- A format missing from the instance's ``search.formats`` is a 403; the bot
  limiter (``server.limiter``) answers 429.

The engine list is instance-supplied text on its way into a transcript, so
:func:`unresponsive_engines` keeps only pairs of two strings, collapses
whitespace, drops control and format characters (escape sequences, bidi
overrides), and caps each name at 40 characters and each reason at 60;
:func:`describe_engines` names at most eight.

Address screening: the probe is handed the operator's ``SEARXNG_BASE_URL``
only (doctor never reads a per-user saved address) and does not run it
through the egress policy, for the reason ``_get_searxng_base_url`` and the
credential tester give: the sidecar is a private host by design. The client
still comes from ``policy_http_client``, so an env proxy never carries the
internal address.
"""

from __future__ import annotations

import unicodedata
from dataclasses import dataclass
from typing import Any, Literal, Sequence
from urllib.parse import urlsplit

CANARY_QUERY = "wikipedia"
MAX_NAMED_ENGINES = 8
PROBE_CONNECT_TIMEOUT_SECONDS = 3.0
PROBE_TIMEOUT_SECONDS = 10.0

_NAME_CAP = 40
_REASON_CAP = 60
# Bound the work per entry before cleaning: a hostile instance can send any
# length, and only the first few dozen characters survive the cap anyway.
_RAW_SLICE = 512
_REASON_TEXT_CAP = 200

ProbeOutcome = Literal["unreachable", "timeout", "http_error", "not_json", "answered"]


@dataclass(frozen=True)
class SearxngProbe:
    """What one canary search found.

    ``outcome`` is ``unreachable`` (no connection), ``timeout`` (connected, no
    answer in time), ``http_error`` (``status_code`` >= 400), ``not_json`` (a
    2xx that is not a JSON object), or ``answered`` (a SearXNG page:
    ``result_count`` results, ``failed_engines`` sanitised). ``reason`` is the
    transport error text for the first two, with the address scrubbed out.
    """

    outcome: ProbeOutcome
    status_code: int | None = None
    result_count: int = 0
    failed_engines: tuple[tuple[str, str], ...] = ()
    reason: str = ""


def _clean(text: str, cap: int) -> str:
    spaced = (" " if ch.isspace() else ch for ch in text[:_RAW_SLICE])
    kept = "".join(ch for ch in spaced if not unicodedata.category(ch).startswith("C"))
    flat = " ".join(kept.split())
    if len(flat) > cap:
        return flat[:cap].rstrip() + "..."
    return flat


def unresponsive_engines(page: Any) -> list[tuple[str, str]]:
    """The sanitised ``(engine, reason)`` pairs a SearXNG JSON page reports.

    SearXNG's order is kept. Anything that is not a two-string pair is ignored,
    as is a pair whose name is blank once cleaned; an exact duplicate is kept
    once. A missing or malformed field gives ``[]``, which callers treat as "no
    engine reported an error".
    """
    if not isinstance(page, dict):
        return []
    raw = page.get("unresponsive_engines")
    if not isinstance(raw, (list, tuple)):
        return []
    engines: list[tuple[str, str]] = []
    seen: set[tuple[str, str]] = set()
    for entry in raw:
        if not isinstance(entry, (list, tuple)) or len(entry) != 2:
            continue
        name, reason = entry
        if not isinstance(name, str) or not isinstance(reason, str):
            continue
        pair = (_clean(name, _NAME_CAP), _clean(reason, _REASON_CAP))
        if pair[0] and pair not in seen:
            seen.add(pair)
            engines.append(pair)
    return engines


def describe_engines(
    engines: Sequence[tuple[str, str]], *, limit: int = MAX_NAMED_ENGINES
) -> str:
    """``"brave (too many requests), google (CAPTCHA), and 3 more"``.

    At most ``limit`` engines are named, in the order given; an empty reason
    renders as the bare name.
    """
    named = [f"{name} ({reason})" if reason else name for name, reason in engines[:limit]]
    hidden = len(engines) - len(named)
    if hidden > 0:
        named.append(f"and {hidden} more")
    return ", ".join(named)


def scrub_address(text: str, base_url: str) -> str:
    """``text`` with the base URL and its host part replaced by a placeholder.

    Transport errors name an errno, not the address, today; this keeps a future
    library message from carrying an internal address or its credentials into
    a transcript or a doctor row.
    """
    stripped = (base_url or "").strip()
    needles = {stripped, stripped.rstrip("/")}
    try:
        needles.add(urlsplit(stripped).netloc)
    except ValueError:
        pass
    for needle in sorted((n for n in needles if n), key=len, reverse=True):
        text = text.replace(needle, "<SearXNG address>")
    return text


def transport_reason(exc: BaseException, base_url: str) -> str:
    """One line describing a transport failure, address scrubbed, capped."""
    text = " ".join((str(exc) or exc.__class__.__name__).split())
    text = scrub_address(text, base_url)
    if len(text) > _REASON_TEXT_CAP:
        text = text[:_REASON_TEXT_CAP].rstrip() + "..."
    return text


def probe_searxng(base_url: str, *, query: str = CANARY_QUERY) -> SearxngProbe:
    """Run one canary search against ``base_url`` and report what came back.

    The same request shape as the tool (``format=json``, ``categories=general``,
    ``safesearch=1``, ``pageno=1``), with a 3 second connect timeout and 10
    seconds per read. Never raises: every failure is an outcome.
    """
    import httpx

    from .http_policy import policy_http_client

    url = f"{(base_url or '').strip().rstrip('/')}/search"
    params = {
        "q": query,
        "format": "json",
        "categories": "general",
        "safesearch": 1,
        "pageno": 1,
    }
    timeout = httpx.Timeout(PROBE_TIMEOUT_SECONDS, connect=PROBE_CONNECT_TIMEOUT_SECONDS)
    try:
        with policy_http_client(timeout=timeout, follow_redirects=True) as client:
            response = client.get(
                url, params=params, headers={"Accept": "application/json"}
            )
    except (httpx.ConnectError, httpx.ConnectTimeout) as exc:
        return SearxngProbe("unreachable", reason=transport_reason(exc, base_url))
    except httpx.TimeoutException as exc:
        return SearxngProbe("timeout", reason=transport_reason(exc, base_url))
    except Exception as exc:  # noqa: BLE001 - a bad URL or protocol error is a finding, not a crash
        return SearxngProbe("unreachable", reason=transport_reason(exc, base_url))

    if response.status_code >= 400:
        return SearxngProbe("http_error", status_code=response.status_code)
    try:
        page = response.json()
    except ValueError:
        page = None
    if not isinstance(page, dict):
        return SearxngProbe("not_json", status_code=response.status_code)
    results = page.get("results")
    count = (
        sum(1 for item in results if isinstance(item, dict))
        if isinstance(results, list)
        else 0
    )
    return SearxngProbe(
        "answered",
        status_code=response.status_code,
        result_count=count,
        failed_engines=tuple(unresponsive_engines(page)),
    )


__all__ = [
    "CANARY_QUERY",
    "MAX_NAMED_ENGINES",
    "PROBE_CONNECT_TIMEOUT_SECONDS",
    "PROBE_TIMEOUT_SECONDS",
    "SearxngProbe",
    "describe_engines",
    "probe_searxng",
    "scrub_address",
    "transport_reason",
    "unresponsive_engines",
]
