"""Shared CLIProxy URL detection helpers.

Both providers.py and nodes.py need to identify CLIProxy base URLs.
This module centralises the hostname/port heuristics so they stay in sync.
"""

from urllib.parse import urlparse

CLIPROXY_PORTS: frozenset[int] = frozenset({8317, 8318})
CLIPROXY_CLAUDE_USER_AGENT = "claude-cli/2.1.113"
CLIPROXY_REDACT_THINKING_BETA = "redact-thinking-2026-02-12"
CLIPROXY_ANTHROPIC_BETA_VALUES: tuple[str, ...] = (
    "claude-code-20250219",
    "oauth-2025-04-20",
    "interleaved-thinking-2025-05-14",
    "context-management-2025-06-27",
    "prompt-caching-scope-2026-01-05",
)
CLIPROXY_ANTHROPIC_BETA_HEADER = ",".join(CLIPROXY_ANTHROPIC_BETA_VALUES)

# cc_version is a MODEL GATE, not just a fingerprint: upstream refuses newer
# models to older advertised Claude Code versions with a 400 ("Claude Code
# 2.1.63 does not support this model; version 2.1.280 or newer is required",
# claude-opus-5-5, 2026-09-23). The User-Agent is not what it reads. Bump this
# to the version such an error names; the cch value is re-signed by the proxy.
# Mirrored in tools/check_cliproxy_cloak.py and nymeria_codehints/blurb.py.
CLIPROXY_BILLING_SYSTEM_BLOCK: dict[str, str] = {
    "type": "text",
    "text": "x-anthropic-billing-header: cc_version=2.1.280.8f3; cc_entrypoint=cli; cch=54031;",
}

# Anthropic prompt-cache breakpoint marker. Single source of truth for both the
# message-sanitization path (nodes.py) and the tool-payload injection path
# (providers.py); copy it with dict(...) at each use site before mutating.
CACHE_CONTROL_EPHEMERAL: dict[str, str] = {"type": "ephemeral"}


def looks_like_cliproxy_gatekeeper_key(key: str) -> bool:
    """Return True when *key* has the shape of a CLIProxy gatekeeper key.

    Every gatekeeper Nymeria mints or documents is ``cpx-`` prefixed
    (``management_client``), and no vendor issues keys in that shape, so a
    caller about to send a key to a real vendor endpoint can refuse it here.
    """
    return key.strip().startswith("cpx-")


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
