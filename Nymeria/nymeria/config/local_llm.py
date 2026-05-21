"""Local LLM endpoint detection and metadata probing."""

from __future__ import annotations

import ipaddress
import logging
import re
import time
from typing import Any, Literal
from urllib.parse import urlparse

import httpx

logger = logging.getLogger(__name__)

LocalServerType = Literal["ollama", "lm-studio", "llamacpp", "vllm"]

LOCAL_MODEL_HOSTS = {"localhost", "127.0.0.1", "0.0.0.0", "::1", "host.docker.internal"}
CONTAINER_LOCAL_SUFFIXES = (
    ".docker.internal",
    ".podman.internal",
    ".lima.internal",
)
TAILSCALE_CGNAT = ipaddress.ip_network("100.64.0.0/10")
LOCAL_METADATA_CACHE_TTL_SECONDS = 300.0

_CACHE: dict[tuple[Any, ...], tuple[float, Any]] = {}


def _cache_get(key: tuple[Any, ...]) -> Any | None:
    item = _CACHE.get(key)
    if not item:
        return None
    created, value = item
    if time.monotonic() - created > LOCAL_METADATA_CACHE_TTL_SECONDS:
        _CACHE.pop(key, None)
        return None
    return value


def _cache_set(key: tuple[Any, ...], value: Any) -> Any:
    _CACHE[key] = (time.monotonic(), value)
    return value


def normalize_base_url(base_url: str | None) -> str:
    return str(base_url or "").strip().rstrip("/")


def server_root(base_url: str | None) -> str:
    root = normalize_base_url(base_url)
    if root.endswith("/v1"):
        root = root[:-3].rstrip("/")
    return root


def is_local_llm_base_url(base_url: str | None) -> bool:
    """Return True for loopback, container-local, private LAN, and Tailscale URLs."""
    clean = normalize_base_url(base_url)
    if not clean:
        return False
    parse_target = clean if "://" in clean else f"http://{clean}"
    try:
        parsed = urlparse(parse_target)
    except ValueError:
        return False
    host = (parsed.hostname or "").lower()
    if not host:
        return False
    if host in LOCAL_MODEL_HOSTS:
        return True
    if any(host.endswith(suffix) for suffix in CONTAINER_LOCAL_SUFFIXES):
        return True
    try:
        addr = ipaddress.ip_address(host)
    except ValueError:
        return False
    if addr.is_private or addr.is_loopback or addr.is_link_local:
        return True
    return isinstance(addr, ipaddress.IPv4Address) and addr in TAILSCALE_CGNAT


def _auth_headers(api_key: str | None) -> dict[str, str]:
    token = str(api_key or "").strip()
    if token:
        return {"Authorization": f"Bearer {token}"}
    return {}


def _strip_provider_prefix(model: str) -> str:
    prefix, sep, remainder = str(model or "").partition(":")
    if sep and prefix.lower() in {
        "local",
        "ollama",
        "lmstudio",
        "lm-studio",
        "llamacpp",
        "llama.cpp",
        "vllm",
    }:
        return remainder
    return str(model or "")


def _model_id_matches(candidate: str | None, requested: str) -> bool:
    candidate = str(candidate or "").strip()
    requested = str(requested or "").strip()
    if not candidate or not requested:
        return False
    if candidate == requested:
        return True
    return candidate.split("/")[-1] == requested.split("/")[-1]


def _parse_ollama_parameters_num_ctx(parameters: Any) -> int | None:
    if not isinstance(parameters, str):
        return None
    for line in parameters.splitlines():
        if "num_ctx" not in line:
            continue
        match = re.search(r"\bnum_ctx\b\s+(\d+)", line)
        if not match:
            continue
        try:
            value = int(match.group(1))
        except ValueError:
            continue
        if value > 0:
            return value
    return None


def _context_from_model_info(model_info: Any) -> int | None:
    if not isinstance(model_info, dict):
        return None
    for key, value in model_info.items():
        if "context_length" not in str(key):
            continue
        try:
            parsed = int(value)
        except (TypeError, ValueError):
            continue
        if parsed > 0:
            return parsed
    return None


def detect_local_server_type(
    base_url: str | None,
    *,
    api_key: str | None = None,
    timeout: float = 2.0,
) -> LocalServerType | None:
    """Probe a local OpenAI-compatible endpoint for its native server type."""
    root = server_root(base_url)
    if not root or not is_local_llm_base_url(root):
        return None
    key = ("server_type", root, bool(api_key))
    cached = _cache_get(key)
    if cached is not None:
        return cached

    headers = _auth_headers(api_key)
    detected: LocalServerType | None = None
    try:
        with httpx.Client(timeout=timeout, headers=headers) as client:
            try:
                response = client.get(f"{root}/api/v1/models")
                if response.status_code == 200:
                    detected = "lm-studio"
            except httpx.HTTPError:
                pass

            if detected is None:
                try:
                    response = client.get(f"{root}/api/tags")
                    if response.status_code == 200 and isinstance(response.json(), dict):
                        if "models" in response.json():
                            detected = "ollama"
                except (ValueError, httpx.HTTPError):
                    pass

            if detected is None:
                try:
                    response = client.get(f"{root}/v1/props")
                    if response.status_code != 200:
                        response = client.get(f"{root}/props")
                    if response.status_code == 200 and "default_generation_settings" in response.text:
                        detected = "llamacpp"
                except httpx.HTTPError:
                    pass

            if detected is None:
                try:
                    response = client.get(f"{root}/version")
                    if response.status_code == 200 and isinstance(response.json(), dict):
                        if "version" in response.json():
                            detected = "vllm"
                except (ValueError, httpx.HTTPError):
                    pass
    except Exception as exc:  # noqa: BLE001 - probe failures are non-fatal.
        logger.debug("Local LLM server detection failed for %s: %s", root, exc)

    return _cache_set(key, detected)


