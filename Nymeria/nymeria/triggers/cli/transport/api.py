"""REST/SSE-backed CLI transport."""

from __future__ import annotations

import copy
import os
import uuid
from collections.abc import AsyncIterator, Callable, Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any, Literal, Protocol

from ....triggers.api_client import NymeriaAPIClient
from ..credentials import CLIConnectionProfile, load_cli_config
from ..events import ErrorEvent, NormalizedEvent, normalize_stream_event
from .base import AgentClient, Attachment
from .disconnected import DisconnectedAgentClient

TransportMode = Literal["api", "local", "auto"]

DEFAULT_API_URL = "http://localhost:8000"


class CLITransportRuntimeConfig(Protocol):
    """Subset of ``CLIRuntimeConfig`` needed for transport selection."""

    transport: TransportMode
    api_url: str | None
    api_key: str | None
    user_id: str
    user_id_explicit: bool


@dataclass(frozen=True, slots=True)
class APIConnectionConfig:
    """Resolved API connection settings for the CLI transport."""

    api_url: str = DEFAULT_API_URL
    api_key: str | None = None
    user_id: str = "default"
    explicit_api_url: bool = False
    explicit_api_key: bool = False
    api_url_source: str = "default"
    api_key_source: str = ""
    user_id_source: str = "default"

    @property
    def has_api_key(self) -> bool:
        return bool(self.api_key and self.api_key.strip())

    @property
    def explicitly_configured(self) -> bool:
        return self.explicit_api_url or self.explicit_api_key

    @property
    def from_saved_profile(self) -> bool:
        return self.api_key_source == "saved" or self.api_url_source == "saved"


