"""HTTP clients for MCP discovery registries.

Phase 1 ships two fetchers:

- OfficialMCPRegistryFetcher (registry.modelcontextprotocol.io) - used for
  both search and install-ID resolution.
- SmitheryFetcher (registry.smithery.ai) - search only; optional bearer key
  surfaces private/verified listings when configured.

Shape mirrors nymeria/skills/marketplace.py: a Protocol with list(query) plus
a 15-minute TTL cache over the raw HTTP responses. Registries are rate-limited
upstream, so holding results briefly pays for itself.
"""

from __future__ import annotations

import logging
import threading
import time
from dataclasses import dataclass
from typing import Any, Dict, List, Optional, Protocol

from ..config import get_settings
from ..tools.definitions.mcp_schema import MCPServerDefinition
from .http_policy import (
    HTTPPolicyRedirectLimit,
    HTTPPolicyViolation,
    requests_get_with_policy,
    validate_http_egress_url,
)

logger = logging.getLogger(__name__)

LIST_CACHE_TTL_SECONDS = 15 * 60


@dataclass
class MCPRegistryEntry:
    """A server visible in a registry listing."""

    id: str  # canonical registry id, e.g. "io.github.org/repo"
    name: str  # human-readable short name
    description: str
    source: str  # "official", "smithery", ...
    install_hint: Optional[str] = None  # stdio command string, package URL, or HTTP URL


class RegistryError(Exception):
    """Raised when a registry lookup fails."""


class MCPRegistryFetcher(Protocol):
    source_name: str

    def list(self, query: Optional[str] = None) -> List[MCPRegistryEntry]: ...


class _TTLCache:
    """Tiny key -> (value, expires_at) cache. Thread-safe."""

    def __init__(self, ttl_seconds: int = LIST_CACHE_TTL_SECONDS):
        self._ttl = ttl_seconds
        self._lock = threading.Lock()
        self._store: Dict[str, tuple] = {}

    def get(self, key: str):
        with self._lock:
            entry = self._store.get(key)
            if not entry:
                return None
            value, expires_at = entry
            if time.time() >= expires_at:
                self._store.pop(key, None)
                return None
            return value

    def put(self, key: str, value) -> None:
        with self._lock:
            self._store[key] = (value, time.time() + self._ttl)


# ---- Official MCP Registry ----

