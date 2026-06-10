"""External-access automation helpers for the setup wizard.

TUI-free: plain subprocess and HTTP logic shared by the Textual steps
(steps/external_access.py), finalize, and tests. Covers the two automated
paths (Tailscale serve/funnel, Cloudflare named tunnel driven over the v4
API with a user-supplied token), public-URL verification, and the ASCII QR
rendering used for the Tailscale login and the bootstrap handoff.

Verification is two checks, not one: a GET on /health proves the tunnel
relays requests, and a streamed GET on /health/stream proves events arrive
incrementally. The second check exists because some relays accept requests
but buffer or drop event streams (Cloudflare quick tunnels are documented
as not supporting SSE), which would pass a health probe and then break chat
streaming, so the wizard must catch it before writing the URL.
"""

from __future__ import annotations

import asyncio
import io
import json
import os
import re
import shutil
import subprocess
import sys
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

import httpx

# Env keys finalize writes and hydrate reads back. NYMERIA_EXTERNAL_ACCESS is
# the wizard's own round-trip marker (the runtime does not read it); the other
# two are real backend settings (config/settings.py).
EXTERNAL_ACCESS_ENV = "NYMERIA_EXTERNAL_ACCESS"
PUBLIC_URL_ENV = "NYMERIA_PUBLIC_URL"
CORS_ORIGINS_ENV = "CORS_ORIGINS"

DEFAULT_BACKEND_PORT = 8000

TAILSCALE_INSTALL_COMMAND = "curl -fsSL https://tailscale.com/install.sh | sh"
TAILSCALE_DOWNLOAD_URL = "https://tailscale.com/download"
CLOUDFLARED_DOWNLOAD_URL = (
    "https://developers.cloudflare.com/cloudflare-one/connections/"
    "connect-networks/downloads/"
)
CLOUDFLARE_API_BASE = "https://api.cloudflare.com/client/v4"

# The AuthURL field inside `tailscale up --json` output. The modern client
# emits that block as MULTI-LINE indented JSON (json.MarshalIndent), so the
# field must be matched per line; a whole-line JSON parse never sees it.
_AUTH_URL_FIELD_RE = re.compile(r'"AuthURL"\s*:\s*"([^"]+)"')
# Bare login URL in plain-text output (older clients, and the macOS --json
# gap). Quotes and punctuation excluded so a JSON-quoted URL is not mangled.
_LOGIN_URL_RE = re.compile(r"https://login\.tailscale\.com/[^\s\"'<>,)]+")
_SUBPROCESS_TIMEOUT = 15.0

# Minimum first-to-last event spread that counts as incremental delivery in
# check_public_sse. Must stay well under the /health/stream emission window
# (see HEALTH_STREAM_* in api/routers/system.py, currently 2 x 0.6s).
SSE_MIN_SPREAD_SECONDS = 0.3


# --- tailscale ----------------------------------------------------------------


def find_tailscale() -> str | None:
    """Locate the tailscale CLI (PATH, then the macOS app bundle)."""
    found = shutil.which("tailscale")
    if found:
        return found
    if sys.platform == "darwin":
        mac_app = "/Applications/Tailscale.app/Contents/MacOS/Tailscale"
        if os.access(mac_app, os.X_OK):
            return mac_app
    return None


@dataclass(frozen=True)
class TailscaleStatus:
    backend_state: str  # "Running", "NeedsLogin", "Stopped", ...
    dns_name: str  # MagicDNS name without the trailing dot, may be ""
    tailscale_ips: tuple[str, ...]

    @property
    def logged_in(self) -> bool:
        return self.backend_state == "Running"

    @property
    def https_url(self) -> str | None:
        """The tailnet HTTPS origin serve/funnel exposes (MagicDNS only).

        No IP fallback: the serve certificate is minted for the DNS name, so
        an `https://100.x.y.z` origin could never verify; better to fail with
        the MagicDNS hint than persist a URL that cannot work.
        """
        if self.dns_name:
            return f"https://{self.dns_name}"
        return None


