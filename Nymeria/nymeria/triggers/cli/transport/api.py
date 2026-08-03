"""REST/SSE-backed CLI transport."""

from __future__ import annotations

import asyncio
import copy
import os
import uuid
from collections.abc import AsyncIterator, Callable, Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Literal, Protocol
from urllib.parse import urlsplit

import httpx

from ....triggers.api_client import NymeriaAPIClient
from ..credentials import CLIConnectionProfile, load_cli_config
from ..events import DoneEvent, ErrorEvent, NormalizedEvent, normalize_stream_event
from .base import AgentClient, Attachment
from .disconnected import DisconnectedAgentClient

TransportMode = Literal["api", "local", "auto"]

DEFAULT_API_URL = "http://localhost:8000"

# Exception shapes that mean "the connection dropped", not "the request was
# rejected": these trigger the turn re-attach recovery path in stream_chat.
# httpx.TransportError covers connect/read/write errors and timeouts;
# HTTPStatusError deliberately stays on the plain error path.
CONNECTION_ERRORS: tuple[type[Exception], ...] = (httpx.TransportError,)

# Turn-recovery backoff (mirrors the GUI clients' reconnect posture).
RECOVERY_BASE_DELAY_SECONDS = 2.0
RECOVERY_MAX_DELAY_SECONDS = 15.0
RECOVERY_MAX_ATTEMPTS = 20


def _track_turn_cursor(
    raw_event: Mapping[str, Any],
    turn_id: str | None,
    last_seq: int,
) -> tuple[str | None, int]:
    """Advance the (turn_id, last_seq) re-attach cursor from a raw event."""

    if raw_event.get("type") == "turn_started":
        new_id = raw_event.get("turn_id")
        if isinstance(new_id, str) and new_id:
            turn_id = new_id
    seq = raw_event.get("seq")
    if isinstance(seq, int) and seq > last_seq:
        last_seq = seq
    return turn_id, last_seq

# Startup-failure codes that mean "the backend is not up yet" rather than "the
# credentials are wrong". Only these are worth retrying automatically from a
# saved profile; an auth failure needs a fresh /login, not a silent retry.
RECONNECTABLE_STARTUP_CODES = frozenset({"api_unavailable", "api_connection_error"})


