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
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any, Callable, Iterable, Optional
from urllib.parse import urljoin, urlparse

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


class HTTPPolicyViolation(RuntimeError):
    """Raised when a concrete outbound request target violates policy."""

    def __init__(
        self,
        decision: HTTPPolicyDecision,
        redirect_chain: Optional[list[dict[str, Any]]] = None,
    ):
        self.decision = decision
        self.redirect_chain = redirect_chain or []
        super().__init__(policy_error_message(decision))


class HTTPPolicyRedirectLimit(RuntimeError):
    """Raised when a policy-managed request exceeds redirect limits."""

    def __init__(self, redirect_chain: list[dict[str, Any]]):
        self.redirect_chain = redirect_chain
        super().__init__("Maximum redirect count exceeded")


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


def _idna_encode(host: str) -> Optional[str]:
    """The IDNA2008/UTS46 form, which is the spelling clients actually connect with.

    ``str.encode("idna")`` is NOT this. The stdlib codec implements IDNA2003,
    and the two standards disagree on the deviation characters (``ß``, final
    sigma, ZWJ, ZWNJ): stdlib maps ``faß.de`` to ``fass.de`` while httpx and
    requests, which both use the ``idna`` package, ask for ``xn--fa-hia.de``.
    Those are different domains, so a policy that judged one and a client that
    connected to the other would be checking something it never reached.
    """
    try:
        import idna

        return idna.encode(host, uts46=True).decode("ascii").lower()
    except Exception:  # noqa: BLE001 - any malformed label simply has no IDNA form
        return None


def _normalize_host(host: str) -> str:
    """One spelling for every host comparison in this module.

    ASCII hosts (including every IP literal) pass through untouched, so this is
    a no-op for all but internationalized names. Those are folded to punycode so
    the address the policy RESOLVES is the address the client CONNECTS to; the
    pin below then keys on the same string the socket layer will ask for.
    """
    normalized = host.strip("[]").rstrip(".").lower()
    if normalized.isascii():
        return normalized
    return _idna_encode(normalized) or normalized


def _host_pin_keys(host: str) -> frozenset[str]:
    """Every spelling a client may hand ``getaddrinfo`` for one hostname.

    ``urlparse`` reports an internationalized host in its UNICODE form while
    httpx and requests connect with the IDNA form, so a pin keyed on the unicode
    spelling alone matched neither: the dispatcher fell through to the system
    resolver and the check and the connect performed independent lookups, which
    is exactly the rebinding window the pin exists to close. It failed silently,
    because the screen still ran, so only the pin's behaviour reveals it.

    ``_normalize_host`` now folds a decision's host to the UTS46 spelling, so
    the first key is already the one the socket layer will ask for. The stdlib
    IDNA2003 form is kept as a second key for the deviation characters, where
    the two standards disagree and some other client (anything going through
    ``str.encode("idna")``) may ask differently; it is a no-op for ASCII hosts.
    """
    normalized = _normalize_host(host)
    keys = {normalized}
    try:
        keys.add(normalized.encode("idna").decode("ascii").lower())
    except (UnicodeError, UnicodeDecodeError):
        # Labels over 63 bytes, empty labels, and a few other shapes have no
        # IDNA form. Those cannot be connected to either, so one key is enough.
        pass
    return frozenset(keys)


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
        except socket.gaierror:
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


def policy_error_message(decision: HTTPPolicyDecision, label: str = "URL") -> str:
    return f"{label} blocked by HTTP egress policy ({decision.reason}): {decision.target}"


def validate_http_egress_url(
    url: str,
    *,
    label: str = "URL",
    config: Optional[HTTPPolicyConfig] = None,
    resolver: Optional[Resolver] = None,
    resolve_dns: Optional[bool] = None,
) -> str:
    """Return a stripped URL or raise ValueError if policy blocks it."""
    candidate = str(url or "").strip()
    if resolve_dns is not None:
        loaded = config or load_http_policy_config()
        config = HTTPPolicyConfig(
            internal_allowlist=loaded.internal_allowlist,
            domain_allowlist=loaded.domain_allowlist,
            domain_blocklist=loaded.domain_blocklist,
            max_redirects=loaded.max_redirects,
            allow_https_to_http_redirect=loaded.allow_https_to_http_redirect,
            resolve_dns=resolve_dns,
        )
    decision = evaluate_http_url(candidate, config=config, resolver=resolver)
    if not decision.allowed:
        raise ValueError(policy_error_message(decision, label=label))
    return candidate