class OfficialMCPRegistryFetcher:
    """Client for registry.modelcontextprotocol.io.

    The registry is still in preview as of 2026 — surface shape may change.
    We code defensively and skip entries we cannot interpret.
    """

    source_name = "official"

    def __init__(self, http_session=None, base_url: Optional[str] = None):
        if http_session is None:
            import requests
            self._http = requests.Session()
            self._http.headers.update({"Accept": "application/json"})
        else:
            self._http = http_session
        settings = get_settings()
        try:
            self._base = validate_http_egress_url(
                (base_url or settings.mcp_registry_url).rstrip("/"),
                label="MCP registry URL",
            )
        except ValueError as e:
            raise RegistryError(str(e)) from e
        self._cache = _TTLCache()

    def list(self, query: Optional[str] = None) -> List[MCPRegistryEntry]:
        cache_key = f"list|{query or ''}"
        cached = self._cache.get(cache_key)
        if cached is not None:
            return cached

        url = f"{self._base}/v0/servers"
        params: Dict[str, Any] = {"limit": 25}
        if query:
            params["search"] = query

        try:
            resp, _redirect_chain, _policy = requests_get_with_policy(
                url,
                session=self._http,
                params=params,
                timeout=15,
            )
        except (HTTPPolicyViolation, HTTPPolicyRedirectLimit) as e:
            raise RegistryError(f"official registry request blocked: {e}") from e
        except Exception as e:
            raise RegistryError(f"official registry request failed: {e}") from e

        if resp.status_code >= 400:
            raise RegistryError(f"official registry HTTP {resp.status_code}: {resp.text[:200]}")

        try:
            payload = resp.json()
        except Exception as e:
            raise RegistryError(f"official registry returned non-JSON: {e}") from e

        servers = payload.get("servers") or payload.get("data") or []
        entries: List[MCPRegistryEntry] = []
        for raw in servers:
            entry = self._entry_from_raw(raw)
            if entry is not None:
                entries.append(entry)

        self._cache.put(cache_key, entries)
        return entries

    def fetch_detail(self, server_id: str) -> Dict[str, Any]:
        """Fetch a server's full record, picking the latest version envelope.

        The official registry exposes detail via `/v0/servers/{id}/versions`,
        which returns {"servers": [envelope, envelope, ...]}. Each envelope is
        {"server": {...}, "_meta": {...}}. We unwrap and pick the latest entry
        (preferring an explicit latest flag, falling back to the last element).
        """
        cache_key = f"detail|{server_id}"
        cached = self._cache.get(cache_key)
        if cached is not None:
            return cached

        from urllib.parse import quote
        url = f"{self._base}/v0/servers/{quote(server_id, safe='')}/versions"
        try:
            resp, _redirect_chain, _policy = requests_get_with_policy(
                url,
                session=self._http,
                timeout=15,
            )
        except (HTTPPolicyViolation, HTTPPolicyRedirectLimit) as e:
            raise RegistryError(f"official registry request blocked: {e}") from e
        except Exception as e:
            raise RegistryError(f"official registry request failed: {e}") from e
        if resp.status_code == 404:
            raise RegistryError(f"server not found in official registry: {server_id}")
        if resp.status_code >= 400:
            raise RegistryError(f"official registry HTTP {resp.status_code}: {resp.text[:200]}")
        try:
            payload = resp.json()
        except Exception as e:
            raise RegistryError(f"official registry returned non-JSON: {e}") from e

        envelopes = payload.get("servers") or payload.get("versions") or payload.get("data") or []
        if not isinstance(envelopes, list) or not envelopes:
            raise RegistryError(f"no versions listed for '{server_id}'")

        # Prefer an explicitly-flagged latest. If none is flagged, fall back to
        # the first entry: the registry orders newest-first, verified against
        # the live API on 2026-04 for both list and versions endpoints.
        latest = next(
            (e for e in envelopes if isinstance(e, dict) and (
                (e.get("_meta") or {}).get("isLatest")
                or (e.get("_meta") or {}).get("is_latest")
                or e.get("isLatest")
                or e.get("is_latest")
            )),
            envelopes[0],
        )
        server = self._unwrap_envelope(latest)
        if server is None:
            raise RegistryError(f"could not extract server record for '{server_id}'")

        self._cache.put(cache_key, server)
        return server

    @staticmethod
    def _unwrap_envelope(raw: Any) -> Optional[Dict[str, Any]]:
        """Handle both the new {"server": {...}, "_meta": {...}} envelope shape
        and the old flat shape, so we stay working across minor API tweaks.
        """
        if not isinstance(raw, dict):
            return None
        if isinstance(raw.get("server"), dict):
            return raw["server"]
        return raw

    def _entry_from_raw(self, raw: Dict[str, Any]) -> Optional[MCPRegistryEntry]:
        server = self._unwrap_envelope(raw)
        if server is None:
            return None
        rid = server.get("id") or server.get("name")
        if not rid:
            return None
        name = (
            server.get("display_name")
            or server.get("displayName")
            or server.get("name")
            or rid
        )
        description = server.get("description", "") or ""
        install_hint = self._install_hint_from_raw(server)
        return MCPRegistryEntry(
            id=rid,
            name=name,
            description=description,
            source=self.source_name,
            install_hint=install_hint,
        )

    @staticmethod
    def _install_hint_from_raw(raw: Dict[str, Any]) -> Optional[str]:
        """Pick the first viable install hint from a server entry's packages.

        Accepts the unwrapped server record OR the envelope (unwraps if needed).
        Accepts both snake_case and camelCase field names (registry_type /
        registryType) since the registry has shipped both.
        """
        server = OfficialMCPRegistryFetcher._unwrap_envelope(raw) or {}
        packages = server.get("packages") or []
        if not isinstance(packages, list):
            return None
        for pkg in packages:
            if not isinstance(pkg, dict):
                continue
            reg = (
                pkg.get("registry_type")
                or pkg.get("registryType")
                or pkg.get("registry")
                or ""
            ).lower()
            name = pkg.get("name") or pkg.get("identifier")
            if not name:
                continue
            version = pkg.get("version") or "latest"
            if reg in ("npm", "npmjs"):
                return f"npx -y {name}@{version}"
            if reg in ("pypi", "pip"):
                return f"uvx {name}=={version}" if version != "latest" else f"uvx {name}"
            if reg in ("oci", "docker"):
                return None  # Phase 2: surface as a docker-catalog hint.
        # Check remote endpoints as a last resort (HTTP/SSE servers).
        remotes = server.get("remotes") or []
        if isinstance(remotes, list):
            for r in remotes:
                if isinstance(r, dict) and r.get("url"):
                    return r["url"]
        return None