def query_ollama_num_ctx(
    model: str,
    base_url: str | None,
    *,
    api_key: str | None = None,
    timeout: float = 3.0,
) -> int | None:
    """Return Ollama runtime num_ctx, falling back to GGUF context metadata."""
    model = _strip_provider_prefix(model)
    root = server_root(base_url)
    if not model or not root:
        return None
    key = ("ollama_num_ctx", root, model, bool(api_key))
    cached = _cache_get(key)
    if cached is not None:
        return cached

    detected: int | None = None
    try:
        with httpx.Client(timeout=timeout, headers=_auth_headers(api_key)) as client:
            response = client.post(f"{root}/api/show", json={"name": model})
            if response.status_code == 200:
                body = response.json()
                detected = (
                    _parse_ollama_parameters_num_ctx(body.get("parameters"))
                    or _context_from_model_info(body.get("model_info"))
                )
    except Exception as exc:  # noqa: BLE001 - probe failures are non-fatal.
        logger.debug("Ollama num_ctx probe failed for %s at %s: %s", model, root, exc)

    return _cache_set(key, detected)


def query_llamacpp_context_length(
    base_url: str | None,
    *,
    api_key: str | None = None,
    timeout: float = 3.0,
) -> int | None:
    root = server_root(base_url)
    if not root:
        return None
    key = ("llamacpp_context", root, bool(api_key))
    cached = _cache_get(key)
    if cached is not None:
        return cached
    detected: int | None = None
    try:
        with httpx.Client(timeout=timeout, headers=_auth_headers(api_key)) as client:
            response = client.get(f"{root}/v1/props")
            if response.status_code != 200:
                response = client.get(f"{root}/props")
            if response.status_code == 200:
                settings = response.json().get("default_generation_settings") or {}
                value = settings.get("n_ctx")
                if value:
                    detected = int(value)
    except Exception as exc:  # noqa: BLE001
        logger.debug("llama.cpp context probe failed for %s: %s", root, exc)
    return _cache_set(key, detected if detected and detected > 0 else None)


def query_local_context_length(
    model: str,
    base_url: str | None,
    *,
    api_key: str | None = None,
    server_type: LocalServerType | None = None,
    timeout: float = 3.0,
) -> int | None:
    """Query common local servers for the selected model's context length."""
    model = _strip_provider_prefix(model)
    root = server_root(base_url)
    if not model or not root or not is_local_llm_base_url(root):
        return None
    server_type = server_type or detect_local_server_type(root, api_key=api_key)
    key = ("context", root, model, server_type, bool(api_key))
    cached = _cache_get(key)
    if cached is not None:
        return cached

    detected: int | None = None
    headers = _auth_headers(api_key)
    try:
        with httpx.Client(timeout=timeout, headers=headers) as client:
            if server_type == "ollama":
                detected = query_ollama_num_ctx(model, root, api_key=api_key, timeout=timeout)
            elif server_type == "lm-studio":
                response = client.get(f"{root}/api/v1/models")
                if response.status_code == 200:
                    for item in response.json().get("models", []) or []:
                        if not (
                            _model_id_matches(item.get("key"), model)
                            or _model_id_matches(item.get("id"), model)
                        ):
                            continue
                        for instance in item.get("loaded_instances", []) or []:
                            config = instance.get("config") if isinstance(instance, dict) else None
                            value = config.get("context_length") if isinstance(config, dict) else None
                            if value:
                                detected = int(value)
                                break
                        break
            elif server_type == "llamacpp":
                detected = query_llamacpp_context_length(root, api_key=api_key, timeout=timeout)

            if detected is None:
                response = client.get(f"{root}/v1/models/{model}")
                if response.status_code == 200:
                    body = response.json()
                    value = (
                        body.get("max_model_len")
                        or body.get("context_length")
                        or body.get("max_tokens")
                    )
                    if value:
                        detected = int(value)

            if detected is None:
                response = client.get(f"{root}/v1/models")
                if response.status_code == 200:
                    for item in response.json().get("data", []) or []:
                        if not _model_id_matches(item.get("id"), model):
                            continue
                        value = (
                            item.get("max_model_len")
                            or item.get("context_length")
                            or item.get("max_tokens")
                        )
                        if value:
                            detected = int(value)
                            break
    except Exception as exc:  # noqa: BLE001
        logger.debug("Local context probe failed for %s at %s: %s", model, root, exc)

    return _cache_set(key, detected if detected and detected > 0 else None)