def _raise_if_blocked(
    decision: HTTPPolicyDecision,
    redirect_chain: Optional[list[dict[str, Any]]] = None,
) -> None:
    if not decision.allowed:
        raise HTTPPolicyViolation(decision, redirect_chain)


def _redirect_target(current_url: str, location: str) -> str:
    return urljoin(current_url, location)


# The requests spelling of ``policy_http_client``'s neutralised mounts, and it
# exists for the same reason: a proxy resolves the hostname ITSELF and opens the
# socket, so a proxied request never touches the address this module approved and
# both the private-address block and the DNS pin become advisory. This path is
# the higher-exposure one of the two, because ``web_fetch``/``browser`` take the
# URL as a plain tool argument where the integrations take theirs from a vault
# record.
#
# All THREE keys are load-bearing, measured on requests 2.34.2: an explicit key
# survives ``merge_environment_settings``' ``setdefault`` and is then dropped by
# ``merge_setting``, leaving no proxy, but omitting ``all`` lets ``ALL_PROXY``
# alone still route the request. `trust_env` stays on, so ``REQUESTS_CA_BUNDLE``,
# ``NO_PROXY`` and ``.netrc`` are untouched; only proxy routing is dropped.
_NO_ENV_PROXIES = {"http": None, "https": None, "all": None}


def requests_get_with_policy(
    url: str,
    *,
    session: Optional[Any] = None,
    headers: Optional[dict[str, str]] = None,
    params: Optional[dict[str, Any]] = None,
    stream: bool = False,
    timeout: float = 30,
    verify: bool = True,
    follow_redirects: bool = True,
    config: Optional[HTTPPolicyConfig] = None,
    resolver: Optional[Resolver] = None,
):
    """Issue a requests GET with policy checks on the initial URL and redirects."""
    import requests

    policy_config = config or load_http_policy_config()
    redirect_chain: list[dict[str, Any]] = []
    current_url = str(url or "").strip()
    current_params = params
    requester = session or requests

    while True:
        decision = evaluate_http_url(current_url, config=policy_config, resolver=resolver)
        _raise_if_blocked(decision, redirect_chain)

        with pinned_dns_resolution(decision):
            response = requester.get(
                current_url,
                headers=headers,
                params=current_params,
                stream=stream,
                timeout=timeout,
                verify=verify,
                allow_redirects=False,
                # The stub types this `MutableMapping[str, str]`, narrower than
                # the runtime contract: a None value is how requests is told a
                # scheme has no proxy, and it is the only spelling that survives
                # `merge_environment_settings`' setdefault. Verified against
                # requests 2.34.2.
                proxies=_NO_ENV_PROXIES,  # pyrefly: ignore[bad-argument-type]
            )

        if not follow_redirects or not getattr(response, "is_redirect", False):
            return response, redirect_chain, decision

        location = response.headers.get("location")
        if not location:
            return response, redirect_chain, decision

        redirect_url = _redirect_target(str(response.url), location)
        redirect_decision = redirect_allowed(
            str(response.url),
            redirect_url,
            config=policy_config,
            resolver=resolver,
        )
        redirect_chain.append(
            {
                "status_code": response.status_code,
                "url": str(response.url),
                "location": location,
                "redirect_url": redirect_url,
                "policy": redirect_decision.to_dict(),
            }
        )
        _raise_if_blocked(redirect_decision, redirect_chain)
        if len(redirect_chain) > policy_config.max_redirects:
            raise HTTPPolicyRedirectLimit(redirect_chain)
        current_url = redirect_url
        current_params = None


