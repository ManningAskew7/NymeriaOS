"""HTTP egress policy and audit helpers.

This module is intentionally independent from LangChain tool wrappers so the
same network policy can protect ad-hoc HTTP tools and saved custom HTTP tools.
"""

from __future__ import annotations

import ipaddress
import json
import logging
import re
import socket
import threading
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any, Callable, Iterable, Optional
from urllib.parse import urlparse

logger = logging.getLogger(__name__)

METADATA_HOSTNAMES = {
    "metadata.google.internal",
    "metadata.azure.com",
}

METADATA_IPS = {
    "169.254.169.254",
    "100.100.100.200",
}

SECRET_PATTERNS = [
    re.compile(r"sk_live_[A-Za-z0-9_\-]+"),
    re.compile(r"sk-[A-Za-z0-9_\-]{16,}"),
    re.compile(r"ghp_[A-Za-z0-9_]+"),
    re.compile(r"github_pat_[A-Za-z0-9_]+"),
    re.compile(r"xox[baprs]-[A-Za-z0-9\-]+"),
    re.compile(r"AKIA[0-9A-Z]{16}"),
    re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----.*?-----END [A-Z ]*PRIVATE KEY-----", re.S),
    re.compile(r"(?i)(authorization\s*:\s*bearer\s+)[^\s,;]+"),
    re.compile(r"(?i)(bearer\s+)[A-Za-z0-9._\-~+/]+=*"),
]

SENSITIVE_HEADER_NAMES = {
    "authorization",
    "proxy-authorization",
    "x-api-key",
    "api-key",
    "cookie",
    "set-cookie",
}


@dataclass(frozen=True)
class HTTPPolicyConfig:
    """Server-side HTTP egress policy settings."""

    internal_allowlist: tuple[str, ...] = ()
    domain_allowlist: tuple[str, ...] = ()
    domain_blocklist: tuple[str, ...] = ()
    max_redirects: int = 5
    allow_https_to_http_redirect: bool = False
    resolve_dns: bool = True


@dataclass(frozen=True)
class HTTPPolicyDecision:
    """Result of evaluating one URL against the HTTP egress policy."""

    allowed: bool
    reason: str
    target: str
    host: Optional[str] = None
    port: Optional[int] = None
    resolved_ips: tuple[str, ...] = ()
    matched_rule: Optional[str] = None

    def to_dict(self) -> dict[str, Any]:
        data: dict[str, Any] = {
            "decision": "allowed" if self.allowed else "blocked",
            "reason": self.reason,
            "target": self.target,
        }
        if self.host:
            data["host"] = self.host
        if self.port is not None:
            data["port"] = self.port
        if self.resolved_ips:
            data["resolved_ips"] = list(self.resolved_ips)
        if self.matched_rule:
            data["matched_rule"] = self.matched_rule
        return data


Resolver = Callable[[str, int], Iterable[str]]


def _split_csv(value: Optional[str]) -> tuple[str, ...]:
    if not value:
        return ()
    return tuple(item.strip() for item in re.split(r"[,;\n]", value) if item.strip())


def load_http_policy_config(settings: Optional[Any] = None) -> HTTPPolicyConfig:
    """Load HTTP policy settings from Nymeria settings."""
    if settings is None:
        from ..config import get_settings

        settings = get_settings()

    return HTTPPolicyConfig(
        internal_allowlist=_split_csv(getattr(settings, "http_internal_allowlist", "")),
        domain_allowlist=_split_csv(getattr(settings, "http_domain_allowlist", "")),
        domain_blocklist=_split_csv(getattr(settings, "http_domain_blocklist", "")),
        max_redirects=max(0, min(20, int(getattr(settings, "http_max_redirects", 5) or 5))),
        allow_https_to_http_redirect=bool(
            getattr(settings, "http_allow_https_to_http_redirect", False)
        ),
    )


def _default_port(scheme: str) -> int:
    return 443 if scheme == "https" else 80