class CLITransportRuntimeConfig(Protocol):
    """Subset of ``CLIRuntimeConfig`` needed for transport selection."""

    @property
    def transport(self) -> TransportMode: ...
    @property
    def api_url(self) -> str | None: ...
    @property
    def api_key(self) -> str | None: ...
    @property
    def user_id(self) -> str: ...
    @property
    def user_id_explicit(self) -> bool: ...


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

    supports_autonomous_stream = True

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

        close_attr = getattr(self.api, "close", None)
        if callable(close_attr):
            close: Any = close_attr
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
        """Stream API SSE chat events as normalized CLI events.

        Connection drops mid-turn do not end the turn locally: the backend
        keeps executing and buffers the turn's events, so this generator
        re-attaches via GET /threads/{id}/turn/stream and resumes from the
        last seen ``seq`` (suffix-only replay: a terminal cannot unprint the
        prefix the way the GUI clients rebuild their reply bubble). Only when
        the turn is genuinely gone (API restart, buffer expired) does it
        yield an honest ``turn_lost`` error event.
        """

        selected_user_id = self._selected_user_id(user_id)
        turn_id: str | None = None
        last_seq = 0
        terminal_seen = False
        prompt_queued_seen = False
        dispatched_to: dict[str, Any] | None = None
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
                turn_id, last_seq = _track_turn_cursor(raw_event, turn_id, last_seq)
                event_type = raw_event.get("type")
                if event_type == "turn_started":
                    continue  # identity marker, not a renderable event
                if event_type in ("done", "error"):
                    terminal_seen = True
                elif event_type == "prompt_queued":
                    # ONLY prompt_queued proves the queued outcome. The legacy
                    # bare "queued" event also fires on paths that still become
                    # the holder (lock handoff / queue-closing races: queued,
                    # then turn_started, then a full holder turn), so treating
                    # it as a queue marker would re-suppress recovery for
                    # exactly the stuck-spinner case this path exists to fix.
                    prompt_queued_seen = True
                elif event_type == "dispatched":
                    raw_dispatch = raw_event.get("dispatched_to")
                    dispatched_to = (
                        dict(raw_dispatch) if isinstance(raw_dispatch, Mapping) else {}
                    )
                yield normalize_stream_event(raw_event, default_thread_id=thread_id)
        except CONNECTION_ERRORS as exc:
            if terminal_seen:
                # The turn already finished on the wire; a drop during the
                # server's stream close has nothing left to recover, and the
                # recovery loop would degrade it to a spurious turn_lost.
                return
            recovery_cause = exc
        except Exception as exc:  # noqa: BLE001 - stream path must report errors.
            yield _error_event_from_exception(
                exc,
                api_url=self.base_url,
                thread_id=thread_id,
                default_code="api_transport_error",
            )
            return
        else:
            if terminal_seen or prompt_queued_seen or turn_id is None:
                # Nothing to recover: the turn ended properly, or this stream
                # was a queued/ack shape that never held a turn. A genuinely
                # queued composer also never sees a turn_started (the holder's
                # is route-synthesized on the holder stream only), so the
                # turn_id guard covers it; prompt_queued is defense in depth.
                return
            if dispatched_to is not None:
                # Dispatched turn (/quick, thread mention): the server buffers
                # it under the TARGET thread while this stream is tagged with
                # the origin thread, so re-attach recovery here would poll the
                # wrong thread and end in a spurious turn_lost AFTER output
                # already rendered. Finalize locally instead: a dispatched
                # done is ignored for context/usage accounting by the reducer,
                # so this only clears the streaming state.
                yield DoneEvent(
                    thread_id=thread_id,
                    status="complete",
                    dispatched_to=dispatched_to,
                )
                return
            # Clean stream end WITHOUT a terminal event on a holder turn we
            # were attached to (turn_started seen). The server buffers every
            # holder turn's tail including its done frame, and it withholds
            # the wire copy whenever a turn-end disconnect probe latched (a
            # probe that can misfire while the socket is actually fine), so
            # a silent end here does not mean the turn is over for the UI:
            # without a reduced terminal the spinner sticks on "Streaming".
            # Drain the buffered tail via the same re-attach recovery used
            # for dropped connections.
            recovery_cause = RuntimeError(
                "chat stream ended without a terminal event"
            )

        # Recovery: the POST /chat connection dropped mid-turn, or ended
        # cleanly while withholding the turn's terminal event.
        async for event in self._recover_interrupted_turn(
            thread_id=thread_id,
            user_id=selected_user_id,
            turn_id=turn_id,
            last_seq=last_seq,
            cause=recovery_cause,
        ):
            yield event

    async def _recover_interrupted_turn(
        self,
        *,
        thread_id: str,
        user_id: str,
        turn_id: str | None,
        last_seq: int,
        cause: Exception,
    ) -> AsyncIterator[NormalizedEvent]:
        """Re-attach to a dropped turn, resuming output from ``last_seq``.

        Bounded backoff loop: each pass polls thread status, re-attaches when
        the turn's buffer is available, and resets the attempt budget whenever
        replayed events actually flow (so a long turn surviving several drops
        is not abandoned). Ends silently after the turn's terminal event, or
        with a ``turn_lost`` error event when the turn cannot be recovered.
        """

        attempt = 0
        while attempt < RECOVERY_MAX_ATTEMPTS:
            attempt += 1
            status: Mapping[str, Any] | None = None
            try:
                raw_status = await self.api.get_thread_status(thread_id, user_id=user_id)
                status = raw_status if isinstance(raw_status, Mapping) else None
            except Exception:  # noqa: BLE001 - backend unreachable; keep retrying.
                status = None

            if status is not None:
                raw_turn = status.get("turn")
                turn = raw_turn if isinstance(raw_turn, Mapping) else None
                turn_matches = turn is not None and (
                    not turn_id or turn.get("turn_id") == turn_id
                )
                if turn is not None and turn_matches:
                    outcome: str | None = None
                    progressed = False
                    try:
                        async for raw_event in self.api.reattach_turn_stream(
                            thread_id,
                            user_id=user_id,
                            turn_id=str(turn.get("turn_id") or "") or None,
                            from_seq=last_seq,
                        ):
                            event_type = raw_event.get("type")
                            if event_type in ("turn_attach", "turn_started"):
                                continue
                            if event_type == "turn_replay_gap":
                                # Overflow evicted events past our cursor
                                # MID-stream (the attach-time 410 only covers
                                # pre-existing gaps); the remainder cannot be
                                # replayed faithfully. The server ends the
                                # stream right after this frame.
                                outcome = "gone"
                                continue
                            turn_id, last_seq = _track_turn_cursor(
                                raw_event, turn_id, last_seq
                            )
                            progressed = True
                            yield normalize_stream_event(
                                raw_event, default_thread_id=thread_id
                            )
                            if event_type in ("done", "error"):
                                outcome = "finished"
                    except CONNECTION_ERRORS:
                        outcome = None  # dropped again: back off and retry
                    except Exception as exc:  # noqa: BLE001
                        if _status_code(exc) in (404, 410):
                            # Buffer replaced/expired or replay gap: the rest
                            # of this turn cannot be recovered here.
                            outcome = "gone"
                        else:
                            outcome = None
                    else:
                        if outcome != "finished":
                            # Clean stream end without a terminal event: the
                            # turn's writer died without finishing.
                            outcome = "gone"
                    if outcome == "finished":
                        return
                    if outcome == "gone":
                        break
                    if progressed:
                        attempt = 0
                elif not status.get("processing"):
                    # Turn is gone (API restart, buffer expired, or another
                    # turn already ran).
                    break
                # else: thread busy with an unattachable turn; keep waiting.

            await asyncio.sleep(
                min(RECOVERY_BASE_DELAY_SECONDS * attempt, RECOVERY_MAX_DELAY_SECONDS)
            )

        yield ErrorEvent(
            thread_id=thread_id,
            content=(
                "Lost connection while this reply was streaming and could not "
                "rejoin it. The turn may still finish on the server; its result "
                "is saved to the thread history."
            ),
            code="turn_lost",
            details={"api_url": self.base_url, "cause": str(cause)},
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
        claimed = await self.api.claim_thread(
            selected_thread_id,
            selected_user_id,
            title=title,
            platform="cli",
        )
        payload: dict[str, Any] = {
            "thread_id": selected_thread_id,
            "title": title or "New Chat",
            "title_source": "user" if title else "default",
            "platform": "cli",
        }
        payload.update(copy.deepcopy(dict(claimed)))
        return payload

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

    async def branch_thread(
        self,
        thread_id: str,
        user_id: str | None = None,
        *,
        title: str | None = None,
        from_message_index: int | None = None,
    ) -> Mapping[str, Any]:
        return await self.api.branch_thread(
            thread_id,
            title=title,
            from_message_index=from_message_index,
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

    async def execute_command(
        self,
        command: str,
        *,
        thread_id: str | None = None,
        source: str = "cli",
        actor: str | None = None,
        surface: str | None = None,
        user_id: str | None = None,
        supports_forms: bool = False,
    ) -> Mapping[str, Any]:
        selected_user_id = self._selected_user_id(user_id)
        return await self.api.execute_command(
            command,
            thread_id=thread_id,
            source=source,
            actor=actor,
            surface=surface,
            user_id=selected_user_id,
            supports_forms=supports_forms,
        )

    async def list_commands(
        self,
        *,
        source: str | None = None,
        actor: str | None = None,
        surface: str | None = None,
        user_id: str | None = None,
    ) -> Sequence[Mapping[str, Any]]:
        return await self.api.list_commands(
            source=source,
            actor=actor,
            surface=surface,
            user_id=self._selected_user_id(user_id),
        )

    async def post_cli_config_result(
        self,
        command_id: str,
        result: Mapping[str, Any],
        user_id: str | None = None,
    ) -> Mapping[str, Any]:
        """Ack a ``cli_config`` autonomous event (first ack wins server-side)."""
        return await self.api.post_cli_config_result(
            command_id,
            dict(result),
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
    except APITransportStartupError as exc:
        if config.from_saved_profile and not config.explicitly_configured:
            if exc.code in RECONNECTABLE_STARTUP_CODES:
                # Backend is simply not up yet: keep the saved profile so the
                # CLI can auto-reconnect (or /reconnect) once it comes up,
                # instead of forcing a full /login with url + token re-entry.
                # On a real launch (environ is None), also sniff for a backend
                # that moved to a different local port and hint at it.
                suggested = ""
                if environ is None:
                    suggested = await suggest_reachable_backend(
                        config.api_url,
                        api_client_factory=api_client_factory,
                    ) or ""
                return DisconnectedAgentClient(
                    default_user_id=config.user_id,
                    startup_error=(
                        f"Backend at {config.api_url} is not reachable yet. "
                        "Run /reconnect once it is up."
                    ),
                    reconnect_api_url=config.api_url,
                    reconnect_api_key=config.api_key or "",
                    reconnect_user_id=config.user_id,
                    suggested_url=suggested,
                )
            return DisconnectedAgentClient(
                default_user_id=config.user_id,
                startup_error=(
                    "Saved CLI connection could not be validated. "
                    "Run /login to reconnect."
                ),
            )
        raise


def _reconnect_config(client: DisconnectedAgentClient) -> APIConnectionConfig:
    """Rebuild a connection config from a placeholder's retained saved profile."""

    return APIConnectionConfig(
        api_url=client.reconnect_api_url,
        api_key=client.reconnect_api_key,
        user_id=client.reconnect_user_id,
        explicit_api_url=True,
        explicit_api_key=True,
        api_url_source="saved",
        api_key_source="saved",
        user_id_source="saved",
    )


async def attempt_saved_reconnect(
    client: AgentClient | None,
    *,
    api_client_factory: Callable[..., Any] = NymeriaAPIClient,
) -> APIAgentClient | None:
    """Re-validate the saved profile retained on a disconnected placeholder.

    Returns a live :class:`APIAgentClient` once the backend is reachable again,
    or ``None`` while it is still down (so a poller can try again later).
    Re-raises :class:`APITransportStartupError` for non-recoverable failures
    such as a revoked token, so the caller can stop polling and surface a
    "run /login" prompt rather than retrying a request that can only keep
    failing.
    """

    if not isinstance(client, DisconnectedAgentClient) or not client.can_reconnect:
        return None
    config = _reconnect_config(client)
    try:
        return await validate_api_agent_client(
            config,
            api_client_factory=api_client_factory,
        )
    except APITransportStartupError as exc:
        if exc.code in RECONNECTABLE_STARTUP_CODES:
            return None
        raise


LOOPBACK_HOSTS = frozenset({"127.0.0.1", "localhost", "::1", "0.0.0.0"})

# A non-secret placeholder bearer for the unauthenticated /health probe. The
# user's saved token is never sent to a candidate; we only need a non-empty
# value because an empty api_key builds an illegal "Bearer " header (httpx
# rejects it, so health() would silently fail and find nothing).
_PROBE_API_KEY = "health-probe"


def is_loopback_url(url: str) -> bool:
    """Whether ``url`` points at this machine (so local-only logic is safe)."""

    try:
        host = (urlsplit(url).hostname or "").casefold()
    except ValueError:
        return False
    return host in LOOPBACK_HOSTS


def _candidate_backend_urls(
    failed_url: str,
    *,
    environ: Mapping[str, str],
    config_path: Path | str | None = None,
) -> list[str]:
    """Curated, deduped loopback backend URLs to probe, most-likely first."""

    failed = failed_url.strip().rstrip("/")
    ordered: list[str] = []

    def add(url: str | None) -> None:
        if not url:
            return
        norm = url.strip().rstrip("/")
        if norm and norm != failed and norm not in ordered and is_loopback_url(norm):
            ordered.append(norm)

    # 1. The project's configured API port: the most authoritative local signal.
    try:
        from ....config import get_settings

        port = getattr(get_settings(), "api_port", None)
    except Exception:  # noqa: BLE001 - settings are a best-effort hint here.
        port = None
    if port:
        add(f"http://127.0.0.1:{port}")
    # 2. An explicit env override, if the user set one.
    add(_clean_optional(environ.get("NYMERIA_API_URL")))
    # 3. The conventional default.
    add("http://127.0.0.1:8000")
    # 4. Other saved CLI profiles the user has connected to before.
    try:
        for profile in load_cli_config(config_path).profiles.values():
            add(profile.api_url)
    except Exception:  # noqa: BLE001 - saved config is a best-effort hint here.
        pass
    return ordered


async def suggest_reachable_backend(
    failed_url: str,
    *,
    api_client_factory: Callable[..., Any] = NymeriaAPIClient,
    environ: Mapping[str, str] | None = None,
    config_path: Path | str | None = None,
) -> str | None:
    """Find a live Nymeria backend on a different local port after a failure.

    Probes a small curated candidate set (the project's configured ``api_port``,
    ``NYMERIA_API_URL``, the default 8000, and other saved profiles) with an
    unauthenticated ``/health`` check and returns the first URL that answers as
    Nymeria, or ``None``. Only runs for loopback targets, so a remote outage
    never triggers a local probe. Intended for one-shot failure surfaces (boot,
    ``/login``, ``/reconnect``), never the reconnect poll loop.
    """

    if not is_loopback_url(failed_url):
        return None
    env = os.environ if environ is None else environ
    for url in _candidate_backend_urls(failed_url, environ=env, config_path=config_path):
        try:
            api = api_client_factory(base_url=url, api_key=_PROBE_API_KEY)
        except Exception:  # noqa: BLE001 - a bad candidate is just "not it".
            continue
        try:
            if await api.health():
                return url
        except Exception:  # noqa: BLE001 - an unreachable/erroring candidate is skipped.
            continue
        finally:
            await _close_api_client(api)
    return None


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
    close_attr = getattr(api, "close", None)
    if callable(close_attr):
        close: Any = close_attr
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
    "RECONNECTABLE_STARTUP_CODES",
    "attempt_saved_reconnect",
    "create_api_agent_client",
    "is_loopback_url",
    "resolve_api_connection_config",
    "select_agent_client",
    "suggest_reachable_backend",
]
