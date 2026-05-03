"""Shared CLIProxy URL detection helpers.

Both providers.py and nodes.py need to identify CLIProxy base URLs.
This module centralises the hostname/port heuristics so they stay in sync.
"""

from urllib.parse import urlparse

CLIPROXY_PORTS: frozenset[int] = frozenset({8317, 8318})

CLIPROXY_BILLING_SYSTEM_BLOCK: dict[str, str] = {
    "type": "text",
    "text": "x-anthropic-billing-header: cc_version=2.1.63.8f3; cc_entrypoint=cli; cch=54031;",
}


def looks_like_cliproxy_url(base_url: str) -> bool:
    """Return True when *base_url* points at a CLIProxy instance.

    Detection uses hostname substrings (``cli-proxy``, ``cliproxy``) and
    the well-known local ports CLIProxy binds to.
    """
    parse_target = base_url.strip()
    if "://" not in parse_target:
        parse_target = f"http://{parse_target}"

    try:
        parsed = urlparse(parse_target)
    except ValueError:
        return False

    host = (parsed.hostname or "").lower()
    try:
        port = parsed.port
    except ValueError:
        port = None

    if not host:
        return False

    return "cli-proxy" in host or "cliproxy" in host or port in CLIPROXY_PORTS