def policy_http_client(**kwargs: Any):
    """Build an ``httpx.Client`` whose requests the egress policy can govern.

    A client that trusts the environment mounts a transport for `HTTP_PROXY` and
    `HTTPS_PROXY`, and a proxied request never touches the address this module
    approved: the socket connects to the proxy, and the PROXY resolves the name.
    Both the private-address block and the DNS pin become advisory, silently and
    only on deployments that happen to set those variables. So the two scheme
    mounts are neutralised here.

    Neutralising the mounts rather than passing ``trust_env=False`` is the whole
    point of the shape: `trust_env` also governs `SSL_CERT_FILE`, `SSLKEYLOGFILE`
    and `.netrc`, and a corporate deployment that sets a proxy is exactly the one
    likely to need a custom CA bundle. Only proxy routing is dropped. httpx
    resolves `mounts` most-specific-first, so `{"http://": None, "https://":
    None}` displaces the env proxy patterns while `{"all://": None}` does NOT
    (measured against httpx 0.28.1: `all://` sorts after the scheme patterns and
    never wins).

    A caller that genuinely wants a proxy is asking for an egress path this
    module cannot see through, which is a policy decision rather than a
    connection setting; there is no such caller today.

    One httpx behaviour to know before passing ``transport=``, which only the
    ``http_api`` test seam does: ``Client._init_transport`` returns a supplied
    transport verbatim, so ``verify``, ``cert``, ``limits``, ``http1`` and
    ``http2`` are silently ignored alongside it. That is httpx's rule, not this
    factory's, and it is harmless on that seam (a mock transport honours none of
    them anyway), but a production caller passing both would not get the TLS
    settings it asked for. Such a caller does not need this factory: an explicit
    transport already disables the env proxies this exists to drop.
    """
    import httpx

    mounts = dict(kwargs.pop("mounts", None) or {})
    mounts.setdefault("http://", None)
    mounts.setdefault("https://", None)
    return httpx.Client(mounts=mounts, **kwargs)


def httpx_request_with_policy(
    method: str,
    url: str,
    *,
    client: Optional[Any] = None,
    follow_redirects: bool = True,
    config: Optional[HTTPPolicyConfig] = None,
    resolver: Optional[Resolver] = None,
    **request_kwargs: Any,
):
    """Issue an httpx request with policy checks on the initial URL and redirects."""
    import httpx

    policy_config = config or load_http_policy_config()
    redirect_chain: list[dict[str, Any]] = []
    current_method = method.upper()
    current_url = str(url or "").strip()
    current_kwargs = dict(request_kwargs)
    owned_client = client is None
    http_client = client or policy_http_client(
        follow_redirects=False,
        limits=httpx.Limits(max_keepalive_connections=0),
        trust_env=False,
    )

    try:
        while True:
            decision = evaluate_http_url(current_url, config=policy_config, resolver=resolver)
            _raise_if_blocked(decision, redirect_chain)

            with pinned_dns_resolution(decision):
                response = http_client.request(
                    current_method,
                    current_url,
                    follow_redirects=False,
                    **current_kwargs,
                )

            if not follow_redirects or not response.is_redirect:
                return response, redirect_chain, decision

            location = response.headers.get("location")
            if not location:
                return response, redirect_chain, decision

            redirect_url = str(response.url.join(location))
            redirect_decision = redirect_allowed(
                str(response.url),
                redirect_url,
                config=policy_config,
                resolver=resolver,
            )
            redirect_chain.append(
                {
                    "status_code": response.status_code,
                    "url": str(response.url),
                    "location": location,
                    "redirect_url": redirect_url,
                    "policy": redirect_decision.to_dict(),
                }
            )
            _raise_if_blocked(redirect_decision, redirect_chain)
            if len(redirect_chain) > policy_config.max_redirects:
                raise HTTPPolicyRedirectLimit(redirect_chain)

            if response.status_code in {301, 302, 303} and current_method != "HEAD":
                current_method = "GET"
                for key in ("content", "data", "files", "json"):
                    current_kwargs.pop(key, None)
            current_url = redirect_url
            current_kwargs.pop("params", None)
    finally:
        if owned_client:
            http_client.close()


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

# DNS-pin dispatcher state. The previous implementation swapped
# ``socket.getaddrinfo`` per request under a global lock held across the whole
# network transfer, which serialized every policy-managed HTTP call
# process-wide. Instead we install one transparent ``getaddrinfo`` replacement
# (``_dns_pin_dispatcher``) exactly once and carry the active pin in
# thread-local state, so concurrent requests on different threads pin
# independently with no lock around the network I/O.
#
# ``_SYSTEM_GETADDRINFO`` is the real resolver, captured once at import before
# any install. The dispatcher delegates every non-pinned lookup straight to it,
# so the dispatcher is behaviourally invisible when no thread has an active pin
# and can never recurse into itself. It is read as a module global at call time
# so tests can substitute a hostile resolver to exercise the pin.
_SYSTEM_GETADDRINFO: Callable[..., Any] = socket.getaddrinfo
_dns_install_lock = threading.Lock()
_dns_pin_state = threading.local()


def _coerce_port(value: Any) -> Optional[int]:
    if value is None:
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        try:
            return socket.getservbyname(str(value))
        except OSError:
            return None