def _parse_json_with_noise(raw: str) -> dict[str, Any] | None:
    """Parse JSON out of CLI output with warning lines before or after it."""
    start = raw.find("{")
    if start < 0:
        return None
    try:
        parsed, _end = json.JSONDecoder().raw_decode(raw[start:])
    except ValueError:
        return None
    return parsed if isinstance(parsed, dict) else None


def parse_tailscale_status(raw: str) -> TailscaleStatus | None:
    """Parse `tailscale status --json` output into a TailscaleStatus."""
    payload = _parse_json_with_noise(raw)
    if payload is None:
        return None
    self_node = payload.get("Self") or {}
    dns_name = str(self_node.get("DNSName") or "").rstrip(".")
    ips = tuple(
        str(ip) for ip in (self_node.get("TailscaleIPs") or []) if str(ip).strip()
    )
    return TailscaleStatus(
        backend_state=str(payload.get("BackendState") or ""),
        dns_name=dns_name,
        tailscale_ips=ips,
    )


def tailscale_status(binary: str) -> TailscaleStatus | None:
    """Run `tailscale status --json`. None when the CLI fails outright."""
    try:
        result = subprocess.run(
            [binary, "status", "--json"],
            capture_output=True,
            text=True,
            timeout=_SUBPROCESS_TIMEOUT,
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    # `status --json` exits non-zero in some states (e.g. NeedsLogin) while
    # still printing the JSON document, so parse before checking the code.
    return parse_tailscale_status(result.stdout or "")


def extract_tailscale_auth_url(line: str) -> str | None:
    """Pull the login URL from one line of `tailscale up --json` output.

    Matches the `"AuthURL": "..."` field first (the real client pretty-prints
    the JSON across lines, so this is the form that actually appears), then a
    single-line JSON document, then a bare login URL in human-readable output
    (older clients, and the macOS `--json` gap).
    """
    field = _AUTH_URL_FIELD_RE.search(line)
    if field:
        return field.group(1)
    payload = _parse_json_with_noise(line)
    if payload is not None:
        auth_url = str(payload.get("AuthURL") or "").strip()
        if auth_url:
            return auth_url
    match = _LOGIN_URL_RE.search(line)
    return match.group(0) if match else None


class TailscaleLoginFlow:
    """A running `tailscale up --json` process being watched for its auth URL.

    `tailscale up` blocks until the node is authenticated, which is exactly
    the wizard semantics: surface the URL (and a QR of it), then wait for the
    process to exit 0. `error` carries stderr-ish output for the failure case
    (typically a permissions problem that needs `sudo tailscale up` once, or
    `sudo tailscale set --operator=$USER`).
    """

    def __init__(self, process: asyncio.subprocess.Process) -> None:
        self.process = process
        self.auth_url: str | None = None
        self.output: list[str] = []

    @classmethod
    async def start(cls, binary: str) -> "TailscaleLoginFlow":
        process = await asyncio.create_subprocess_exec(
            binary,
            "up",
            "--json",
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.STDOUT,
        )
        return cls(process)

    async def wait_for_auth_url(self, timeout: float = 30.0) -> str | None:
        """Read output until an auth URL appears, the process exits, or timeout.

        Returns None when the node authenticated without needing a login URL
        (already approved) or on failure; check `process.returncode`.
        """
        assert self.process.stdout is not None
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            remaining = deadline - time.monotonic()
            try:
                raw = await asyncio.wait_for(
                    self.process.stdout.readline(), timeout=max(remaining, 0.1)
                )
            except asyncio.TimeoutError:
                return None
            if not raw:
                return None  # EOF: up finished (or failed) without a URL
            line = raw.decode("utf-8", errors="replace").rstrip()
            if line:
                self.output.append(line)
            url = extract_tailscale_auth_url(line)
            if url:
                self.auth_url = url
                return url
        return None

    async def wait_until_done(self, timeout: float = 300.0) -> bool:
        """Wait for `tailscale up` to exit. True on exit code 0."""
        assert self.process.stdout is not None
        try:
            async with asyncio.timeout(timeout):
                # Drain remaining output so the pipe never blocks the exit.
                while True:
                    raw = await self.process.stdout.readline()
                    if not raw:
                        break
                    line = raw.decode("utf-8", errors="replace").rstrip()
                    if line:
                        self.output.append(line)
                await self.process.wait()
        except asyncio.TimeoutError:
            return False
        return self.process.returncode == 0

    def terminate(self) -> None:
        if self.process.returncode is None:
            try:
                self.process.terminate()
            except ProcessLookupError:
                pass  # already exited between the check and the signal

    @property
    def error(self) -> str:
        tail = [line for line in self.output if line][-4:]
        return " / ".join(tail)


def permission_hint(detail: str) -> str:
    lowered = detail.lower()
    if "permission denied" in lowered or "access denied" in lowered:
        return (
            f"{detail} (run `sudo tailscale set --operator=$USER` once to let "
            "your user manage tailscale without sudo, then retry)"
        )
    return detail


def enable_tailscale_serve(
    binary: str, port: int = DEFAULT_BACKEND_PORT, *, funnel: bool = False
) -> tuple[bool, str]:
    """Run `tailscale serve|funnel --bg <port>`. Returns (ok, detail).

    Serve exposes the backend tailnet-only over HTTPS; funnel exposes it to
    the public internet (first use may need a one-click admin-console
    approval, which tailscale prints to stderr with the exact URL).
    """
    subcommand = "funnel" if funnel else "serve"
    try:
        result = subprocess.run(
            [binary, subcommand, "--bg", str(port)],
            capture_output=True,
            text=True,
            timeout=60.0,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        return False, f"could not run tailscale {subcommand}: {exc}"
    if result.returncode != 0:
        detail = (result.stderr or result.stdout or "").strip()
        lowered = detail.lower()
        if "unknown" in lowered or "usage:" in lowered:
            detail += " (the bare-port serve/funnel form needs tailscale 1.52+)"
        return False, permission_hint(detail or f"tailscale {subcommand} failed")
    return True, (result.stdout or "").strip()


# --- cloudflare ---------------------------------------------------------------


def find_cloudflared() -> str | None:
    return shutil.which("cloudflared")


class CloudflareError(RuntimeError):
    """A Cloudflare v4 API failure, with the API's own messages when present."""


def tunnel_name_for_hostname(hostname: str) -> str:
    """Per-hostname tunnel name, so two installs on one account never share a
    tunnel (a shared name would make the second machine a load-balanced
    replica of the first and replace its ingress). Very long hostnames get a
    hash suffix so truncation cannot collide two of them back together."""
    import hashlib

    slug = re.sub(r"[^a-z0-9-]+", "-", hostname.lower()).strip("-")
    name = f"nymeria-{slug}"
    if len(name) > 63:
        digest = hashlib.sha256(hostname.lower().encode()).hexdigest()[:6]
        name = name[:56].rstrip("-") + "-" + digest
    return name


class CloudflareTunnelClient:
    """Minimal async client for the named-tunnel recipe over the v4 API.

    The user-supplied API token needs `Account > Cloudflare Tunnel: Edit`,
    `Zone > Zone: Read` (for the zone lookup), and `Zone > DNS: Edit`. Flow:
    verify token, find the zone (and its owning account) for the chosen
    hostname, create or reuse the tunnel, set its ingress to the local
    backend, point a proxied CNAME at it, then poll the connector status.
    """

    def __init__(
        self,
        api_token: str,
        *,
        base_url: str = CLOUDFLARE_API_BASE,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        self._client = httpx.AsyncClient(
            base_url=base_url,
            headers={"Authorization": f"Bearer {api_token}"},
            timeout=15.0,
            transport=transport,
        )

    async def aclose(self) -> None:
        await self._client.aclose()

    async def _request(self, method: str, path: str, **kwargs: Any) -> Any:
        try:
            response = await self._client.request(method, path, **kwargs)
        except httpx.HTTPError as exc:
            raise CloudflareError(f"Cloudflare API unreachable: {exc}") from exc
        try:
            payload = response.json()
        except ValueError as exc:
            raise CloudflareError(
                f"Cloudflare API returned non-JSON (HTTP {response.status_code})"
            ) from exc
        if not payload.get("success"):
            messages = "; ".join(
                str(err.get("message") or err)
                for err in payload.get("errors") or []
            )
            raise CloudflareError(messages or f"HTTP {response.status_code}")
        return payload.get("result")

    async def verify_token(self) -> None:
        await self._request("GET", "/user/tokens/verify")

    async def find_account_id(self) -> str:
        """Fallback account lookup; the zone's own account.id is preferred."""
        accounts = await self._request("GET", "/accounts")
        if not accounts:
            raise CloudflareError(
                "the API token can see no Cloudflare account (it may need "
                "account-level permissions)"
            )
        return str(accounts[0]["id"])

    async def find_zone(self, hostname: str) -> tuple[str, str, str]:
        """Return (zone_id, zone_name, account_id) for the hostname's zone.

        Queries `/zones?name=<suffix>` per candidate suffix instead of listing
        all zones: exact-name lookups dodge pagination on large accounts and
        return the zone's owning account, which is the right account for the
        tunnel even when the token spans several accounts.
        """
        host = hostname.lower().strip(".")
        labels = host.split(".")
        candidates = [".".join(labels[i:]) for i in range(len(labels) - 1)]
        for candidate in candidates:
            zones = await self._request("GET", "/zones", params={"name": candidate})
            if zones:
                zone = zones[0]
                account_id = str((zone.get("account") or {}).get("id") or "")
                return str(zone["id"]), str(zone.get("name") or candidate), account_id
        raise CloudflareError(
            f"no Cloudflare zone matches {hostname}. The domain must use "
            "Cloudflare DNS, and the token needs Zone: Read on it"
        )

    async def get_or_create_tunnel(self, account_id: str, name: str) -> tuple[str, str]:
        """Return (tunnel_id, tunnel_token), creating the tunnel if needed.

        Reuse is refused for a locally-managed tunnel of the same name: its
        connector ignores remote ingress config, so the recipe would report
        success while routing nothing.
        """
        existing = await self._request(
            "GET",
            f"/accounts/{account_id}/cfd_tunnel",
            params={"name": name, "is_deleted": "false"},
        )
        try:
            if existing:
                tunnel = existing[0]
                config_src = str(tunnel.get("config_src") or "cloudflare")
                if config_src != "cloudflare":
                    raise CloudflareError(
                        f"a tunnel named {name!r} already exists but is "
                        f"locally managed (config_src={config_src}); delete "
                        "or rename it, or configure it yourself"
                    )
                tunnel_id = str(tunnel["id"])
                token = await self._request(
                    "GET", f"/accounts/{account_id}/cfd_tunnel/{tunnel_id}/token"
                )
                return tunnel_id, str(token)
            created = await self._request(
                "POST",
                f"/accounts/{account_id}/cfd_tunnel",
                json={"name": name, "config_src": "cloudflare"},
            )
            return str(created["id"]), str(created["token"])
        except (KeyError, TypeError, IndexError) as exc:
            raise CloudflareError(
                f"unexpected tunnel API response shape ({exc!r})"
            ) from exc

    async def put_ingress(
        self,
        account_id: str,
        tunnel_id: str,
        hostname: str,
        service: str = f"http://localhost:{DEFAULT_BACKEND_PORT}",
    ) -> None:
        await self._request(
            "PUT",
            f"/accounts/{account_id}/cfd_tunnel/{tunnel_id}/configurations",
            json={
                "config": {
                    "ingress": [
                        {"hostname": hostname, "service": service},
                        {"service": "http_status:404"},
                    ]
                }
            },
        )

    async def ensure_dns_record(
        self, zone_id: str, hostname: str, tunnel_id: str
    ) -> None:
        """Point a proxied CNAME at the tunnel, updating an existing record."""
        content = f"{tunnel_id}.cfargotunnel.com"
        records = await self._request(
            "GET",
            f"/zones/{zone_id}/dns_records",
            params={"name": hostname},
        )
        body = {
            "type": "CNAME",
            "name": hostname,
            "content": content,
            "proxied": True,
        }
        if records:
            record_id = str(records[0]["id"])
            await self._request(
                "PUT", f"/zones/{zone_id}/dns_records/{record_id}", json=body
            )
        else:
            await self._request("POST", f"/zones/{zone_id}/dns_records", json=body)

    async def tunnel_healthy(self, account_id: str, tunnel_id: str) -> bool:
        tunnel = await self._request(
            "GET", f"/accounts/{account_id}/cfd_tunnel/{tunnel_id}"
        )
        return str((tunnel or {}).get("status") or "") == "healthy"


def _read_live_pid(pid_path: Path) -> int | None:
    """Return the pid recorded at pid_path if it is a live cloudflared.

    Windows is excluded outright: `os.kill(pid, 0)` there sends CTRL_C_EVENT
    (0 IS that event), which can terminate the very process being probed, so
    a re-run just starts another connector (a duplicate is a benign replica).
    On Linux the cmdline is checked too, so a recycled pid belonging to some
    other process does not masquerade as the connector.
    """
    if sys.platform == "win32":
        return None
    try:
        pid = int(pid_path.read_text(encoding="utf-8").strip())
    except (OSError, ValueError):
        return None
    try:
        os.kill(pid, 0)
    except PermissionError:
        return None  # alive but owned by someone else: not our connector
    except OSError:
        return None
    try:
        cmdline = Path(f"/proc/{pid}/cmdline").read_bytes()
    except OSError:
        return pid  # no /proc (macOS): liveness is the best signal available
    return pid if b"cloudflared" in cmdline else None


def start_cloudflared_connector(
    binary: str, tunnel_token: str, *, log_path: Path
) -> tuple[bool, str]:
    """Launch `cloudflared tunnel run` detached, logging to a file.

    The token rides in the TUNNEL_TOKEN environment variable, not argv, so it
    never shows in `ps` for the connector's lifetime. A pid file prevents
    wizard re-runs from stacking duplicate connectors. A detached process
    keeps the wizard sudo-free; the persisted assets (see
    persist_cloudflared_assets) carry the service-install command that makes
    it survive reboots.
    """
    log_path.parent.mkdir(parents=True, exist_ok=True)
    pid_path = log_path.parent / "cloudflared.pid"
    live = _read_live_pid(pid_path)
    if live is not None:
        return True, f"cloudflared connector already running (pid {live})"
    env = dict(os.environ)
    env["TUNNEL_TOKEN"] = tunnel_token
    creationflags = 0
    if sys.platform == "win32":
        # Fully detach: without DETACHED_PROCESS the child shares the wizard's
        # console and dies when that console window closes.
        creationflags = (
            getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)
            | getattr(subprocess, "DETACHED_PROCESS", 0)
            | getattr(subprocess, "CREATE_NO_WINDOW", 0)
        )
    try:
        with open(log_path, "ab") as log_file:
            process = subprocess.Popen(
                [binary, "tunnel", "run"],
                stdout=log_file,
                stderr=subprocess.STDOUT,
                env=env,
                start_new_session=(sys.platform != "win32"),
                creationflags=creationflags,
            )
    except OSError as exc:
        return False, f"could not start cloudflared: {exc}"
    # An immediate exit means a bad token or environment, not a slow start.
    time.sleep(1.0)
    if process.poll() is not None:
        return False, (
            f"cloudflared exited immediately (code {process.returncode}); "
            f"see {log_path}"
        )
    try:
        pid_path.write_text(f"{process.pid}\n", encoding="utf-8")
    except OSError:
        pass  # duplicate-start protection degrades; the connector still runs
    return True, f"cloudflared connector running (pid {process.pid})"


def persist_cloudflared_assets(root: Path, tunnel_token: str) -> Path:
    """Write the tunnel token (0600 from creation) and a README under
    <root>/cloudflared/."""
    directory = root / "cloudflared"
    directory.mkdir(parents=True, exist_ok=True)
    token_path = directory / "TUNNEL_TOKEN.txt"
    fd = os.open(token_path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as handle:
        handle.write(tunnel_token + "\n")
    readme = directory / "README.txt"
    readme.write_text(
        "Cloudflare tunnel connector for Nymeria.\n"
        "\n"
        "The setup wizard started a temporary connector process. To make it\n"
        "survive reboots, install it as a system service once (the token\n"
        "appears in the process list only for the install command itself):\n"
        "\n"
        f'  sudo cloudflared service install "$(cat {token_path})"\n'
        "\n"
        "Or re-start the temporary connector after a reboot with:\n"
        "\n"
        f'  TUNNEL_TOKEN="$(cat {token_path})" cloudflared tunnel run\n',
        encoding="utf-8",
    )
    return token_path


# --- verification ---------------------------------------------------------------


@dataclass(frozen=True)
class UrlProbe:
    status: str  # "healthy" | "origin_down" | "error" | "unreachable"
    detail: str

    @property
    def tunnel_reached(self) -> bool:
        """True when the relay answered at all (even with the backend down)."""
        return self.status in ("healthy", "origin_down")


# Gateway statuses a relay returns when it is up but the local backend is not:
# 502/504 (generic bad gateway), 503 (tailscale serve with origin down).
# Cloudflare's 530 is deliberately NOT here: it wraps error 1033 ("cannot
# reach the tunnel"), which means the CONNECTOR is down, the opposite signal.
_ORIGIN_DOWN_STATUSES = {502, 503, 504}


async def check_public_health(
    public_url: str,
    *,
    timeout: float = 10.0,
    transport: httpx.AsyncBaseTransport | None = None,
) -> UrlProbe:
    """GET <public_url>/health and classify what answered."""
    url = public_url.rstrip("/") + "/health"
    try:
        async with httpx.AsyncClient(
            timeout=timeout, follow_redirects=True, transport=transport
        ) as client:
            response = await client.get(url)
    except (httpx.HTTPError, httpx.InvalidURL) as exc:
        return UrlProbe(
            status="unreachable", detail=f"{type(exc).__name__}: {exc}"
        )
    if 200 <= response.status_code < 300:
        return UrlProbe(status="healthy", detail=f"HTTP {response.status_code}")
    if response.status_code in _ORIGIN_DOWN_STATUSES:
        return UrlProbe(
            status="origin_down",
            detail=(
                f"HTTP {response.status_code}: the tunnel answered but the "
                "backend is not running yet"
            ),
        )
    if response.status_code == 530:
        return UrlProbe(
            status="error",
            detail=(
                "HTTP 530: Cloudflare cannot reach the tunnel connector "
                "(cloudflared is not connected); check the connector log"
            ),
        )
    return UrlProbe(status="error", detail=f"HTTP {response.status_code}")


@dataclass(frozen=True)
class SseProbe:
    ok: bool
    streamed: bool
    events: int
    detail: str


async def check_public_sse(
    public_url: str,
    *,
    timeout: float = 20.0,
    transport: httpx.AsyncBaseTransport | None = None,
) -> SseProbe:
    """Stream <public_url>/health/stream and confirm events arrive incrementally.

    The endpoint emits its events spread over about a second, so a relay that
    buffers the stream delivers them in one burst at close: events with no
    spread between first and last means buffered, which breaks chat streaming.
    """
    url = public_url.rstrip("/") + "/health/stream"
    first_at: float | None = None
    last_at: float | None = None
    events = 0
    try:
        async with httpx.AsyncClient(
            timeout=timeout, follow_redirects=True, transport=transport
        ) as client:
            async with client.stream(
                "GET", url, headers={"Accept": "text/event-stream"}
            ) as response:
                if response.status_code != 200:
                    return SseProbe(
                        ok=False,
                        streamed=False,
                        events=0,
                        detail=f"HTTP {response.status_code}",
                    )
                content_type = response.headers.get("content-type", "")
                if "text/event-stream" not in content_type:
                    return SseProbe(
                        ok=False,
                        streamed=False,
                        events=0,
                        detail=f"unexpected content-type {content_type!r}",
                    )
                current_event = "message"
                async for line in response.aiter_lines():
                    if line.startswith("event:"):
                        current_event = line.split(":", 1)[1].strip()
                    elif not line:
                        current_event = "message"  # blank line = event boundary
                    elif line.startswith("data:") and current_event != "end":
                        # data lines of the `end` terminator frame are not
                        # probe events; counting them would misreport.
                        events += 1
                        now = time.monotonic()
                        if first_at is None:
                            first_at = now
                        last_at = now
    except (httpx.HTTPError, httpx.InvalidURL) as exc:
        return SseProbe(
            ok=False,
            streamed=False,
            events=events,
            detail=f"{type(exc).__name__}: {exc}",
        )
    if events == 0 or first_at is None or last_at is None:
        return SseProbe(
            ok=False, streamed=False, events=events, detail="no events received"
        )
    streamed = (last_at - first_at) > SSE_MIN_SPREAD_SECONDS
    detail = (
        "events streamed incrementally"
        if streamed
        else "events arrived in one burst (the relay buffers SSE)"
    )
    return SseProbe(ok=streamed, streamed=streamed, events=events, detail=detail)


# --- env derivation ---------------------------------------------------------------


def public_origin(url: str) -> str:
    """Normalize a URL to the origin form a browser sends in `Origin`.

    Lowercases scheme and host and strips default ports, because Starlette's
    CORS middleware matches origins as exact strings and browsers send them
    lowercase without `:443`/`:80`.
    """
    parts = urlsplit(url.strip())
    try:
        port = parts.port
    except ValueError:
        return url.strip().rstrip("/")
    # Rebuild from hostname/port rather than netloc: this drops userinfo (a
    # pasted user:pass@ proxy URL must not land in the env file) and trailing
    # host dots, neither of which a browser ever sends in Origin.
    host = (parts.hostname or "").rstrip(".")
    if not parts.scheme or not host:
        return url.strip().rstrip("/")
    scheme = parts.scheme.lower()
    if ":" in host:  # bare IPv6 from urlsplit: re-bracket
        host = f"[{host}]"
    default_port = {"https": 443, "http": 80}.get(scheme)
    if port is None or port == default_port:
        return f"{scheme}://{host}"
    return f"{scheme}://{host}:{port}"


def merged_cors_origins(existing: str, public_url: str) -> str:
    """Append the public origin to the existing (or default) CORS list."""
    from ..config.settings import DEFAULT_CORS_ORIGINS

    base = existing.strip() or DEFAULT_CORS_ORIGINS
    origins = [origin.strip() for origin in base.split(",") if origin.strip()]
    origin = public_origin(public_url)
    if origin and origin not in origins:
        origins.append(origin)
    return ",".join(origins)


# --- QR rendering ---------------------------------------------------------------


def qr_ascii(data: str) -> str | None:
    """Render `data` as a terminal QR code, or None when qrcode is missing."""
    try:
        import qrcode
    except ImportError:
        return None
    qr = qrcode.QRCode(
        border=2, error_correction=qrcode.constants.ERROR_CORRECT_L
    )
    qr.add_data(data)
    qr.make(fit=True)
    out = io.StringIO()
    qr.print_ascii(out=out, invert=True)
    return out.getvalue()


__all__ = [
    "CLOUDFLARED_DOWNLOAD_URL",
    "CORS_ORIGINS_ENV",
    "CloudflareError",
    "CloudflareTunnelClient",
    "DEFAULT_BACKEND_PORT",
    "EXTERNAL_ACCESS_ENV",
    "PUBLIC_URL_ENV",
    "SSE_MIN_SPREAD_SECONDS",
    "SseProbe",
    "TAILSCALE_INSTALL_COMMAND",
    "TAILSCALE_DOWNLOAD_URL",
    "TailscaleLoginFlow",
    "TailscaleStatus",
    "UrlProbe",
    "check_public_health",
    "check_public_sse",
    "enable_tailscale_serve",
    "extract_tailscale_auth_url",
    "find_cloudflared",
    "find_tailscale",
    "merged_cors_origins",
    "parse_tailscale_status",
    "permission_hint",
    "persist_cloudflared_assets",
    "public_origin",
    "qr_ascii",
    "start_cloudflared_connector",
    "tailscale_status",
    "tunnel_name_for_hostname",
]