@dataclass(slots=True)
class APITransportStartupError(RuntimeError):
    """Structured startup error for API transport selection failures."""

    message: str
    code: str
    api_url: str
    status_code: int | None = None
    details: dict[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        RuntimeError.__init__(self, self.message)

    def as_event(self, *, thread_id: str | None = None) -> ErrorEvent:
        """Represent the startup failure as a normalized CLI error event."""

        details = {
            "api_url": self.api_url,
            **copy.deepcopy(self.details),
        }
        if self.status_code is not None:
            details["status_code"] = self.status_code
        return ErrorEvent(
            thread_id=thread_id,
            content=self.message,
            code=self.code,
            details=details,
        )


class APIAgentClient:
    """CLI ``AgentClient`` implementation backed by ``NymeriaAPIClient``."""

    def __init__(
        self,
        api: NymeriaAPIClient,
        *,
        base_url: str | None = None,
        default_user_id: str = "default",
    ) -> None:
        self.api = api
        self.base_url = (base_url or getattr(api, "base_url", DEFAULT_API_URL)).rstrip("/")
        self.default_user_id = default_user_id or "default"

    @property
    def connection_label(self) -> str:
        return f"api {self.base_url}"

    async def close(self) -> None:
        """Close the wrapped API client's HTTP pool when supported."""

        close = getattr(self.api, "close", None)
        if callable(close):
            await close()

    async def aclose(self) -> None:
        """httpx-style alias for close."""

        await self.close()

    async def stream_chat(
        self,
        message: str,
        thread_id: str,
        user_id: str = "default",
        attachments: Sequence[Attachment] | None = None,
        **options: Any,
    ) -> AsyncIterator[NormalizedEvent]:
        """Stream API SSE chat events as normalized CLI events."""

        selected_user_id = self._selected_user_id(user_id)
        try:
            async for raw_event in self.api.chat_stream(
                message=message,
                thread_id=thread_id,
                user_id=selected_user_id,
                attachments=_attachment_dicts(attachments),
                is_self_invoke=bool(options.get("is_self_invoke", False)),
                trigger_override=options.get("trigger_override"),
                force_unsupported_attachments=bool(
                    options.get("force_unsupported_attachments", False)
                ),
            ):
                yield normalize_stream_event(raw_event, default_thread_id=thread_id)
        except Exception as exc:  # noqa: BLE001 - stream path must report errors.
            yield _error_event_from_exception(
                exc,
                api_url=self.base_url,
                thread_id=thread_id,
                default_code="api_transport_error",
            )

    async def stream_autonomous(
        self,
        user_id: str = "default",
        *,
        client_id: str | None = None,
    ) -> AsyncIterator[NormalizedEvent]:
        """Stream API autonomous SSE events as normalized CLI events."""

        selected_user_id = self._selected_user_id(user_id)
        try:
            async for raw_event in self.api.autonomous_stream(
                user_id=selected_user_id,
                act_as=selected_user_id,
                client_id=client_id,
            ):
                yield normalize_stream_event(raw_event)
        except Exception as exc:  # noqa: BLE001 - background stream reports status.
            yield _error_event_from_exception(
                exc,
                api_url=self.base_url,
                thread_id=None,
                default_code="api_autonomous_stream_error",
            )

    async def stop(
        self,
        thread_id: str,
        user_id: str | None = None,
    ) -> Mapping[str, Any]:
        return await self.api.stop(thread_id, user_id=self._selected_user_id(user_id))

    async def get_history(
        self,
        thread_id: str,
        user_id: str | None = None,
        *,
        include_internal: bool = False,
        **_options: Any,
    ) -> Mapping[str, Any]:
        return await self.api.get_history(
            thread_id,
            include_internal=include_internal,
            user_id=self._selected_user_id(user_id),
        )

    async def list_threads(
        self,
        user_id: str = "default",
    ) -> Sequence[Mapping[str, Any]]:
        return await self.api.list_threads(self._selected_user_id(user_id))

    async def list_thread_teams(
        self,
        user_id: str = "default",
    ) -> Sequence[Mapping[str, Any]]:
        return await self.api.list_thread_teams(self._selected_user_id(user_id))

    async def list_todos(
        self,
        user_id: str = "default",
        *,
        filter_status: str | None = None,
        thread_id: str | None = None,
    ) -> Sequence[Mapping[str, Any]]:
        return await self.api.list_todos(
            self._selected_user_id(user_id),
            filter_status=filter_status,
            thread_id=thread_id,
        )

    async def list_triggers(
        self,
        user_id: str = "default",
        *,
        enabled_only: bool = False,
        thread_id: str | None = None,
    ) -> Sequence[Mapping[str, Any]]:
        return await self.api.list_triggers(
            self._selected_user_id(user_id),
            enabled_only=enabled_only,
            thread_id=thread_id,
        )

    async def get_context_stats(
        self,
        thread_id: str,
        user_id: str | None = None,
    ) -> Mapping[str, Any]:
        return await self.api.get_context_stats(
            thread_id,
            user_id=self._selected_user_id(user_id),
        )

    async def create_thread(
        self,
        user_id: str = "default",
        *,
        thread_id: str | None = None,
        title: str | None = None,
    ) -> Mapping[str, Any]:
        selected_user_id = self._selected_user_id(user_id)
        selected_thread_id = thread_id or str(uuid.uuid4())[:8]
        claimed = await self.api.claim_thread(selected_thread_id, selected_user_id)
        if title is None:
            return {
                "thread_id": selected_thread_id,
                "title": "New Chat",
                "title_source": "default",
                **copy.deepcopy(dict(claimed)),
            }
        return await self.api.update_thread_metadata(
            selected_thread_id,
            selected_user_id,
            title=title,
        )

    async def update_thread_metadata(
        self,
        thread_id: str,
        user_id: str | None = None,
        *,
        title: str | None = None,
        pinned: bool | None = None,
    ) -> Mapping[str, Any]:
        return await self.api.update_thread_metadata(
            thread_id,
            self._selected_user_id(user_id),
            title=title,
            pinned=pinned,
        )

    async def delete_thread(
        self,
        thread_id: str,
        user_id: str | None = None,
    ) -> Mapping[str, Any]:
        return await self.api.delete_thread(
            thread_id,
            user_id=self._selected_user_id(user_id),
        )

    async def rewind_thread(
        self,
        thread_id: str,
        steps: int = 1,
        user_id: str | None = None,
    ) -> Mapping[str, Any]:
        return await self.api.rewind_thread(
            thread_id,
            steps=steps,
            user_id=self._selected_user_id(user_id),
        )

    def _selected_user_id(self, user_id: str | None) -> str:
        if user_id and (user_id != "default" or self.default_user_id == "default"):
            return user_id
        return self.default_user_id


def resolve_api_connection_config(
    runtime_config: CLITransportRuntimeConfig | None = None,
    *,
    environ: Mapping[str, str] | None = None,
    saved_profile: CLIConnectionProfile | None = None,
) -> APIConnectionConfig:
    """Resolve API URL/key from CLI flags, env, saved profile, then defaults."""

    env = os.environ if environ is None else environ
    if saved_profile is None and environ is None:
        saved_profile = load_cli_config().active

    cli_url = _clean_optional(getattr(runtime_config, "api_url", None))
    cli_key = _clean_optional(getattr(runtime_config, "api_key", None))
    env_url = _clean_optional(env.get("NYMERIA_API_URL"))
    env_key = (
        _clean_optional(env.get("NYMERIA_CLI_API_KEY"))
        or _clean_optional(env.get("NYMERIA_SERVICE_TOKEN"))
        or _clean_optional(env.get("NYMERIA_API_KEY"))
    )
    saved_url = _clean_optional(saved_profile.api_url if saved_profile else None)
    saved_key = _clean_optional(saved_profile.api_key if saved_profile else None)

    api_url, api_url_source = _select_value(
        (cli_url, "flag"),
        (env_url, "env"),
        (saved_url, "saved"),
        (DEFAULT_API_URL, "default"),
    )
    api_key, api_key_source = _select_value(
        (cli_key, "flag"),
        (env_key, "env"),
        (saved_key, "saved"),
    )

    runtime_user_id = getattr(runtime_config, "user_id", "default") or "default"
    user_id_explicit = bool(getattr(runtime_config, "user_id_explicit", False)) or (
        runtime_user_id != "default"
    )
    saved_user_id = _clean_optional(saved_profile.user_id if saved_profile else None)
    if user_id_explicit:
        user_id = runtime_user_id
        user_id_source = "flag"
    elif saved_user_id:
        user_id = saved_user_id
        user_id_source = "saved"
    else:
        user_id = runtime_user_id
        user_id_source = "default"

    return APIConnectionConfig(
        api_url=api_url.rstrip("/"),
        api_key=api_key,
        user_id=user_id,
        explicit_api_url=bool(cli_url or env_url),
        explicit_api_key=bool(cli_key or env_key),
        api_url_source=api_url_source,
        api_key_source=api_key_source,
        user_id_source=user_id_source,
    )


def create_api_agent_client(
    runtime_config: CLITransportRuntimeConfig | None = None,
    *,
    api_client_factory: Callable[..., Any] = NymeriaAPIClient,
    environ: Mapping[str, str] | None = None,
    saved_profile: CLIConnectionProfile | None = None,
) -> APIAgentClient:
    """Create an API transport from resolved runtime configuration."""

    config = resolve_api_connection_config(
        runtime_config,
        environ=environ,
        saved_profile=saved_profile,
    )
    if not config.has_api_key:
        raise APITransportStartupError(
            "API transport requires an API token. Run /login or pass --api-key.",
            code="api_auth_missing",
            api_url=config.api_url,
        )

    api = api_client_factory(base_url=config.api_url, api_key=config.api_key)
    return APIAgentClient(
        api,
        base_url=config.api_url,
        default_user_id=config.user_id,
    )


async def validate_api_agent_client(
    config: APIConnectionConfig,
    *,
    api_client_factory: Callable[..., Any] = NymeriaAPIClient,
) -> APIAgentClient:
    """Create and validate an API client with health and identity checks."""

    if not config.has_api_key:
        raise APITransportStartupError(
            "API transport requires an API token. Run /login or pass --api-key.",
            code="api_auth_missing",
            api_url=config.api_url,
        )

    api = api_client_factory(base_url=config.api_url, api_key=config.api_key)
    api_client = APIAgentClient(
        api,
        base_url=config.api_url,
        default_user_id=config.user_id,
    )

    try:
        healthy = await api.health()
        if not healthy:
            raise APITransportStartupError(
                f"Nymeria API at {config.api_url} did not pass its health check.",
                code="api_unavailable",
                api_url=config.api_url,
            )

        await api.get_me(act_as=config.user_id or None)
    except APITransportStartupError:
        await _close_api_client(api)
        raise
    except Exception as exc:  # noqa: BLE001 - startup must surface structured errors.
        await _close_api_client(api)
        raise _startup_error_from_exception(exc, api_url=config.api_url) from exc

    return api_client


async def select_agent_client(
    runtime_config: CLITransportRuntimeConfig,
    *,
    local_client: AgentClient | None = None,
    api_client_factory: Callable[..., Any] = NymeriaAPIClient,
    environ: Mapping[str, str] | None = None,
    saved_profile: CLIConnectionProfile | None = None,
) -> AgentClient:
    """Select API, disconnected, or explicit local transport."""

    mode = getattr(runtime_config, "transport", "auto")
    if mode == "local":
        if local_client is None:
            raise APITransportStartupError(
                "Local transport was requested but no local client is available.",
                code="local_transport_unavailable",
                api_url=DEFAULT_API_URL,
            )
        return local_client

    config = resolve_api_connection_config(
        runtime_config,
        environ=environ,
        saved_profile=saved_profile,
    )
    if not config.has_api_key:
        return DisconnectedAgentClient(default_user_id=config.user_id)

    try:
        return await validate_api_agent_client(
            config,
            api_client_factory=api_client_factory,
        )
    except APITransportStartupError:
        if config.from_saved_profile and not config.explicitly_configured:
            return DisconnectedAgentClient(
                default_user_id=config.user_id,
                startup_error=(
                    "Saved CLI connection could not be validated. "
                    "Run /login to reconnect."
                ),
            )
        raise


def _attachment_dicts(
    attachments: Sequence[Attachment] | None,
) -> list[dict[str, Any]] | None:
    if attachments is None:
        return None
    return [copy.deepcopy(dict(item)) for item in attachments]


def _clean_optional(value: Any) -> str | None:
    if not isinstance(value, str):
        return None
    value = value.strip()
    return value or None


def _select_value(*candidates: tuple[str | None, str]) -> tuple[str, str]:
    for value, source in candidates:
        if value:
            return value, source
    return "", ""


async def _close_api_client(api: Any) -> None:
    close = getattr(api, "close", None)
    if callable(close):
        await close()


def _startup_error_from_exception(exc: Exception, *, api_url: str) -> APITransportStartupError:
    status_code = _status_code(exc)
    if _is_auth_error(exc):
        return APITransportStartupError(
            _http_error_message(exc, fallback="API authentication failed."),
            code="api_auth_error",
            api_url=api_url,
            status_code=status_code,
            details=_exception_details(exc),
        )
    return APITransportStartupError(
        _http_error_message(exc, fallback=f"Could not connect to Nymeria API at {api_url}."),
        code="api_connection_error",
        api_url=api_url,
        status_code=status_code,
        details=_exception_details(exc),
    )


def _error_event_from_exception(
    exc: Exception,
    *,
    api_url: str,
    thread_id: str | None,
    default_code: str,
) -> ErrorEvent:
    code = "api_auth_error" if _is_auth_error(exc) else default_code
    return ErrorEvent(
        thread_id=thread_id,
        content=_http_error_message(exc, fallback="API transport stream failed."),
        code=code,
        details={
            "api_url": api_url,
            **_exception_details(exc),
        },
    )


def _is_auth_error(exc: Exception) -> bool:
    return _status_code(exc) in {401, 403, 404}


def _status_code(exc: Exception) -> int | None:
    response = getattr(exc, "response", None)
    status_code = getattr(response, "status_code", None)
    return status_code if isinstance(status_code, int) else None


def _http_error_message(exc: Exception, *, fallback: str) -> str:
    status_code = _status_code(exc)
    detail = _response_detail(exc)
    if status_code in {401, 403}:
        return f"API authentication failed: {detail or 'check the configured API token.'}"
    if status_code == 404:
        return f"API identity check failed: {detail or 'act-as user was not found.'}"
    if status_code:
        return f"API request failed with HTTP {status_code}: {detail or str(exc)}"
    return str(exc) or fallback


def _response_detail(exc: Exception) -> str:
    response = getattr(exc, "response", None)
    if response is None:
        return ""
    try:
        data = response.json()
    except Exception:  # noqa: BLE001 - best-effort diagnostic.
        text = getattr(response, "text", "")
        return str(text).strip()
    if isinstance(data, Mapping):
        detail = data.get("detail") or data.get("message") or data.get("error")
        return str(detail).strip() if detail is not None else ""
    return str(data).strip()


def _exception_details(exc: Exception) -> dict[str, Any]:
    details: dict[str, Any] = {"error_type": exc.__class__.__name__}
    status_code = _status_code(exc)
    if status_code is not None:
        details["status_code"] = status_code
    detail = _response_detail(exc)
    if detail:
        details["detail"] = detail
    return details


__all__ = [
    "APIAgentClient",
    "APIConnectionConfig",
    "APITransportStartupError",
    "DEFAULT_API_URL",
    "create_api_agent_client",
    "resolve_api_connection_config",
    "select_agent_client",
]