# ---- Smithery ----

class SmitheryFetcher:
    source_name = "smithery"

    def __init__(self, http_session=None):
        if http_session is None:
            import requests
            self._http = requests.Session()
        else:
            self._http = http_session
        settings = get_settings()
        api_key = getattr(settings, "smithery_api_key", None) or ""
        if api_key:
            self._http.headers.update({"Authorization": f"Bearer {api_key}"})
        self._http.headers.update({"Accept": "application/json"})
        self._cache = _TTLCache()

    def list(self, query: Optional[str] = None) -> List[MCPRegistryEntry]:
        cache_key = f"list|{query or ''}"
        cached = self._cache.get(cache_key)
        if cached is not None:
            return cached

        url = "https://registry.smithery.ai/servers"
        params: Dict[str, Any] = {"pageSize": 25}
        if query:
            params["q"] = query

        try:
            resp, _redirect_chain, _policy = requests_get_with_policy(
                url,
                session=self._http,
                params=params,
                timeout=15,
            )
        except (HTTPPolicyViolation, HTTPPolicyRedirectLimit) as e:
            raise RegistryError(f"smithery request blocked: {e}") from e
        except Exception as e:
            raise RegistryError(f"smithery request failed: {e}") from e
        if resp.status_code >= 400:
            raise RegistryError(f"smithery HTTP {resp.status_code}: {resp.text[:200]}")
        try:
            payload = resp.json()
        except Exception as e:
            raise RegistryError(f"smithery returned non-JSON: {e}") from e

        servers = payload.get("servers") or payload.get("data") or []
        entries: List[MCPRegistryEntry] = []
        for raw in servers:
            if not isinstance(raw, dict):
                continue
            rid = raw.get("qualifiedName") or raw.get("id") or raw.get("name")
            if not rid:
                continue
            entries.append(MCPRegistryEntry(
                id=rid,
                name=raw.get("displayName") or raw.get("name") or rid,
                description=raw.get("description", "") or "",
                source=self.source_name,
                install_hint=None,  # Smithery install requires their signed connection URL; phase 2.
            ))

        self._cache.put(cache_key, entries)
        return entries


# ---- Public entrypoints used by the installer and agent tools ----

def search_registries(query: str) -> List[MCPRegistryEntry]:
    """Search across both registries, best-effort; errors from one do not block the other."""
    results: List[MCPRegistryEntry] = []
    for fetcher_cls in (OfficialMCPRegistryFetcher, SmitheryFetcher):
        try:
            fetcher = fetcher_cls()
            results.extend(fetcher.list(query))
        except RegistryError as e:
            logger.warning(f"{fetcher_cls.__name__} search failed: {e}")
    return results


def resolve_registry_id(server_id: str) -> MCPServerDefinition:
    """Resolve a registry ID (e.g. 'io.github.org/repo') into an installable definition.

    Uses the official MCP registry only. Raises MCPInstallError on any failure
    so the installer can surface a clean error to the caller.
    """
    from .mcp_installer import parse_mcp_source
    from .mcp_sources import MCPInstallError, new_mcp_server_id

    fetcher = OfficialMCPRegistryFetcher()
    try:
        detail = fetcher.fetch_detail(server_id)
    except RegistryError as e:
        raise MCPInstallError(f"could not resolve '{server_id}': {e}") from e

    install_hint = OfficialMCPRegistryFetcher._install_hint_from_raw(detail)
    if not install_hint:
        raise MCPInstallError(
            f"registry entry '{server_id}' has no installable package (npm/pypi/remote)"
        )

    # Delegate back to the ready-source parser for stdio/package/HTTP handling
    # and naming.
    defn = parse_mcp_source(
        install_hint,
        name=(
            detail.get("display_name")
            or detail.get("displayName")
            or detail.get("name")
            or server_id
        ),
        resolver=None,  # shouldn't recurse — install_hint is a command or URL
    )
    defn.description = detail.get("description", "") or defn.description
    # Re-id to match the registry id slug for traceability.
    defn.id = new_mcp_server_id(defn.name)
    return defn