def _normalize_host(host: str) -> str:
    return host.strip("[]").rstrip(".").lower()


def _normalize_allowlist_entry(entry: str) -> str:
    value = entry.strip().lower()
    if "://" in value:
        parsed = urlparse(value)
        value = parsed.netloc or parsed.path
    return value.strip("/").strip("[]").rstrip(".")


def _domain_matches(pattern: str, host: str) -> bool:
    pattern = _normalize_allowlist_entry(pattern)
    host = _normalize_host(host)
    if not pattern:
        return False
    if pattern.startswith("*."):
        suffix = pattern[1:]
        return host.endswith(suffix) and host != pattern[2:]
    if pattern.startswith("."):
        return host.endswith(pattern)
    return host == pattern


def _internal_allowlist_matches(config: HTTPPolicyConfig, host: str, port: int) -> Optional[str]:
    host = _normalize_host(host)
    host_port = f"{host}:{port}"
    for raw_entry in config.internal_allowlist:
        entry = _normalize_allowlist_entry(raw_entry)
        if not entry:
            continue
        if entry == host or entry == host_port:
            return raw_entry
    return None


def _resolve_host(host: str, port: int) -> tuple[str, ...]:
    addresses: set[str] = set()
    for family, _socktype, _proto, _canonname, sockaddr in socket.getaddrinfo(
        host,
        port,
        type=socket.SOCK_STREAM,
    ):
        if family in {socket.AF_INET, socket.AF_INET6}:
            addresses.add(str(sockaddr[0]))
    return tuple(sorted(addresses))


def _is_blocked_ip(ip_text: str) -> tuple[bool, str]:
    if ip_text in METADATA_IPS:
        return True, "metadata_target"

    ip = ipaddress.ip_address(ip_text)
    if ip.is_loopback:
        return True, "loopback_network"
    if ip.is_link_local:
        return True, "link_local_network"
    if ip.is_private:
        return True, "private_network"
    if ip.is_multicast:
        return True, "multicast_network"
    if ip.is_unspecified:
        return True, "unspecified_network"
    if ip.is_reserved:
        return True, "reserved_network"
    return False, "public_network"


def evaluate_http_url(
    url: str,
    config: Optional[HTTPPolicyConfig] = None,
    resolver: Optional[Resolver] = None,
) -> HTTPPolicyDecision:
    """Evaluate whether an HTTP URL can be requested."""
    config = config or load_http_policy_config()
    parsed = urlparse(url)
    if parsed.scheme not in {"http", "https"} or not parsed.netloc:
        return HTTPPolicyDecision(False, "invalid_http_url", url)

    host = _normalize_host(parsed.hostname or "")
    if not host:
        return HTTPPolicyDecision(False, "invalid_http_url", url)
    port = parsed.port or _default_port(parsed.scheme)

    if host in METADATA_HOSTNAMES:
        return HTTPPolicyDecision(False, "metadata_target", url, host, port)

    for pattern in config.domain_blocklist:
        if _domain_matches(pattern, host):
            return HTTPPolicyDecision(
                False,
                "domain_blocklist",
                url,
                host,
                port,
                matched_rule=pattern,
            )

    internal_match = _internal_allowlist_matches(config, host, port)

    try:
        ipaddress.ip_address(host)
        literal_ips = (host,)
    except ValueError:
        literal_ips = ()

    if literal_ips:
        blocked, reason = _is_blocked_ip(host)
        if blocked and not internal_match:
            return HTTPPolicyDecision(False, reason, url, host, port, literal_ips)
        return HTTPPolicyDecision(
            True,
            "internal_allowlist" if blocked else "public_network",
            url,
            host,
            port,
            literal_ips,
            matched_rule=internal_match,
        )

    if internal_match:
        return HTTPPolicyDecision(
            True,
            "internal_allowlist",
            url,
            host,
            port,
            matched_rule=internal_match,
        )

    resolved_ips: tuple[str, ...] = ()
    if config.resolve_dns:
        try:
            resolved = resolver(host, port) if resolver else _resolve_host(host, port)
            resolved_ips = tuple(sorted({str(ip) for ip in resolved}))
        except socket.gaierror as exc:
            return HTTPPolicyDecision(False, "dns_resolution_failed", url, host, port)
        except OSError:
            return HTTPPolicyDecision(False, "dns_resolution_failed", url, host, port)

        if not resolved_ips:
            return HTTPPolicyDecision(False, "dns_resolution_failed", url, host, port)

        for ip_text in resolved_ips:
            try:
                blocked, reason = _is_blocked_ip(ip_text)
            except ValueError:
                return HTTPPolicyDecision(False, "dns_resolution_failed", url, host, port)
            if blocked:
                return HTTPPolicyDecision(False, reason, url, host, port, resolved_ips)

    if config.domain_allowlist:
        for pattern in config.domain_allowlist:
            if _domain_matches(pattern, host):
                return HTTPPolicyDecision(
                    True,
                    "domain_allowlist",
                    url,
                    host,
                    port,
                    resolved_ips,
                    matched_rule=pattern,
                )
        return HTTPPolicyDecision(False, "domain_allowlist_miss", url, host, port, resolved_ips)

    return HTTPPolicyDecision(True, "public_network", url, host, port, resolved_ips)