def _addrinfo_for_pinned_ip(
    ip_text: str,
    port: int,
    family: int,
    socktype: int,
    proto: int,
) -> Optional[tuple[Any, ...]]:
    try:
        ip = ipaddress.ip_address(ip_text)
    except ValueError:
        return None
    ip_family = socket.AF_INET6 if ip.version == 6 else socket.AF_INET
    if family not in {0, ip_family}:
        return None
    if socktype not in {0, socket.SOCK_STREAM}:
        return None
    if proto not in {0, socket.IPPROTO_TCP}:
        return None
    sockaddr = (ip_text, port, 0, 0) if ip.version == 6 else (ip_text, port)
    return (
        ip_family,
        socktype or socket.SOCK_STREAM,
        proto or socket.IPPROTO_TCP,
        "",
        sockaddr,
    )


def _dns_pin_dispatcher(
    host: Any,
    port: Any,
    family: int = 0,
    type: int = 0,
    proto: int = 0,
    flags: int = 0,
):
    """Process-wide ``getaddrinfo`` that honours the calling thread's pin.

    When the current thread has an active pin matching the requested
    ``(host, port)``, return the policy-approved addresses; otherwise delegate to
    ``_SYSTEM_GETADDRINFO`` (the real resolver captured at import). The pin lives
    in thread-local state, so concurrent policy-managed requests can pin
    different hosts at the same time without a shared lock around the request.
    """
    pin = getattr(_dns_pin_state, "active", None)
    if pin is not None:
        target_hosts, target_port, pinned_ips = pin
        if _normalize_host(str(host)) in target_hosts and _coerce_port(port) == target_port:
            infos = [
                info
                for ip_text in pinned_ips
                if (
                    info := _addrinfo_for_pinned_ip(
                        ip_text,
                        target_port,
                        family,
                        type,
                        proto,
                    )
                )
                is not None
            ]
            if infos:
                return infos
            raise socket.gaierror(socket.EAI_NONAME, "No pinned address for requested family")
    return _SYSTEM_GETADDRINFO(host, port, family, type, proto, flags)


def _ensure_dns_dispatcher_installed() -> None:
    """Install the transparent ``getaddrinfo`` dispatcher (idempotent).

    The dispatcher is left in place once installed: it delegates all non-pinned
    lookups to the system resolver, so leaving it installed is transparent and
    avoids reintroducing the per-request global swap (and its lock) that this
    change exists to remove. The lock guards only the one-time pointer swap; no
    network I/O happens while it is held. If some other component has displaced
    the dispatcher, re-assert it so an active pin is never silently ignored.
    """
    if socket.getaddrinfo is _dns_pin_dispatcher:
        return
    with _dns_install_lock:
        if socket.getaddrinfo is not _dns_pin_dispatcher:
            socket.getaddrinfo = _dns_pin_dispatcher


@contextmanager
def pinned_dns_resolution(decision: HTTPPolicyDecision):
    """
    Force the request-time resolver to use the IPs approved by policy.

    requests/httpx keep the URL hostname for Host, SNI, and certificate checks,
    but their socket layer would otherwise perform a second DNS lookup during
    connect. This scoped resolver pin closes the DNS-rebinding gap for
    policy-managed synchronous requests.

    The pin is stored in thread-local state behind a process-wide transparent
    ``getaddrinfo`` dispatcher (installed once on first use), so two threads can
    pin different hosts simultaneously and no lock is held across the network
    request.

    Correctness depends on the caller resolving and connecting on the same
    thread that entered this context. That holds for synchronous ``requests``
    and ``httpx.Client`` callers, which is how every policy wrapper here issues
    requests. It does NOT hold for ``httpx.AsyncClient``: async DNS runs on an
    anyio worker thread that does not carry this thread-local pin, so the pin
    would be silently skipped and the rebinding protection lost. Keep these
    wrappers synchronous (callers that need them off the event loop should use
    ``asyncio.to_thread`` so resolve and connect stay on one worker thread).
    """
    if not decision.host or decision.port is None or not decision.resolved_ips:
        yield
        return

    _ensure_dns_dispatcher_installed()
    pin = (_host_pin_keys(decision.host), decision.port, decision.resolved_ips)
    previous = getattr(_dns_pin_state, "active", None)
    _dns_pin_state.active = pin
    try:
        yield
    finally:
        _dns_pin_state.active = previous


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
        logger.error("Failed to write HTTP audit event", exc_info=True)