def redirect_allowed(
    from_url: str,
    to_url: str,
    config: Optional[HTTPPolicyConfig] = None,
    resolver: Optional[Resolver] = None,
) -> HTTPPolicyDecision:
    """Evaluate a redirect target, including scheme downgrade policy."""
    config = config or load_http_policy_config()
    from_parsed = urlparse(from_url)
    to_parsed = urlparse(to_url)
    if (
        from_parsed.scheme == "https"
        and to_parsed.scheme == "http"
        and not config.allow_https_to_http_redirect
    ):
        return HTTPPolicyDecision(False, "https_to_http_redirect", to_url, to_parsed.hostname)
    return evaluate_http_url(to_url, config=config, resolver=resolver)


def blocked_network_error(decision: HTTPPolicyDecision, message: Optional[str] = None) -> dict[str, Any]:
    return {
        "type": "blocked_network_target",
        "message": message
        or "Private, loopback, link-local, reserved, and metadata network targets are blocked unless explicitly allowlisted.",
        "reason": decision.reason,
    }


def redact_secrets(value: Any) -> Any:
    """Best-effort redaction for audit payloads."""
    if isinstance(value, dict):
        redacted: dict[str, Any] = {}
        for key, item in value.items():
            if str(key).lower() in SENSITIVE_HEADER_NAMES:
                redacted[key] = "[REDACTED]"
            else:
                redacted[key] = redact_secrets(item)
        return redacted
    if isinstance(value, list):
        return [redact_secrets(item) for item in value]
    if isinstance(value, tuple):
        return tuple(redact_secrets(item) for item in value)
    if isinstance(value, str):
        text = value
        for pattern in SECRET_PATTERNS:
            text = pattern.sub(lambda match: f"{match.group(1)}[REDACTED]" if match.groups() else "[REDACTED]", text)
        return text
    return value


_audit_lock = threading.Lock()


def audit_http_event(event: dict[str, Any]) -> None:
    """Append a redacted HTTP tooling event to Nymeria's audit log."""
    try:
        from ..config import get_settings

        settings = get_settings()
        if not settings.audit_log_enabled:
            return
        log_dir = settings.logs_dir
        log_dir.mkdir(parents=True, exist_ok=True)
        now = datetime.now(UTC)
        entry = {
            "timestamp": now.isoformat(),
            "event_type": "http_tool_call",
            **redact_secrets(event),
        }
        log_file = log_dir / f"audit_{now.strftime('%Y%m%d')}.jsonl"
        with _audit_lock:
            with open(log_file, "a", encoding="utf-8") as f:
                f.write(json.dumps(entry, default=str) + "\n")
    except Exception:
        logger.debug("Failed to write HTTP audit event", exc_info=True)
