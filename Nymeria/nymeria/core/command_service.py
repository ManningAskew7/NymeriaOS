"""Central markdown slash-command service.

The service owns the command registry, parses a raw slash-command string once,
and executes command bodies against a small backend interface. In-process
callers use :class:`CommandBackendClient`; the REST client remains only for
out-of-process compatibility shims.
"""

from __future__ import annotations

import asyncio
import difflib
import json
import logging
import os
import shlex
import uuid
import dataclasses
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any, Callable, Literal, Mapping, NoReturn, Optional
from urllib.parse import quote

import httpx

from ..config import get_settings
from .command_executor_aliases import AliasCommandsMixin
from .command_executor_cliproxy import CliproxyCommandsMixin
from .command_executor_context import ContextCommandsMixin
from .command_executor_llm import LLMCommandsMixin, model_select_form
from .command_executor_provider_setup import ProviderSetupCommandsMixin
from .command_executor_threads import ThreadCommandsMixin
from .command_forms import (
    CommandOutput,
    CommandResultLevel,
    command_data,
    command_error,
    command_info,
    command_success,
    command_warning,
    form_options_markdown,
    render_outcome,
    table_cell,
)
from .command_form_generation import generate_param_form
from .command_naming import validate_command_naming
from .command_option_resolvers import OPTION_RESOLVERS, resolve_models
from .command_params import (
    BoundArgs,
    CommandParam,
    bind_args,
    generated_usage,
    params_to_payload,
    validate_params,
)
from .registry_defaults import register_default_commands
from .time_utils import (
    ensure_aware_utc,
    format_user_time_compact,
    parse_tool_ttl,
    utc_now,
)

logger = logging.getLogger(__name__)

CommandSource = Literal["user", "agent", "cli"]
CommandActor = Literal["user", "agent", "system"]
CommandSurface = Literal[
    "desktop",
    "mobile",
    "cli",
    "discord",
    "telegram",
    "slack",
    "whatsapp",
    "teams",
    "twitch",
    "api",
    "agent",
]
CommandScope = Literal["global", "surface_local"]
CommandDangerLevel = Literal["safe", "normal", "dangerous"]
CommandExecutionKind = Literal["command", "chat_stream", "surface_local"]
# CommandResultLevel is imported from command_forms (the leaf that owns the
# CommandOutput contract); re-exported here for existing importers.

DEFAULT_GLOBAL_SURFACES: tuple[CommandSurface, ...] = (
    "desktop",
    "mobile",
    "cli",
    "discord",
    "telegram",
    "slack",
    "whatsapp",
    "teams",
    "api",
    "agent",
)

# The chat platforms, for blocked_surfaces declarations: every surface whose
# transport persists typed text in a third-party message history. Includes
# twitch (in the CommandSurface Literal but not DEFAULT_GLOBAL_SURFACES) so a
# future chat surface is blocked by construction rather than by remembering.
CHAT_PLATFORM_SURFACES: tuple[CommandSurface, ...] = (
    "discord",
    "telegram",
    "slack",
    "whatsapp",
    "teams",
    "twitch",
)

# Roots the agent actor may never dispatch. "ask" and "start" match no
# registered command today: they are bot-local chat entries (Telegram /start,
# the ask commands) pre-blocked so a future registration cannot quietly hand
# them to the agent; command_naming.AGENT_BLOCKED_UNREGISTERED records them
# and validate_registry fails on any OTHER unregistered entry.
AGENT_BLOCKED = {"ask", "stop", "clear", "restart", "compact", "start"}

# Command output budgets, per surface. Truncating a command result is a
# LAST RESORT, not a product limit: a clipped listing is silently wrong
# information, so a command whose ordinary output outgrows the compact
# budget gets SPLIT or filtered (the /provider list default view is the
# worked example), never left to truncate.
#
# Roomy surfaces render into a scrollback or a scrollable view: the CLI
# writes above its footer with native scrollback intact (mouse_support is
# off precisely so the wheel still scrolls it), and the GUIs render a
# scrollable card. The budget there is a runaway guard only, an order of
# magnitude above the largest listing the catalog can currently produce
# (`/provider list --all`, which the ratchet below measures).
OUTPUT_BUDGET_ROOMY = 100_000
# Everything else: the chat platforms (whose _send_text already chunks to
# the platform limit, so this bounds message COUNT rather than API
# validity), the agent surface (whose output is charged to the model's
# context), and unknown callers. 12k was already the /skills show budget,
# which is now simply the floor everywhere rather than a per-command
# exemption. tests/test_command_output_budget.py ratchets the
# catalog-driven listings against it (NOT every built-in: the ones whose
# size tracks user data or a live agent are vacuous under the fixture,
# and that test says so).
OUTPUT_BUDGET_COMPACT = 12_000
# Typed against the surface Literal so a typo or a renamed surface fails
# type-check here rather than silently downgrading that surface to the
# compact budget.
ROOMY_OUTPUT_SURFACES: frozenset[CommandSurface] = frozenset(
    {"cli", "desktop", "mobile", "api"}
)


def output_budget(surface: str | None) -> int:
    """Max characters a command result may carry on *surface*.

    Takes a loose ``str`` because surfaces arrive off the wire, where an
    unrecognized value is a real possibility rather than a type error. It
    lands on the compact budget: the conservative direction, since the
    roomy budget assumes a client that can scroll. Same for a legacy
    caller that sends no surface at all.
    """
    return (
        OUTPUT_BUDGET_ROOMY
        if surface in ROOMY_OUTPUT_SURFACES
        else OUTPUT_BUDGET_COMPACT
    )


@dataclass(frozen=True)
class CommandContext:
    user_id: str
    thread_id: str | None = None
    source: CommandSource = "user"
    actor: CommandActor | None = None
    surface: CommandSurface | None = None
    is_admin: bool | None = None
    via_act_as: bool = False
    # Capability flag: the caller renders declarative form payloads
    # (data["form"]). Only form-rendering clients send it (the Rich CLI);
    # execute() strips form payloads for everyone else, and the
    # missing-required bind rescue (generated pickers, backlog #110) only
    # fires when it is set. Default False so bots, GUIs, agents, and older
    # clients keep the pre-#110 wire behavior byte-for-byte.
    supports_forms: bool = False

    @property
    def effective_actor(self) -> CommandActor:
        if self.actor is not None:
            return self.actor
        return _actor_from_source(self.source)

    @property
    def effective_surface(self) -> CommandSurface | None:
        if self.surface is not None:
            return self.surface
        return _surface_from_source(self.source)


@dataclass(frozen=True)
class CommandResult:
    success: bool
    markdown: str
    command: str
    level: CommandResultLevel = "info"
    data: dict[str, Any] | None = None


@dataclass(frozen=True)
class CommandInfo:
    name: str
    description: str
    usage: str
    category: str
    subcommands: list[str] = field(default_factory=list)
    id: str = ""
    path: list[str] = field(default_factory=list)
    aliases: list[str] = field(default_factory=list)
    # The CALLER'S own user-defined aliases for this command (#133), present
    # only when list_commands ran with a user_id. Display and client-mirror
    # metadata: dispatch reads the store directly, never this field.
    user_aliases: list[str] = field(default_factory=list)
    scope: CommandScope = "global"
    surfaces: list[str] = field(default_factory=list)
    blocked_surfaces: list[str] = field(default_factory=list)
    blocked_reason: str | None = None
    agent_allowed: bool = True
    requires_thread: bool = False
    requires_admin: bool = False
    mutates_state: bool = False
    danger_level: CommandDangerLevel = "safe"
    execution_kind: CommandExecutionKind = "command"
    level: CommandResultLevel = "info"
    note: str | None = None
    examples: list[str] = field(default_factory=list)
    # None = no declared schema; [] = schema'd with zero arguments.
    params: list[dict[str, Any]] | None = None


@dataclass(frozen=True)
class CommandDefinition:
    id: str
    path: tuple[str, ...]
    description: str
    usage: str
    category: str
    aliases: tuple[tuple[str, ...], ...] = ()
    subcommands: tuple[str, ...] = ()
    scope: CommandScope = "global"
    surfaces: tuple[CommandSurface, ...] = DEFAULT_GLOBAL_SURFACES
    # ``surfaces`` filters DISCOVERY only (menus, /help, GET /commands);
    # execute() deliberately ignores it so bots can forward surface-hidden
    # subcommands like "/hook create". ``blocked_surfaces`` is the enforced
    # axis: execute() refuses these surfaces outright, with
    # ``blocked_reason`` rendered into the refusal copy.
    blocked_surfaces: tuple[CommandSurface, ...] = ()
    blocked_reason: str | None = None
    agent_allowed: bool = True
    requires_thread: bool = False
    requires_admin: bool = False
    mutates_state: bool = False
    danger_level: CommandDangerLevel = "safe"
    execution_kind: CommandExecutionKind = "command"
    note: str | None = None
    hidden: bool = False
    examples: tuple[str, ...] = ()
    # None = unadopted legacy command (handler hand-parses ``(args, rest)``);
    # a tuple, even empty, means the dispatcher binds and validates BEFORE
    # the handler runs and the handler receives ``BoundArgs``. ``()`` is the
    # strict zero-argument declaration: extras become usage errors.
    params: tuple[CommandParam, ...] | None = None

    @property
    def name(self) -> str:
        """The DISPLAYED spelling: hyphens, never underscores (#131 rule 4).

        Paths are stored underscore-normalized (``set_url``) because parsing
        folds ``-`` to ``_``, so both spellings resolve; every surface that
        SHOWS a name shows the hyphenated one, which is what
        ``generated_usage`` already rendered. Anything that needs the stored
        form must read ``path``, never split this.
        """
        return _display_path(self.path)

    @property
    def executable(self) -> bool:
        return self.execution_kind == "command"


@dataclass(frozen=True)
class ParsedCommand:
    tokens: tuple[str, ...]
    path: tuple[str, ...]
    args: list[str]
    rest: str
    definition: CommandDefinition | None
    matched_input_len: int = 0


@dataclass(frozen=True)
class SkillSlashResult:
    """A prepared `/skill` or `/kit` dispatch for the chat-stream surface.

    ``message`` is a plain BODY carrying no sentinel or artifact (#132);
    ``level`` is the authored outcome the caller renders through
    ``render_outcome`` for non-streaming results (#144: the deactivation
    no-op is ``info``, not a ``**Done.**`` claim). It defaults to ``error``
    because most non-streaming constructions are refusals; the two
    non-error shapes set it explicitly. ``success`` stays for the
    should-it-stream fork.
    """

    success: bool
    should_stream: bool
    message: str
    skill_name: str | None = None
    level: CommandResultLevel = "error"


def _path_param(value: Any) -> str:
    return quote(str(value), safe="")


def _clean_params(**values: Any) -> dict[str, Any] | None:
    params = {key: value for key, value in values.items() if value is not None}
    return params or None


def coerce_value(value_str: str) -> Any:
    """Auto-convert command string values to bool/None/int/float/str."""
    lower = value_str.lower()
    if lower in ("true", "false"):
        return lower == "true"
    if lower == "none":
        return None
    try:
        return int(value_str)
    except ValueError:
        pass  # Not an integer; try the next scalar type.
    try:
        return float(value_str)
    except ValueError:
        pass  # Not a float; keep the original string.
    return value_str


def fmt_tokens(n: Optional[int]) -> str:
    """Format token counts for compact command output."""
    if not n:
        return "0"
    if n >= 1_000_000:
        return f"{n / 1_000_000:.1f}M"
    if n >= 1_000:
        return f"{n / 1_000:.1f}k"
    return str(n)


def _client_for_context(ctx: "CommandContext") -> Any:
    """The client execute() and resolve_options construct when the caller
    supplies none: in-process backend doors first, service-token HTTP
    fallback. Raises RuntimeError when neither is available."""
    try:
        return CommandBackendClient.from_context(ctx)
    except RuntimeError:
        return CommandHttpClient.from_service_token()


def live_temporary_tools(thread_config: Optional[dict[str, Any]]) -> set[str]:
    """The still-in-date Skill Kit / TTL'd tool names of a thread config.

    Skill Kit tools live in ``temporary_tools``, NOT ``enabled_tools``. The
    graph folds the live (non-expired) ones into the bound tool list exactly
    like ``enabled_tools``, so every effective-tool-set view must count them
    too or an active Skill Kit's tools look absent on the thread. This is
    THE one liveness computation: ``/tools list`` and the ``tools``
    option resolver both call it; do not re-derive it inline.
    """
    live: set[str] = set()
    temp_raw = thread_config.get("temporary_tools") if thread_config else None
    if isinstance(temp_raw, dict):
        now = utc_now()
        for name, entry in temp_raw.items():
            expires = entry.get("expires_at") if isinstance(entry, dict) else None
            if not expires:
                continue
            try:
                if ensure_aware_utc(datetime.fromisoformat(expires)) > now:
                    live.add(str(name))
            except (TypeError, ValueError):
                continue
    return live


def http_error_detail(exc: httpx.HTTPStatusError, *, text_limit: int = 200) -> str:
    """Extract a concise user-facing detail from an HTTP status error."""
    response = exc.response
    if response is not None:
        try:
            body = response.json()
            if isinstance(body, dict) and "detail" in body:
                return str(body["detail"])
        except Exception:  # noqa: BLE001
            pass
        text = (response.text or "").strip()
        if text:
            return text[:text_limit]
        return f"HTTP {response.status_code}"
    return str(exc)


def _raise_http_status(status_code: int, detail: str) -> NoReturn:
    """Raise an HTTPStatusError compatible with the legacy command executor."""
    request = httpx.Request("COMMAND", "nymeria://command-service")
    response = httpx.Response(
        status_code,
        json={"detail": detail},
        request=request,
    )
    raise httpx.HTTPStatusError(detail, request=request, response=response)


def _resolve_base_url() -> str:
    explicit = os.environ.get("NYMERIA_API_URL")
    if explicit:
        return explicit.rstrip("/")
    if Path("/.dockerenv").exists():
        return "http://api:8000"
    return "http://localhost:8000"


def _actor_from_source(source: str | None) -> CommandActor:
    if source == "agent":
        return "agent"
    return "user"


def _surface_from_source(source: str | None) -> CommandSurface | None:
    if source == "cli":
        return "cli"
    if source == "agent":
        return "agent"
    return None


def _normalize_token(token: str) -> str:
    return token.strip().lower().lstrip("/").replace("-", "_")


def _normalize_path(value: str | tuple[str, ...] | list[str]) -> tuple[str, ...]:
    if isinstance(value, str):
        cleaned = value.strip().lstrip("/")
        if not cleaned:
            return ()
        try:
            parts = shlex.split(cleaned, posix=True)
        except ValueError:
            parts = cleaned.split()
    else:
        parts = [str(part) for part in value]
    return tuple(token for token in (_normalize_token(part) for part in parts) if token)


def _display_path(path: tuple[str, ...] | list[str]) -> str:
    """A command path as it is DISPLAYED: hyphens, never underscores.

    Registry paths normalize ``-`` to ``_``, and parsing folds the same way,
    so ``/background set-url`` and ``/background set_url`` both resolve; rule
    4 of the style guide says only one of them is ever shown.
    """
    return " ".join(token.replace("_", "-") for token in path)


def _usage_for_path(path: tuple[str, ...]) -> str:
    return "/" + _display_path(path)


def _id_for_path(path: tuple[str, ...]) -> str:
    return ".".join(path)


def _alias_display(path: tuple[str, ...]) -> str:
    """An alias path VERBATIM, unlike ``_display_path``.

    Deliberate asymmetry: the flat ``family_verb`` aliases ARE the chat
    platforms' command names (Telegram rejects a hyphen in a command, and
    ``_telegram_command_name`` reads the first single-token alias straight out
    of this payload to name a menu entry), so hyphenating them here would
    advertise, and register, a spelling those platforms cannot accept.
    """
    return "/" + " ".join(path)


def _split_rest_after_tokens(command_text: str, token_count: int) -> str:
    rest = command_text.strip().lstrip("/")
    for _ in range(token_count):
        # Split on ANY whitespace, not just " ": chat clients send Shift+Enter
        # newlines ("/notepad write\nmy note"), and partitioning on a literal
        # space silently dropped the word glued to the newline.
        parts = rest.split(None, 1)
        if len(parts) < 2:
            return ""
        rest = parts[1]
    return rest.strip()


def _split_args(rest: str) -> list[str]:
    try:
        return shlex.split(rest, posix=True) if rest else []
    except ValueError:
        return rest.split()


def _parse_hook_conditions(raw_list: list[str], case_sensitive: bool):
    """Parse ``--cond "field op value"`` strings into ``HookCondition`` objects.

    Returns ``(conditions, error)``; ``error`` is empty on success.
    """
    from typing import cast, get_args

    from .conditions import ConditionOperator, HookCondition

    valid_ops = get_args(ConditionOperator)
    out = []
    for raw in raw_list:
        parts = raw.split(maxsplit=2)
        if len(parts) < 3:
            return [], f"condition {raw!r} must be 'field operator value'."
        field, op, value = parts[0], parts[1], parts[2]
        if op not in valid_ops:
            return [], f"invalid condition operator {op!r}. Valid: {', '.join(valid_ops)}."
        try:
            out.append(
                HookCondition(
                    field=field,
                    operator=cast(ConditionOperator, op),
                    value=value,
                    case_sensitive=case_sensitive,
                )
            )
        except Exception as e:  # noqa: BLE001
            return [], str(e)
    return out, ""


def _parse_hook_sets(raw_list: list[str]) -> tuple[dict[str, str], str]:
    """Parse ``--set "arg=value"`` strings into an updates dict (split on first =)."""
    out: dict[str, str] = {}
    for raw in raw_list:
        if "=" not in raw:
            return {}, f"--set {raw!r} must be 'arg=value'."
        key, _, value = raw.partition("=")
        key = key.strip()
        if not key:
            return {}, f"--set {raw!r} has an empty arg name."
        out[key] = value
    return out, ""


def _parse_hook_timeout(raw: str) -> tuple[float | None, str]:
    """Parse ``--timeout N`` seconds into a float (None when unset)."""
    if not raw:
        return None, ""
    try:
        return float(raw), ""
    except (TypeError, ValueError):
        return None, f"--timeout {raw!r} must be a number of seconds."


def _parse_hook_workflow_params(raw: str) -> tuple[dict | None, str]:
    """Parse ``--workflow-params '<json object>'`` (None when unset)."""
    if not raw:
        return None, ""
    try:
        data = json.loads(raw)
    except Exception:  # noqa: BLE001 - report, never raise into the command
        return None, "--workflow-params must be a JSON object, e.g. '{\"key\": \"value\"}'."
    if not isinstance(data, dict):
        return None, "--workflow-params must be a JSON object, e.g. '{\"key\": \"value\"}'."
    return data, ""


def _parse_hook_on_fault(raw: str) -> tuple[str | None, str]:
    """Validate ``--on-fault allow|deny`` (None when unset)."""
    if not raw:
        return None, ""
    value = raw.strip().lower()
    if value not in ("allow", "deny"):
        return None, "--on-fault must be 'allow' or 'deny'."
    return value, ""


def _truncate(text: str, limit: int = OUTPUT_BUDGET_COMPACT) -> str:
    if len(text) <= limit:
        return text
    note = f"\n\n**Note:** Output truncated (was {len(text)} chars)."
    # Reserve room for the note INSIDE the limit. A naive `limit - 80`
    # goes negative for a small limit and returns more than it was asked
    # for; every live budget is far above that, but the function now takes
    # its limit from a caller rather than a constant.
    keep = max(0, limit - len(note))
    return text[:keep] + note


def _dict_result(value: Any) -> dict[str, Any]:
    if not isinstance(value, dict):
        return {}
    return {str(key): item for key, item in value.items()}


def _thread_override_text(
    llm_cfg: dict[str, Any], settings: dict[str, Any]
) -> str | None:
    """The one spelling of a thread LLM override for /status and /context.

    Renders only the axes that DIFFER from the global settings (a pin equal
    to the global value changes nothing; /context set that precedent), and
    covers the provider-only override /provider switch <p> thread writes
    (model stays None): a thread can run a different provider with the
    same model id.
    """
    model = str(llm_cfg.get("model") or "").strip()
    provider = str(llm_cfg.get("provider") or "").strip()
    if model and model == str(settings.get("llm_model") or ""):
        model = ""
    if provider and provider == str(settings.get("llm_provider") or ""):
        provider = ""
    if model:
        return f"thread override: {model}" + (f" ({provider})" if provider else "")
    if provider:
        return f"thread override: provider {provider}"
    return None


def _is_not_found(value: Any) -> bool:
    """True when a gathered result is the 404 meaning "no such thread".

    Both runtime shapes raise the same type here, which is what makes one
    check enough: the HTTP client raises ``httpx.HTTPStatusError``, and the
    in-process client fabricates the identical exception through
    ``_raise_http_status``, response object and all.
    """
    return (
        isinstance(value, httpx.HTTPStatusError)
        and value.response is not None
        and value.response.status_code == 404
    )


def _optional_dict_result(value: Any) -> dict[str, Any] | None:
    result = _dict_result(value)
    return result or None


def _dict_list_result(value: Any) -> list[dict[str, Any]]:
    if not isinstance(value, list):
        return []
    return [_dict_result(item) for item in value if isinstance(item, dict)]


def _string_set_result(value: Any) -> set[str]:
    if not isinstance(value, (list, tuple, set, frozenset)):
        return set()
    return {item for item in value if isinstance(item, str)}


def _render_result_markdown(
    text: str, level: CommandResultLevel
) -> tuple[bool, CommandResultLevel, str]:
    """Render a handler's authored level into the markdown artifacts every
    surface reads (#132): ``**Error:**`` / ``**Done.**`` / ``**Warning:**``.

    The artifacts themselves come from ``command_forms.render_outcome``,
    the ONE producer (#144). Desktop, mobile, and the five bots render the
    markdown with no branch on success or level, so the artifact IS their
    outcome signal; the CLI pops it into a glyph. ``info`` gets no artifact
    (readouts stay quiet) and instead the heading heuristic: a multi-line
    body whose first line is plain text gets that line promoted to ``### ``
    (the CLI renders it as a block heading). Both dispatch-boundary extras
    (the sentinel parse below, the heading heuristic) apply HERE only, not
    in ``render_outcome``: chat.py's funnel must not rewrite bodies.

    Compat, PERMANENT: text still carrying a legacy ``[Error]:``/
    ``[Success]:``/``[Info]:`` sentinel wins over ``level``. First-party
    handlers are swept clean and ratcheted (test_command_service.py's
    sentinel scanner), so this branch exists for out-of-tree handlers
    (plugins, agent-registered commands) that still speak the old
    protocol. Do not remove it as dead code.
    """
    stripped = text.strip()
    if stripped.startswith("[Error]:"):
        level = "error"
        stripped = stripped.removeprefix("[Error]:").strip()
    elif stripped.startswith("[Success]:"):
        level = "success"
        stripped = stripped.removeprefix("[Success]:").strip()
    elif stripped.startswith("[Info]:"):
        level = "info"
        stripped = stripped.removeprefix("[Info]:").strip()

    if level == "error":
        return False, "error", render_outcome("error", stripped)
    if level == "success":
        return True, "success", render_outcome("success", stripped)
    if level == "warning":
        return True, "warning", render_outcome("warning", stripped)

    if not stripped:
        return True, "info", ""

    lines = stripped.splitlines()
    if len(lines) > 1 and lines[0] and not lines[0].startswith(("-", "*", "#", "`")):
        return True, "info", "### " + lines[0] + "\n\n" + "\n".join(lines[1:]).strip()
    return True, "info", stripped


class CommandHttpClient:
    """Small REST compatibility client for out-of-process command callers.

    ``use_act_as`` is enabled for service-token callers. For direct user-token
    callers, the token itself is authoritative and no X-Nymeria-Act-As header
    is sent.
    """

    def __init__(self, base_url: str, api_key: str, *, use_act_as: bool):
        self.base_url = base_url.rstrip("/")
        self.api_key = api_key
        self.use_act_as = use_act_as
        self._headers = {"Authorization": f"Bearer {api_key}"}
        self._client = httpx.AsyncClient(timeout=httpx.Timeout(connect=10, read=30, write=10, pool=10))

    @classmethod
    def from_service_token(cls) -> "CommandHttpClient":
        settings = get_settings()
        api_key = settings.nymeria_service_token
        if not api_key:
            raise RuntimeError(
                "NYMERIA_SERVICE_TOKEN is required for internal command execution."
            )
        return cls(_resolve_base_url(), api_key, use_act_as=True)

    @classmethod
    def from_bearer(cls, token: str, *, use_act_as: bool = False) -> "CommandHttpClient":
        return cls(_resolve_base_url(), token, use_act_as=use_act_as)

    async def close(self) -> None:
        await self._client.aclose()

    async def aclose(self) -> None:
        await self.close()

    def _url(self, path: str) -> str:
        return f"{self.base_url}{path}"

    def _headers_for(self, act_as: Optional[str]) -> dict[str, str]:
        if not self.use_act_as or not act_as:
            return self._headers
        return {**self._headers, "X-Nymeria-Act-As": act_as}

    async def _request(
        self,
        method: str,
        path: str,
        *,
        json_body: Optional[dict] = None,
        params: Optional[dict] = None,
        act_as: Optional[str] = None,
        timeout: httpx.Timeout | None = None,
    ) -> Any:
        resp = await self._client.request(
            method,
            self._url(path),
            headers=self._headers_for(act_as),
            json=json_body,
            params=params,
            timeout=timeout,
        )
        resp.raise_for_status()
        if resp.status_code == 204:
            return {}
        return resp.json()

    async def _get(self, path: str, params: Optional[dict] = None, act_as: Optional[str] = None) -> Any:
        return await self._request("GET", path, params=params, act_as=act_as)

    async def _post(self, path: str, json: Optional[dict] = None, params: Optional[dict] = None, act_as: Optional[str] = None) -> Any:
        return await self._request("POST", path, json_body=json, params=params, act_as=act_as)

    async def _patch(
        self,
        path: str,
        json: Optional[dict] = None,
        params: Optional[dict] = None,
        act_as: Optional[str] = None,
    ) -> Any:
        return await self._request("PATCH", path, json_body=json, params=params, act_as=act_as)

    async def _delete(self, path: str, params: Optional[dict] = None, act_as: Optional[str] = None) -> Any:
        return await self._request("DELETE", path, params=params, act_as=act_as)

    async def get_context_stats(self, thread_id: str, user_id: Optional[str] = None) -> dict:
        return await self._get(f"/threads/{_path_param(thread_id)}/context", act_as=user_id)

    async def get_history(
        self,
        thread_id: str,
        user_id: Optional[str] = None,
        *,
        include_internal: bool = False,
    ) -> dict:
        return await self._get(
            f"/threads/{_path_param(thread_id)}/history",
            params={"include_internal": str(include_internal).lower()},
            act_as=user_id,
        )

    async def get_thread_config(self, thread_id: str, user_id: Optional[str] = None) -> Optional[dict]:
        try:
            return await self._get(f"/threads/{_path_param(thread_id)}/config", act_as=user_id)
        except httpx.HTTPStatusError as e:
            if e.response.status_code == 404:
                return None
            raise

    async def list_threads(self, user_id: Optional[str] = None) -> list[dict]:
        # owned_only mirrors CommandBackendClient.list_threads so the two
        # TurnExecutor shapes cannot drift.
        data = await self._get("/threads", params={"owned_only": "true"}, act_as=user_id)
        if isinstance(data, dict):
            threads = data.get("threads", [])
            return [thread for thread in threads if isinstance(thread, dict)]
        return []

    async def list_thread_teams(self, user_id: Optional[str] = None) -> list[dict]:
        data = await self._get("/thread-teams", act_as=user_id)
        if isinstance(data, dict):
            teams = data.get("teams", [])
            return [team for team in teams if isinstance(team, dict)]
        return []

    async def create_thread(
        self,
        user_id: Optional[str] = None,
        *,
        thread_id: Optional[str] = None,
        title: Optional[str] = None,
    ) -> dict:
        selected_thread_id = thread_id or uuid.uuid4().hex[:8]
        claimed = await self._post(
            f"/threads/{_path_param(selected_thread_id)}/claim",
            json=_clean_params(title=title, platform="cli"),
            act_as=user_id,
        )
        payload: dict[str, Any] = {
            "thread_id": selected_thread_id,
            "title": title or "New Chat",
            "title_source": "user" if title else "default",
            "platform": "cli",
        }
        if isinstance(claimed, dict):
            payload.update(claimed)
        return payload

    async def update_thread_metadata(
        self,
        thread_id: str,
        user_id: Optional[str] = None,
        *,
        title: Optional[str] = None,
        pinned: Optional[bool] = None,
    ) -> dict:
        body: dict[str, Any] = {}
        if title is not None:
            body["title"] = title
        if pinned is not None:
            body["pinned"] = pinned
        return await self._patch(
            f"/threads/{_path_param(thread_id)}/metadata", json=body, act_as=user_id
        )

    async def delete_thread(self, thread_id: str, user_id: Optional[str] = None) -> dict:
        return await self._delete(f"/threads/{_path_param(thread_id)}", act_as=user_id)

    async def branch_thread(
        self,
        thread_id: str,
        user_id: Optional[str] = None,
        *,
        title: Optional[str] = None,
        from_message_index: Optional[int] = None,
    ) -> dict:
        body: dict[str, Any] = {}
        if title is not None:
            body["title"] = title
        if from_message_index is not None:
            body["from_message_index"] = from_message_index
        return await self._post(
            f"/threads/{_path_param(thread_id)}/branch", json=body, act_as=user_id
        )

    async def compact_thread(self, thread_id: str, user_id: Optional[str] = None) -> dict:
        return await self._post(
            f"/threads/{_path_param(thread_id)}/compact",
            params={"user_id": user_id} if user_id else None,
            act_as=user_id,
        )

    async def get_settings(self, user_id: Optional[str] = None) -> dict:
        return await self._get("/settings", act_as=user_id)

    async def get_default_tools(self, user_id: str = "default") -> dict:
        return await self._get("/tools/defaults", params={"user_id": user_id}, act_as=user_id)

    async def get_tool_categories(self) -> dict:
        return await self._get("/tools/categories")

    async def list_memories(self, user_id: str) -> list[dict]:
        data = await self._get(f"/users/{_path_param(user_id)}/memories", act_as=user_id)
        return data.get("memories", [])

    async def save_memory(self, user_id: str, key: str, value: str) -> dict:
        return await self._post(
            f"/users/{_path_param(user_id)}/memories",
            json={"key": key, "value": value},
            act_as=user_id,
        )

    async def forget_memory(self, user_id: str, key: str) -> dict:
        return await self._delete(
            f"/users/{_path_param(user_id)}/memories/{_path_param(key)}",
            act_as=user_id,
        )

    async def search_memories(self, user_id: str, query: str) -> list[dict]:
        data = await self._get(
            f"/users/{_path_param(user_id)}/memories/search",
            params={"q": query},
            act_as=user_id,
        )
        return data.get("results", [])

    async def list_todos(
        self,
        user_id: str,
        *,
        filter_status: Optional[str] = None,
        thread_id: Optional[str] = None,
    ) -> list[dict]:
        data = await self._get(
            "/todos",
            params=_clean_params(
                user_id=user_id,
                filter_status=filter_status,
                thread_id=thread_id,
            ),
            act_as=user_id,
        )
        return data.get("items", [])

    async def add_todo(
        self,
        user_id: str,
        task: str,
        scheduled_for: str = "1d",
        notes: Optional[str] = None,
        recurrence: Optional[str] = None,
        thread_id: Optional[str] = None,
    ) -> dict:
        body: dict[str, Any] = {"task": task, "scheduled_for": scheduled_for}
        if notes:
            body["notes"] = notes
        if recurrence:
            body["recurrence"] = recurrence
        if thread_id:
            body["thread_id"] = thread_id
        return await self._post("/todos", json=body, params={"user_id": user_id}, act_as=user_id)

    async def complete_todo(self, user_id: str, todo_id: str) -> dict:
        return await self._post(
            f"/todos/{_path_param(todo_id)}/complete",
            params={"user_id": user_id},
            act_as=user_id,
        )

    async def delete_todo(self, user_id: str, todo_id: str) -> dict:
        return await self._delete(
            f"/todos/{_path_param(todo_id)}",
            params={"user_id": user_id},
            act_as=user_id,
        )

    async def update_todo(self, user_id: str, todo_id: str, **patch: Any) -> dict:
        return await self._patch(
            f"/todos/{_path_param(todo_id)}",
            json=patch,
            params={"user_id": user_id},
            act_as=user_id,
        )

    async def update_settings(self, *, user_id: Optional[str] = None, **kwargs) -> dict:
        return await self._patch("/settings", json=kwargs, act_as=user_id)

    async def test_llm_provider_config(
        self,
        request: dict,
        *,
        user_id: Optional[str] = None,
    ) -> dict:
        """Test an LLM provider configuration without saving it (admin)."""
        return await self._post("/settings/llm/test", json=request, act_as=user_id)

    async def update_thread_config(
        self,
        thread_id: str,
        *,
        user_id: Optional[str] = None,
        **kwargs,
    ) -> dict:
        return await self._patch(f"/threads/{_path_param(thread_id)}/config", json=kwargs, act_as=user_id)

    async def get_env_vars(self, *, user_id: Optional[str] = None) -> dict:
        return await self._get("/settings/env", act_as=user_id)

    async def get_env_var(self, key: str, *, user_id: Optional[str] = None) -> dict:
        # `/env <key>` is an explicit admin reveal, so request the unmasked
        # value; the bulk `/env` listing stays masked.
        return await self._get(
            f"/settings/env/{_path_param(key)}",
            params={"reveal": "true"},
            act_as=user_id,
        )

    async def list_available_models(
        self,
        provider: Optional[str] = None,
        user_id: Optional[str] = None,
        *,
        api_key: Optional[str] = None,
        base_url: Optional[str] = None,
    ) -> list[dict]:
        if api_key or base_url:
            # Ephemeral credential override: POST so the key rides the request
            # body, never a query string (URLs and access logs).
            body: dict = {}
            if provider:
                body["provider"] = provider
            if api_key:
                body["api_key"] = api_key
            if base_url:
                body["base_url"] = base_url
            return await self._post("/models/available", json=body, act_as=user_id)
        params = {"provider": provider} if provider else None
        return await self._get("/models/available", params=params, act_as=user_id)

    # --- CLIProxy subscription OAuth (admin-only routes) ------------------
    # All six pass act_as so the route-level admin check evaluates the
    # ACTING user, not the service token (the get_env_vars idiom). The
    # chain applies routes globally only, so there is no scope parameter.

    async def cliproxy_auth_files(
        self,
        provider: Optional[str] = None,
        *,
        user_id: Optional[str] = None,
    ) -> list[dict]:
        params = {"provider": provider} if provider else None
        return list(
            await self._get("/cliproxy/auth-files", params=params, act_as=user_id)
            or []
        )

    async def cliproxy_oauth_start(
        self, provider: str, *, user_id: Optional[str] = None
    ) -> dict:
        return await self._post(
            "/cliproxy/oauth/start",
            json={"provider": provider},
            act_as=user_id,
        )

    async def cliproxy_oauth_callback(
        self,
        provider: str,
        *,
        redirect_url: Optional[str] = None,
        code: Optional[str] = None,
        state: Optional[str] = None,
        user_id: Optional[str] = None,
    ) -> dict:
        body: dict = {"provider": provider}
        if redirect_url:
            body["redirect_url"] = redirect_url
        if code:
            body["code"] = code
        if state:
            body["state"] = state
        return await self._post(
            "/cliproxy/oauth/callback", json=body, act_as=user_id
        )

    async def cliproxy_oauth_status(
        self, state: str, provider: str, *, user_id: Optional[str] = None
    ) -> dict:
        return await self._get(
            "/cliproxy/oauth/status",
            params={"state": state, "provider": provider},
            act_as=user_id,
        )

    async def cliproxy_models(
        self, *, user_id: Optional[str] = None
    ) -> list[dict]:
        payload = await self._get("/cliproxy/models", act_as=user_id)
        return list((payload or {}).get("models") or [])

    async def cliproxy_verify_credential(
        self,
        provider: str,
        *,
        model: Optional[str] = None,
        user_id: Optional[str] = None,
    ) -> dict:
        return await self._post(
            "/cliproxy/verify",
            json={"provider": provider, "model": model or ""},
            act_as=user_id,
        )

    async def cliproxy_apply_route(
        self, provider: str, model: str, *, user_id: Optional[str] = None
    ) -> dict:
        return await self._post(
            "/cliproxy/apply-route",
            json={"provider": provider, "model": model, "scope": "global"},
            act_as=user_id,
        )


@dataclass(frozen=True)
class _CommandBackendUser:
    id: str
    role: Literal["user", "admin"] = "user"
    email: str = ""
    display_name: str = ""
    via_act_as: bool = False


class CommandBackendClient:
    """In-process command backend adapter.

    The command executor only needs a small API-shaped interface. This adapter
    implements that interface by calling Nymeria managers directly instead of
    sending HTTP requests back into the same API process.
    """

    def __init__(
        self,
        agent: Any,
        *,
        user: _CommandBackendUser,
        settings_fn: Callable[[], Any] = get_settings,
    ) -> None:
        self.agent = agent
        self.user = user
        self.settings_fn = settings_fn

    @classmethod
    def from_context(
        cls,
        ctx: CommandContext,
        *,
        agent: Any | None = None,
        user: Any | None = None,
        settings_fn: Callable[[], Any] | None = None,
    ) -> "CommandBackendClient":
        if agent is None:
            from .agent import get_current_agent

            agent = get_current_agent()
        if agent is None:
            raise RuntimeError("No current NymeriaAgent is available for command execution.")

        if user is not None:
            backend_user = _CommandBackendUser(
                id=user.id,
                role=user.role,
                email=getattr(user, "email", ""),
                display_name=getattr(user, "display_name", ""),
                via_act_as=getattr(user, "via_act_as", False),
            )
        else:
            role: Literal["user", "admin"] = "admin" if ctx.is_admin else "user"
            email = ""
            display_name = ctx.user_id
            try:
                record = agent.accounts_repo.get_user_by_id(ctx.user_id)
            except Exception:  # noqa: BLE001
                record = None
            if record is not None:
                role = record.role
                email = record.email
                display_name = record.display_name
            backend_user = _CommandBackendUser(
                id=ctx.user_id,
                role=role,
                email=email,
                display_name=display_name,
                via_act_as=ctx.via_act_as,
            )
        return cls(agent, user=backend_user, settings_fn=settings_fn or get_settings)

    async def close(self) -> None:
        return None

    async def aclose(self) -> None:
        return None

    def _settings(self) -> Any:
        return self.settings_fn()

    def _require_admin(self) -> None:
        if self.user.role != "admin":
            _raise_http_status(403, "Admin only")

    def _require_same_user_or_admin(self, user_id: str) -> None:
        if user_id != self.user.id and self.user.role != "admin":
            _raise_http_status(404, "Not found")

    def _require_thread_access(self, thread_id: str) -> None:
        from .thread_classification import is_shared_channel

        if self.user.role == "admin":
            if is_shared_channel(thread_id):
                return
            self.agent.accounts_repo.claim_thread(thread_id, self.user.id)
            return

        if is_shared_channel(thread_id):
            if self.user.via_act_as:
                return
            _raise_http_status(404, "Not found")

        owner = self.agent.accounts_repo.claim_thread(thread_id, self.user.id)
        if owner != self.user.id:
            _raise_http_status(404, "Not found")

    def _checked_user_id(self, user_id: str) -> str:
        self._require_same_user_or_admin(user_id)
        return user_id

    async def clear_thread(self, thread_id: str) -> dict:
        """Clear conversation history; preserve notepad + tool config."""
        self._require_thread_access(thread_id)
        agent = self.agent
        settings = self._settings()
        user_id = self.user.id

        try:
            config = {"configurable": {"thread_id": thread_id}}
            state = await agent._default_async_graph.aget_state(config)
            messages = state.values.get("messages", [])
            if messages:
                agent._flush_memories_before_trim(user_id, thread_id, messages)
        except Exception as e:
            logger.warning("Pre-clear RAG flush failed for %s: %s", thread_id, e)

        agent.thread_metadata_manager.delete_thread(user_id, thread_id)

        try:
            from .checkpoint_cleanup import delete_thread_checkpoints

            delete_thread_checkpoints(settings, thread_id)
        except Exception as e:
            logger.warning("Failed to delete checkpoints for %s: %s", thread_id, e)

        logger.info("Thread %s conversation cleared (config + notepad preserved)", thread_id)
        return {"status": "ok", "thread_id": thread_id}

    async def restart_api(self) -> dict:
        """Schedule an API server restart. Admin-only."""
        self._require_admin()
        from ..api.routers.system import restart_api_process

        restart_api_process(self.agent, self._settings())
        return {"status": "restarting"}

    async def stop_thread(self, thread_id: str) -> dict:
        """Abort the running turn on a thread; cascades to callable children.

        A user-initiated stop hands queued user prompts back instead of
        discarding them (``restored_prompts``, raw text in FIFO order),
        mirroring the REST stop route.
        """
        self._require_thread_access(thread_id)
        thread_locks = getattr(self.agent, "_thread_locks", None)
        lock_info = thread_locks.get_lock_info(thread_id) if thread_locks else None
        if lock_info:
            from .pending_prompt_queue import restored_prompts_payload

            restored = self.agent.abort_with_cascade(thread_id, restore_queue=True)
            return {
                "status": "stopping",
                "thread_id": thread_id,
                "holder": lock_info.get("holder"),
                "held_seconds": lock_info.get("held_seconds", 0),
                "restored_prompts": restored_prompts_payload(restored),
            }
        return {
            "status": "idle",
            "thread_id": thread_id,
            "restored_prompts": [],
        }

    async def prune_thread(self, thread_id: str, mode: str = "full") -> dict:
        """Deterministically compress tool returns in a thread (no LLM)."""
        self._require_thread_access(thread_id)
        return await self.agent.prune_now(thread_id, self.user.id, mode=mode)

    async def get_context_stats(self, thread_id: str, user_id: Optional[str] = None) -> dict:
        self._require_thread_access(thread_id)
        stats = dict(self.agent.get_context_stats(thread_id) or {})
        thread_locks = getattr(self.agent, "_thread_locks", None)
        if thread_locks is not None:
            try:
                stats["processing"] = thread_locks.get_lock_info(thread_id) is not None
            except Exception as e:  # noqa: BLE001
                logger.warning("Failed to inspect processing state for %s: %s", thread_id, e)
                stats["processing"] = False
        return stats

    async def get_history(
        self,
        thread_id: str,
        user_id: Optional[str] = None,
        *,
        include_internal: bool = False,
    ) -> dict:
        # Mirrors GET /threads/{id}/history: with include_internal the route
        # forces the autonomous/prompt-metadata filters off, so match that here.
        self._require_thread_access(thread_id)
        show_autonomous = False
        show_prompt_metadata = False
        if not include_internal:
            tc = self.agent.thread_config_manager.get_config(thread_id)
            if tc and getattr(tc, "show_autonomous_prompts", False):
                show_autonomous = True
            if tc and getattr(tc, "show_prompt_metadata", False):
                show_prompt_metadata = True
        messages = self.agent.get_conversation_history(
            thread_id,
            include_internal=include_internal,
            show_autonomous_prompts=show_autonomous,
            show_prompt_metadata=show_prompt_metadata,
        )
        return {"thread_id": thread_id, "messages": messages}

    async def get_thread_config(self, thread_id: str, user_id: Optional[str] = None) -> Optional[dict]:
        self._require_thread_access(thread_id)
        from ..api.routers.thread_config import (
            _config_response,
            _default_thread_config_response,
        )

        tc = self.agent.thread_config_manager.get_config(thread_id)
        if tc:
            return _config_response(tc)
        return _default_thread_config_response(thread_id)

    async def list_threads(self, user_id: Optional[str] = None) -> list[dict]:
        # Mirrors GET /threads?owned_only=true: only threads recorded in
        # thread_owners for the acting user, skipping the checkpoint/resource
        # recovery enrichment (the safe default the route documents).
        from ..api.routers.threads import _thread_list_payload

        target_user_id = self._checked_user_id(user_id or self.user.id)
        agent = self.agent
        owned_ids = set(agent.accounts_repo.list_threads_for_user(target_user_id))
        store = agent.thread_metadata_manager.get_store(target_user_id)
        return [
            _thread_list_payload(agent, tid, store.threads.get(tid))
            for tid in sorted(owned_ids)
        ]

    async def list_thread_teams(self, user_id: Optional[str] = None) -> list[dict]:
        # Callable visibility teams via the one shared serializer
        # (core/team_manager.py), mirroring GET /thread-teams. The thread
        # universe is this surface's own listing so semantics match the
        # threads the caller can see.
        from .team_manager import serialize_thread_teams

        target_user_id = self._checked_user_id(user_id or self.user.id)
        thread_ids = [
            thread_id
            for thread in await self.list_threads(target_user_id)
            if (thread_id := str(thread.get("thread_id") or thread.get("id") or ""))
        ]
        return serialize_thread_teams(
            self.agent, target_user_id, thread_ids=thread_ids
        )["teams"]

    async def create_thread(
        self,
        user_id: Optional[str] = None,
        *,
        thread_id: Optional[str] = None,
        title: Optional[str] = None,
    ) -> dict:
        # Mirrors POST /threads/{id}/claim: upsert cli-platform metadata and
        # claim ownership so the thread appears in owned listings.
        from ..api.routers.threads import _thread_list_payload

        target_user_id = self._checked_user_id(user_id or self.user.id)
        selected_thread_id = thread_id or uuid.uuid4().hex[:8]
        fields: dict[str, Any] = {"platform": "cli"}
        if title:
            fields["title"] = title
            fields["title_source"] = "user"
        meta = self.agent.thread_metadata_manager.upsert_thread(
            target_user_id, selected_thread_id, **fields
        )
        self.agent.accounts_repo.claim_thread(selected_thread_id, target_user_id)
        return _thread_list_payload(self.agent, selected_thread_id, meta)

    async def update_thread_metadata(
        self,
        thread_id: str,
        user_id: Optional[str] = None,
        *,
        title: Optional[str] = None,
        pinned: Optional[bool] = None,
    ) -> dict:
        self._require_thread_access(thread_id)
        fields: dict[str, Any] = {}
        if title is not None:
            fields["title"] = title.strip()
            fields["title_source"] = "user"
        if pinned is not None:
            fields["pinned"] = pinned
        meta = self.agent.thread_metadata_manager.upsert_thread(
            self.user.id, thread_id, **fields
        )
        return meta.model_dump(mode="json")

    async def delete_thread(self, thread_id: str, user_id: Optional[str] = None) -> dict:
        # Mirrors DELETE /threads/{id}: full cascade delete of every resource
        # that could recreate the thread.
        self._require_thread_access(thread_id)
        from .thread_deletion import ThreadDeletionBusy, cascade_delete_thread

        try:
            deletion = cascade_delete_thread(
                self.agent, self._settings(), self.user.id, thread_id
            )
        except ThreadDeletionBusy as e:
            _raise_http_status(409, str(e))
        return deletion.model_dump()

    async def branch_thread(
        self,
        thread_id: str,
        user_id: Optional[str] = None,
        *,
        title: Optional[str] = None,
        from_message_index: Optional[int] = None,
    ) -> dict:
        # Mirrors POST /threads/{id}/branch (thread_branch.branch_thread run off
        # the event loop), including the route's mid-turn guard: branching a
        # processing thread would copy the last committed checkpoint and drop
        # the in-flight turn.
        self._require_thread_access(thread_id)
        from ..api.thread_overview import is_thread_processing

        if is_thread_processing(self.agent, thread_id):
            _raise_http_status(409, "Cannot branch while the source thread is processing")
        from .thread_branch import ThreadBranchError, branch_thread

        try:
            return await asyncio.to_thread(
                branch_thread,
                agent=self.agent,
                settings=self._settings(),
                user_id=self.user.id,
                source_thread_id=thread_id,
                title=title,
                from_message_index=from_message_index,
            )
        except ThreadBranchError as e:
            _raise_http_status(400, str(e))

    async def compact_thread(self, thread_id: str, user_id: Optional[str] = None) -> dict:
        self._require_thread_access(thread_id)
        return await self.agent.compact_now(thread_id, self.user.id)

    async def get_settings(self, user_id: Optional[str] = None) -> dict:
        from ..api.routers.settings import serialize_server_settings

        # One canonical serializer shared with GET /settings, so the in-process
        # slash-command settings view cannot drift from the route the way this
        # hand-built dict had (it had silently fallen ~20 fields behind). The
        # mode="json" dump matches the JSON the HTTP command backends parse back
        # from that same route, keeping the two TurnExecutor shapes identical.
        return serialize_server_settings(self._settings()).model_dump(mode="json")

    async def get_default_tools(self, user_id: str = "default") -> dict:
        # Shared serializer with GET /tools/defaults so the two TurnExecutor
        # shapes cannot drift (mirrors serialize_server_settings / _env_entries).
        from ..api.routers.tools import serialize_default_tools

        target_user_id = self._checked_user_id(user_id)
        return serialize_default_tools(
            self.agent, user_id=target_user_id, role=self.user.role
        )

    async def get_tool_categories(self) -> dict:
        from ..tools import filter_discoverable_catalog_tool_names
        from ..tools.metadata import get_category_tools_summary

        categories = get_category_tools_summary()
        visible_names = filter_discoverable_catalog_tool_names(
            {name for names in categories.values() for name in names},
            self.user.role,
        )
        return {
            "categories": {
                category: [name for name in names if name in visible_names]
                for category, names in categories.items()
            }
        }

    async def list_memories(self, user_id: str) -> list[dict]:
        target_user_id = self._checked_user_id(user_id)
        profile = self.agent.profile_manager.get_profile(target_user_id)
        return [
            {
                "key": m.key,
                "value": m.value,
                "created_at": m.created_at.isoformat(),
                "accessed_at": m.accessed_at.isoformat(),
                "access_count": m.access_count,
            }
            for m in profile.memories
        ]

    def _upsert_memory_rag_chunk(self, user_id: str, key: str, value: str) -> None:
        memory_index = self.agent._get_memory_index(user_id)
        if not memory_index:
            return
        try:
            memory_index.delete_memory_key(user_id, key)
            memory_index.add_chunk(
                content=f"{key}: {value}",
                metadata={"key": key},
                chunk_type="memory",
                user_id=user_id,
            )
        except Exception as e:  # noqa: BLE001
            logger.warning("Failed to sync memory '%s' into RAG index: %s", key, e)

    def _delete_memory_rag_chunk(self, user_id: str, key: str) -> None:
        memory_index = self.agent._get_memory_index(user_id)
        if not memory_index:
            return
        try:
            memory_index.delete_memory_key(user_id, key)
        except Exception as e:  # noqa: BLE001
            logger.warning("Failed to remove memory '%s' from RAG index: %s", key, e)

    async def save_memory(self, user_id: str, key: str, value: str) -> dict:
        from .memory_limits import (
            get_global_memory_char_limit,
            get_memory_max_entries,
            get_memory_value_max_chars,
            validate_profile_memory_write,
        )

        target_user_id = self._checked_user_id(user_id)
        agent_settings = getattr(self.agent, "settings", None)
        max_entries = get_memory_max_entries(agent_settings)
        value_cap = get_memory_value_max_chars(agent_settings)
        with self.agent.profile_manager.atomic_update(target_user_id) as profile:
            limit_error = validate_profile_memory_write(
                profile,
                key=key,
                value=value,
                limit=get_global_memory_char_limit(agent_settings),
                max_entries=max_entries,
                max_value_chars=value_cap,
            )
            if limit_error:
                _raise_http_status(400, limit_error)
            success = profile.add_memory(
                key, value, max_entries=max_entries, max_value_chars=value_cap
            )
            stored = profile.get_memory(key)
            stored_value = stored.value if stored else value
        if not success:
            _raise_http_status(400, f"Memory limit reached ({max_entries})")
        self._upsert_memory_rag_chunk(target_user_id, key, stored_value)
        return {"status": "ok", "key": key}

    async def forget_memory(self, user_id: str, key: str) -> dict:
        target_user_id = self._checked_user_id(user_id)
        with self.agent.profile_manager.atomic_update(target_user_id) as profile:
            removed = profile.remove_memory(key)
        if not removed:
            _raise_http_status(404, f"No memory with key '{key}'")
        self._delete_memory_rag_chunk(target_user_id, key)
        return {"status": "ok", "key": key}

    async def search_memories(self, user_id: str, query: str) -> list[dict]:
        target_user_id = self._checked_user_id(user_id)
        profile = self.agent.profile_manager.get_profile(target_user_id)
        return [
            {"key": m.key, "value": m.value, "access_count": m.access_count}
            for m in profile.search_memories(query)
        ]

    async def list_todos(
        self,
        user_id: str,
        *,
        filter_status: Optional[str] = None,
        thread_id: Optional[str] = None,
    ) -> list[dict]:
        from ..api.routers.todos import STATUS_ORDER, _todo_to_response
        from .todo_manager import TodoManager, TodoStatus

        target_user_id = self._checked_user_id(user_id)
        todo_list = TodoManager(self._settings().data_dir).get_todos(target_user_id)
        if filter_status == "all":
            items = todo_list.items
        elif filter_status:
            try:
                status = TodoStatus(filter_status.lower())
            except ValueError:
                _raise_http_status(400, f"Invalid status filter '{filter_status}'")
            items = [i for i in todo_list.items if i.status == status]
        else:
            items = todo_list.get_active_todos()
        if thread_id:
            items = [i for i in items if i.thread_id == thread_id]
        sorted_items = sorted(
            items,
            key=lambda i: (STATUS_ORDER.get(i.status, 3), i.created_at),
        )
        return [_todo_to_response(item).model_dump(mode="json") for item in sorted_items]

    async def add_todo(
        self,
        user_id: str,
        task: str,
        scheduled_for: str = "1d",
        notes: Optional[str] = None,
        recurrence: Optional[str] = None,
        thread_id: Optional[str] = None,
    ) -> dict:
        from fastapi import HTTPException

        from ..api.routers.todos import (
            _get_todo_schedule_db,
            _parse_scheduled_for,
            _todo_to_response,
        )
        from .todo_constants import validate_recurrence
        from .todo_manager import TodoManager

        target_user_id = self._checked_user_id(user_id)
        settings = self._settings()
        todo_manager = TodoManager(settings.data_dir)
        canonical_recurrence: Optional[str] = None
        if recurrence:
            try:
                canonical_recurrence = validate_recurrence(recurrence)
            except ValueError as exc:
                _raise_http_status(400, str(exc))
        try:
            parsed_schedule = _parse_scheduled_for(scheduled_for)
        except HTTPException as exc:
            # _parse_scheduled_for raises only HTTPException(400) on a bad
            # schedule string; forward that as the user-facing status. Any other
            # error is a genuine bug and propagates to the dispatcher's
            # logger.exception handler instead of being masked as a 400.
            _raise_http_status(exc.status_code, str(exc.detail))
        todo_thread_id = thread_id or f"default-{target_user_id}"
        with todo_manager.atomic_update(target_user_id) as todo_list:
            item = todo_list.add_item(
                task=task,
                notes=notes,
                scheduled_for=parsed_schedule,
                thread_id=todo_thread_id,
                created_by="user",
                recurrence=canonical_recurrence,
            )
            if item is None:
                _raise_http_status(400, "Cannot create TODO: maximum limit reached")
            created_item = item
        if created_item.scheduled_for:
            schedule_db = _get_todo_schedule_db(settings)
            todo_manager.sync_schedule_to_db(target_user_id, created_item.id, schedule_db)
        return _todo_to_response(created_item).model_dump(mode="json")

    async def complete_todo(self, user_id: str, todo_id: str) -> dict:
        from fastapi import HTTPException

        from ..api.routers.todos import (
            _get_todo_schedule_db,
            _raise_if_todo_executing,
            _recurrence_anchor,
            _todo_to_response,
        )
        from .todo_constants import compute_recurrence_reschedule
        from .todo_manager import TodoManager, TodoStatus

        target_user_id = self._checked_user_id(user_id)
        settings = self._settings()
        todo_manager = TodoManager(settings.data_dir)
        schedule_db = _get_todo_schedule_db(settings)
        existing = todo_manager.get_todos(target_user_id).get_item(todo_id)
        if not existing:
            _raise_http_status(404, f"TODO '{todo_id}' not found")
        try:
            _raise_if_todo_executing(schedule_db, todo_id, target_user_id, settings)
        except HTTPException as exc:
            # _raise_if_todo_executing raises only HTTPException(409) when a
            # scheduled run owns the TODO; forward that. A storage/operational
            # error from is_execution_active is a genuine fault and propagates to
            # the dispatcher's logger.exception handler rather than masking as 409.
            _raise_http_status(exc.status_code, str(exc.detail))
        with todo_manager.atomic_update(target_user_id) as todo_list:
            item = todo_list.get_item(todo_id)
            if not item:
                _raise_http_status(404, f"TODO '{todo_id}' not found")
            # A paused schedule (#154) must not be silently resumed by
            # "done": mirror _reschedule_recurring_done's guard.
            has_recurrence = (
                item.recurrence if item.schedule_paused_at is None else None
            )
            if not todo_list.complete_item(todo_id):
                _raise_http_status(404, f"TODO '{todo_id}' not found")
            if has_recurrence:
                recurrence_anchor = _recurrence_anchor(item)
                next_execution, origin_to_persist = compute_recurrence_reschedule(
                    has_recurrence,
                    recurrence_anchor,
                    item.recurrence_anchor,
                )
                if next_execution:
                    todo_list.update_item(
                        todo_id,
                        scheduled_for=next_execution,
                        status=TodoStatus.PENDING,
                    )
                    refreshed = todo_list.get_item(todo_id)
                    if refreshed:
                        refreshed.last_execution = recurrence_anchor
                        if origin_to_persist is not None:
                            refreshed.recurrence_anchor = origin_to_persist
            item = todo_list.get_item(todo_id)
            if has_recurrence and item and item.scheduled_for:
                todo_manager.sync_schedule_to_db(target_user_id, todo_id, schedule_db)
            else:
                schedule_db.remove_scheduled(todo_id)
            if item is None:
                _raise_http_status(404, f"TODO '{todo_id}' not found after completion")
            return _todo_to_response(item).model_dump(mode="json")

    async def delete_todo(self, user_id: str, todo_id: str) -> dict:
        from fastapi import HTTPException

        from ..api.routers.todos import _get_todo_schedule_db, _raise_if_todo_executing
        from .todo_manager import TodoManager

        target_user_id = self._checked_user_id(user_id)
        settings = self._settings()
        todo_manager = TodoManager(settings.data_dir)
        schedule_db = _get_todo_schedule_db(settings)
        existing = todo_manager.get_todos(target_user_id).get_item(todo_id)
        if not existing:
            _raise_http_status(404, f"TODO '{todo_id}' not found")
        try:
            _raise_if_todo_executing(schedule_db, todo_id, target_user_id, settings)
        except HTTPException as exc:
            # See complete_todo: forward the 409, let real faults propagate.
            _raise_http_status(exc.status_code, str(exc.detail))
        with todo_manager.atomic_update(target_user_id) as todo_list:
            if not todo_list.get_item(todo_id):
                _raise_http_status(404, f"TODO '{todo_id}' not found")
            if not todo_list.delete_item(todo_id):
                _raise_http_status(404, f"TODO '{todo_id}' not found")
            schedule_db.remove_scheduled(todo_id)
        return {"status": "ok", "deleted_id": todo_id}

    async def update_todo(self, user_id: str, todo_id: str, **patch: Any) -> dict:
        # Mirrors the REST PATCH /todos/{id} handler (api/routers/todos.py):
        # same field set and validation, same done-auto-reschedule and
        # schedule-db sync ordering. Like the local complete/delete twins it
        # runs the executing-guard once, before the lock (the REST handler
        # re-checks inside it), and relies on update_item's truncation
        # rather than the request-model max_length caps.
        from fastapi import HTTPException

        from ..api.routers.todos import (
            _get_todo_schedule_db,
            _parse_scheduled_for,
            _raise_if_todo_executing,
            _reschedule_recurring_done,
            _todo_to_response,
        )
        from .todo_constants import validate_recurrence
        from .todo_manager import TodoManager, TodoStatus

        target_user_id = self._checked_user_id(user_id)
        settings = self._settings()
        todo_manager = TodoManager(settings.data_dir)
        schedule_db = _get_todo_schedule_db(settings)

        status = None
        raw_status = patch.get("status")
        if raw_status:
            try:
                status = TodoStatus(str(raw_status).lower())
            except ValueError:
                _raise_http_status(
                    400,
                    f"Invalid status: '{raw_status}'. Use: pending, in_progress, done",
                )
        clear_schedule = bool(patch.get("clear_schedule"))
        clear_recurrence = bool(patch.get("clear_recurrence"))
        recurrence = None
        if patch.get("recurrence") and not clear_recurrence:
            try:
                recurrence = validate_recurrence(str(patch["recurrence"]))
            except ValueError as exc:
                _raise_http_status(400, str(exc))
        scheduled_for = None
        if patch.get("scheduled_for") and not clear_schedule:
            try:
                scheduled_for = _parse_scheduled_for(str(patch["scheduled_for"]))
            except HTTPException as exc:
                # See add_todo: forward the 400, let real faults propagate.
                _raise_http_status(exc.status_code, str(exc.detail))

        existing = todo_manager.get_todos(target_user_id).get_item(todo_id)
        if not existing:
            _raise_http_status(404, f"TODO '{todo_id}' not found")
        try:
            _raise_if_todo_executing(schedule_db, todo_id, target_user_id, settings)
        except HTTPException as exc:
            # See complete_todo: forward the 409, let real faults propagate.
            _raise_http_status(exc.status_code, str(exc.detail))
        with todo_manager.atomic_update(target_user_id) as todo_list:
            item = todo_list.get_item(todo_id)
            if not item:
                _raise_http_status(404, f"TODO '{todo_id}' not found")
            success = todo_list.update_item(
                todo_id=todo_id,
                task=patch.get("task"),
                status=status,
                notes=patch.get("notes"),
                scheduled_for=scheduled_for,
                clear_schedule=clear_schedule,
                thread_id=patch.get("thread_id"),
                recurrence=recurrence,
                clear_recurrence=clear_recurrence,
            )
            if not success:
                _raise_http_status(404, f"TODO '{todo_id}' not found")
            if status == TodoStatus.DONE:
                updated = todo_list.get_item(todo_id)
                if updated:
                    _reschedule_recurring_done(todo_list, updated, todo_id)
            updated_item = todo_list.get_item(todo_id)
        if updated_item is None:
            _raise_http_status(404, f"TODO '{todo_id}' not found after update")
        todo_manager.sync_schedule_to_db(target_user_id, todo_id, schedule_db)
        return _todo_to_response(updated_item).model_dump(mode="json")

    async def update_settings(self, *, user_id: Optional[str] = None, **kwargs) -> dict:
        self._require_admin()
        from fastapi import HTTPException

        from ..api.routers.settings import apply_server_settings_update
        from ..api.schemas.settings import ServerSettingsUpdate

        from pydantic import ValidationError

        # One canonical applier shared with PATCH /settings: atomic 0600 quoted env
        # write, os.environ sync, settings-cache clear, agent re-bind, graph rebuild,
        # and restart reporting all live there so this path cannot drift from the route.
        try:
            updates = ServerSettingsUpdate(**kwargs)
            return apply_server_settings_update(
                updates,
                settings=self._settings(),
                agent=self.agent,
                get_settings_fn=self.settings_fn,
            )
        except ValidationError as exc:
            # A wrong-typed value (`/env set TWITCH_PULSE_ENABLED maybe`) fails
            # the update model itself; the HTTP shape answers that with a 422,
            # so the in-process shape must not answer it with a stack trace.
            first = exc.errors()[0]
            loc = ".".join(str(part) for part in first.get("loc", ())) or "value"
            _raise_http_status(400, f"Invalid value for {loc}: {first.get('msg', 'invalid value')}")
        except HTTPException as exc:
            # The applier raises only 400s a user can act on (unknown setting
            # keys, invalid values, a provider with no key slot); forward those
            # as the user-facing status. Anything else is a genuine fault and
            # propagates to the dispatcher's logger.exception handler.
            _raise_http_status(exc.status_code, str(exc.detail))

    async def test_llm_provider_config(
        self,
        request: dict,
        *,
        user_id: Optional[str] = None,
    ) -> dict:
        self._require_admin()
        # Same probe as POST /settings/llm/test (incl. its admin gate above):
        # credential resolution falls through vault -> settings -> environment
        # when the request carries no api_key.
        from ..api.routers.settings import _test_llm_provider_config
        from ..api.schemas.settings import LLMProviderTestRequest

        response = await _test_llm_provider_config(
            LLMProviderTestRequest(**request),
            settings=self._settings(),
            vault=getattr(self.agent, "credential_vault", None),
            owner_user_id=self.user.id,
        )
        return response.model_dump(mode="json")

    # --- CLIProxy subscription OAuth (in-process twins) -------------------
    # Same module-scope bodies as the /cliproxy routes (the
    # `_available_models` precedent), so the two TurnExecutor shapes cannot
    # drift; management errors map to the routes' wire statuses.

    def _cliproxy_client_or_400(self):
        from ..api.routers.cliproxy import management_client_from_settings

        client = management_client_from_settings(self._settings())
        if client is None:
            _raise_http_status(
                400,
                "CLIProxy management is not configured; set "
                "CLIPROXY_MANAGEMENT_URL and CLIPROXY_MANAGEMENT_KEY first",
            )
        return client

    @staticmethod
    def _cliproxy_raise(error: Exception) -> NoReturn:
        from ..api.routers.cliproxy import management_error_status
        from ..cliproxy.management_client import CLIProxyManagementError

        if isinstance(error, CLIProxyManagementError):
            _raise_http_status(management_error_status(error), str(error))
        _raise_http_status(502, str(error))

    def _cliproxy_spec_or_404(self, provider: str):
        from ..cliproxy.catalog import get_cliproxy_provider

        spec = get_cliproxy_provider(provider)
        if spec is None:
            _raise_http_status(404, f"Unknown CLIProxy provider: {provider}")
        return spec

    async def cliproxy_auth_files(
        self,
        provider: Optional[str] = None,
        *,
        user_id: Optional[str] = None,
    ) -> list[dict]:
        # user_id is accepted for HTTP-twin signature parity; this client's
        # acting user was fixed at construction.
        self._require_admin()
        from ..cliproxy.management_client import (
            CLIProxyManagementError,
            auth_entry_matches_spec,
        )

        client = self._cliproxy_client_or_400()
        try:
            files = await client.list_auth_files()
        except CLIProxyManagementError as error:
            self._cliproxy_raise(error)
        if provider:
            spec = self._cliproxy_spec_or_404(provider)
            files = [
                entry for entry in files if auth_entry_matches_spec(entry, spec)
            ]
        return files

    async def cliproxy_oauth_start(
        self, provider: str, *, user_id: Optional[str] = None
    ) -> dict:
        self._require_admin()
        from ..cliproxy.management_client import CLIProxyManagementError

        spec = self._cliproxy_spec_or_404(provider)
        client = self._cliproxy_client_or_400()
        try:
            started = await client.start_oauth(spec)
        except CLIProxyManagementError as error:
            self._cliproxy_raise(error)
        return {
            "provider": spec.id,
            "flow": spec.flow,
            "url": started["url"],
            "state": started["state"],
        }

    async def cliproxy_oauth_callback(
        self,
        provider: str,
        *,
        redirect_url: Optional[str] = None,
        code: Optional[str] = None,
        state: Optional[str] = None,
        user_id: Optional[str] = None,
    ) -> dict:
        self._require_admin()
        from ..cliproxy.management_client import CLIProxyManagementError

        spec = self._cliproxy_spec_or_404(provider)
        if not redirect_url and not (code and state):
            _raise_http_status(400, "Provide redirect_url or code and state")
        client = self._cliproxy_client_or_400()
        try:
            await client.oauth_callback(
                spec, redirect_url=redirect_url, code=code, state=state
            )
        except CLIProxyManagementError as error:
            self._cliproxy_raise(error)
        return {"status": "ok"}

    async def cliproxy_oauth_status(
        self, state: str, provider: str, *, user_id: Optional[str] = None
    ) -> dict:
        self._require_admin()
        from ..api.routers.cliproxy import confirmed_oauth_status
        from ..cliproxy.management_client import CLIProxyManagementError

        client = self._cliproxy_client_or_400()
        try:
            status, detail = await confirmed_oauth_status(
                client, state, provider
            )
        except CLIProxyManagementError as error:
            self._cliproxy_raise(error)
        return {"status": status, "detail": detail}

    async def cliproxy_models(
        self, *, user_id: Optional[str] = None
    ) -> list[dict]:
        self._require_admin()
        from ..api.routers.cliproxy import list_cliproxy_models
        from ..cliproxy.management_client import CLIProxyManagementError

        client = self._cliproxy_client_or_400()
        settings = self._settings()
        management_url = (
            getattr(settings, "cliproxy_management_url", None) or ""
        ).strip()
        try:
            return await list_cliproxy_models(client, management_url)
        except CLIProxyManagementError as error:
            self._cliproxy_raise(error)
        except httpx.HTTPError as error:
            _raise_http_status(502, f"CLIProxy model list failed: {error}")

    async def cliproxy_verify_credential(
        self,
        provider: str,
        *,
        model: Optional[str] = None,
        user_id: Optional[str] = None,
    ) -> dict:
        self._require_admin()
        from ..api.routers.cliproxy import verify_cliproxy_credential
        from ..cliproxy.management_client import CLIProxyManagementError

        spec = self._cliproxy_spec_or_404(provider)
        client = self._cliproxy_client_or_400()
        settings = self._settings()
        management_url = (
            getattr(settings, "cliproxy_management_url", None) or ""
        ).strip()
        try:
            verdict, detail = await verify_cliproxy_credential(
                client,
                management_url,
                spec,
                model=str(model or ""),
                settings=settings,
                vault=getattr(self.agent, "credential_vault", None),
                owner_user_id=self.user.id,
            )
        except CLIProxyManagementError as error:
            self._cliproxy_raise(error)
        except httpx.HTTPError as error:
            # Not reaching the proxy says nothing about the credential.
            return {
                "verdict": "inconclusive",
                "detail": f"CLIProxy verification could not run: {error}",
            }
        return {"verdict": verdict, "detail": detail}

    async def cliproxy_apply_route(
        self, provider: str, model: str, *, user_id: Optional[str] = None
    ) -> dict:
        self._require_admin()
        from fastapi import HTTPException

        from ..api.routers.cliproxy import perform_apply_route
        from ..api.schemas.cliproxy import CLIProxyApplyRouteRequest

        # Global scope only: the chain has no thread apply, and a thread
        # apply through this facade would skip the thread-access gate.
        request = CLIProxyApplyRouteRequest(provider=provider, model=model)
        try:
            response = await perform_apply_route(
                request,
                settings=self._settings(),
                agent=self.agent,
                get_settings_fn=self.settings_fn,
                admin=self.user,
            )
        except HTTPException as error:
            _raise_http_status(error.status_code, str(error.detail))
        return response.model_dump(mode="json")

    async def update_thread_config(
        self,
        thread_id: str,
        *,
        user_id: Optional[str] = None,
        **kwargs,
    ) -> dict:
        from .thread_config import ThreadConfig, ThreadLLMConfig

        self._require_thread_access(thread_id)
        if user_id is not None:
            self._checked_user_id(user_id)

        tc = self.agent.thread_config_manager.get_config(thread_id)
        if tc is None:
            tc = ThreadConfig(thread_id=thread_id)

        if "enabled_tools" in kwargs and kwargs["enabled_tools"] is not None:
            enabled_tools = list(kwargs["enabled_tools"])
            if self.user.role != "admin":
                from ..tools import (
                    ADMIN_ONLY_TOOL_NAMES,
                    DEVELOPER_ONLY_TOOL_NAMES,
                )

                blocked = ADMIN_ONLY_TOOL_NAMES.intersection(enabled_tools)
                if blocked:
                    _raise_http_status(
                        403,
                        "Admin-only tools cannot be enabled by this user: "
                        f"{sorted(blocked)}",
                    )
                blocked = DEVELOPER_ONLY_TOOL_NAMES.intersection(enabled_tools)
                if blocked:
                    _raise_http_status(
                        403,
                        "Developer-only diagnostic tools cannot be enabled by this user: "
                        f"{sorted(blocked)}",
                    )
            tc.enabled_tools = enabled_tools
        if "disabled_tools" in kwargs and kwargs["disabled_tools"] is not None:
            tc.disabled_tools = list(kwargs["disabled_tools"])
        if "llm_config" in kwargs and kwargs["llm_config"] is not None:
            llm_data = dict(kwargs["llm_config"])
            if tc.llm_config is None:
                tc.llm_config = ThreadLLMConfig(
                    **{key: value for key, value in llm_data.items() if value is not None}
                )
            else:
                for key, value in llm_data.items():
                    setattr(tc.llm_config, key, value)
        if kwargs.get("clear_memory_char_limit"):
            tc.memory_char_limit = None
        elif "memory_char_limit" in kwargs and kwargs["memory_char_limit"] is not None:
            tc.memory_char_limit = int(kwargs["memory_char_limit"])
        if kwargs.get("clear_sequential_tool_execution"):
            tc.sequential_tool_execution = None
        elif (
            "sequential_tool_execution" in kwargs
            and kwargs["sequential_tool_execution"] is not None
        ):
            tc.sequential_tool_execution = bool(kwargs["sequential_tool_execution"])
        if kwargs.get("clear_hooks_enabled"):
            tc.hooks_enabled = None
        elif "hooks_enabled" in kwargs and kwargs["hooks_enabled"] is not None:
            tc.hooks_enabled = bool(kwargs["hooks_enabled"])
        if kwargs.get("clear_hook_overrides"):
            tc.hook_overrides = {}
        elif "hook_overrides" in kwargs and kwargs["hook_overrides"] is not None:
            tc.hook_overrides = {
                str(k): bool(v) for k, v in dict(kwargs["hook_overrides"]).items()
            }

        if not self.agent.thread_config_manager.save_config(tc):
            _raise_http_status(500, "Failed to save thread config")
        self.agent.invalidate_thread_config_cache(thread_id)
        return tc.model_dump(mode="json") | {"has_customizations": tc.has_customizations()}

    async def get_env_vars(self, *, user_id: Optional[str] = None) -> dict:
        self._require_admin()
        # Shared serializer with GET /settings/env so the two TurnExecutor shapes
        # cannot drift: the prior hand-built body masked only the allowlist (not
        # the suffix superset) and labeled with a naive key.upper(), so a
        # suffix-style secret like groq_api_key leaked raw on the in-process path.
        from ..api.routers.settings import serialize_env_entries

        return serialize_env_entries(self._settings())

    async def get_env_var(self, key: str, *, user_id: Optional[str] = None) -> dict:
        self._require_admin()
        from fastapi import HTTPException

        from ..api.routers.settings import serialize_env_var

        # Shared serializer with GET /settings/env/{key} (the serialize_env_entries
        # idiom): the prior hand-built body skipped secret masking entirely,
        # mislabeled env_var with a naive key.upper(), and 404'd known-but-unset
        # settings as "Unknown". reveal=True matches the HTTP client, which
        # requests the unmasked value for the explicit `/env get` admin reveal.
        try:
            return serialize_env_var(
                self._settings(), key, reveal=True, actor=str(user_id or "command")
            )
        except HTTPException as exc:
            _raise_http_status(exc.status_code, str(exc.detail))

    async def list_available_models(
        self,
        provider: Optional[str] = None,
        user_id: Optional[str] = None,
        *,
        api_key: Optional[str] = None,
        base_url: Optional[str] = None,
    ) -> list[dict]:
        # Delegates to the SAME module-scope implementation as GET/POST
        # /models/available (the two-TurnExecutor-shape invariant, mirroring
        # test_llm_provider_config above): the previous hand-copied body had
        # drifted, missing the CLIProxy /v1 base normalization and the
        # provider probe headers. ``api_key``/``base_url`` are the /provider
        # setup flow's ephemeral overrides (nothing stored, never logged).
        from ..api.routers.settings import _available_models

        return await _available_models(
            provider=provider,
            base_url=base_url,
            api_key=api_key,
            vault=getattr(self.agent, "credential_vault", None),
            owner_user_id=self.user.id,
            settings=self._settings(),
        )


class CommandService:
    """Registry and dispatcher for Nymeria slash commands."""

    def __init__(self) -> None:
        self._commands: dict[str, CommandDefinition] = {}
        self._path_index: dict[tuple[str, ...], str] = {}
        self._aliases: dict[tuple[str, ...], str] = {}
        # Injected aliases (#133): alias path -> the argument tokens the
        # expansion contributes AFTER the target's canonical path. Every key
        # here is also a key of _aliases; this map only adds the payload.
        self._alias_injections: dict[tuple[str, ...], tuple[str, ...]] = {}
        self._register_defaults()
        self.validate_registry()

    def register(
        self,
        name: str,
        handler: Any = None,
        *,
        description: str,
        category: str,
        id: str | None = None,
        usage: str | None = None,
        aliases: tuple[str | tuple[str, ...], ...] = (),
        subcommands: tuple[str, ...] = (),
        scope: CommandScope = "global",
        surfaces: tuple[CommandSurface, ...] = DEFAULT_GLOBAL_SURFACES,
        blocked_surfaces: tuple[CommandSurface, ...] = (),
        blocked_reason: str | None = None,
        agent_allowed: bool = True,
        requires_thread: bool = False,
        requires_admin: bool = False,
        mutates_state: bool = False,
        danger_level: CommandDangerLevel = "safe",
        execution_kind: CommandExecutionKind = "command",
        note: str | None = None,
        hidden: bool = False,
        examples: tuple[str, ...] = (),
        params: tuple[CommandParam, ...] | None = None,
        injected_aliases: Mapping[str, str | tuple[str, ...]] | None = None,
    ) -> None:
        del handler
        path = _normalize_path(name)
        if not path:
            raise ValueError("Command path cannot be empty")
        if params is not None:
            if usage is not None:
                raise ValueError(
                    f"Command {name!r} declares params; usage is generated "
                    "and cannot be hand-written"
                )
            validate_params((id or _id_for_path(path)).lower(), params)
            usage = generated_usage(path, params)
        command_id = (id or _id_for_path(path)).lower()
        if command_id in self._commands:
            raise ValueError(f"Duplicate command id: {command_id}")
        if path in self._path_index:
            existing = self._path_index[path]
            raise ValueError(
                f"Duplicate command path: /{_display_path(path)} "
                f"({existing} and {command_id})"
            )

        # Injected aliases (#133): an alias whose expansion contributes
        # argument tokens after the target path. Tokens must be single
        # shlex-safe words so the parse-time args/rest composition is
        # trivially correct; anything richer (quoted prompts) is a recorded
        # non-goal of the mechanism.
        injection_paths: dict[tuple[str, ...], tuple[str, ...]] = {}
        for spelling, injected in (injected_aliases or {}).items():
            injected_path = _normalize_path(spelling)
            if not injected_path:
                raise ValueError(f"Empty injected alias for command {command_id}")
            injected_tokens = (
                (injected,) if isinstance(injected, str) else tuple(injected)
            )
            if not injected_tokens:
                raise ValueError(
                    f"Injected alias {spelling!r} for {command_id} injects nothing; "
                    "declare it as a plain alias instead"
                )
            for token in injected_tokens:
                if (
                    not token
                    or any(ch.isspace() for ch in token)
                    or set(token) & {'"', "'"}
                ):
                    raise ValueError(
                        f"Injected alias {spelling!r} for {command_id}: token "
                        f"{token!r} must be a single unquoted word"
                    )
            injection_paths[injected_path] = injected_tokens

        alias_paths = tuple(_normalize_path(alias) for alias in aliases) + tuple(
            injection_paths
        )
        for alias_path in alias_paths:
            if not alias_path:
                raise ValueError(f"Empty alias for command {command_id}")
            existing_path_id = self._path_index.get(alias_path)
            if existing_path_id and existing_path_id != command_id:
                raise ValueError(
                    f"Alias {_alias_display(alias_path)} for {command_id} "
                    f"conflicts with command path {existing_path_id}"
                )
            existing_alias_id = self._aliases.get(alias_path)
            if existing_alias_id and existing_alias_id != command_id:
                raise ValueError(
                    f"Alias {_alias_display(alias_path)} for {command_id} "
                    f"already points to {existing_alias_id}"
                )

        definition = CommandDefinition(
            id=command_id,
            path=path,
            description=description,
            usage=usage or _usage_for_path(path),
            category=category,
            aliases=alias_paths,
            subcommands=subcommands,
            scope=scope,
            surfaces=surfaces,
            blocked_surfaces=blocked_surfaces,
            blocked_reason=blocked_reason,
            agent_allowed=agent_allowed,
            requires_thread=requires_thread,
            requires_admin=requires_admin,
            mutates_state=mutates_state,
            danger_level=danger_level,
            execution_kind=execution_kind,
            note=note,
            hidden=hidden,
            examples=examples,
            params=params,
        )
        self._commands[command_id] = definition
        self._path_index[path] = command_id
        for alias_path in alias_paths:
            self._aliases[alias_path] = command_id
        self._alias_injections.update(injection_paths)

    def _register_defaults(self) -> None:
        register_default_commands(self)

    def validate_registry(self) -> None:
        seen_ids: set[str] = set()
        seen_paths: dict[tuple[str, ...], str] = {}
        seen_aliases: dict[tuple[str, ...], str] = {}
        for command_id, cmd in self._commands.items():
            if command_id in seen_ids:
                raise ValueError(f"Duplicate command id: {command_id}")
            seen_ids.add(command_id)
            existing_path = seen_paths.get(cmd.path)
            if existing_path and existing_path != command_id:
                raise ValueError(
                    f"Duplicate command path: /{cmd.name} "
                    f"({existing_path} and {command_id})"
                )
            seen_paths[cmd.path] = command_id
            if cmd.path[0] in AGENT_BLOCKED and cmd.agent_allowed:
                raise ValueError(f"Agent-blocked command is agent-allowed: {command_id}")
            if cmd.requires_admin and cmd.danger_level == "dangerous" and not cmd.mutates_state:
                raise ValueError(f"Dangerous admin command must declare mutates_state: {command_id}")
            overlap = set(cmd.blocked_surfaces) & set(cmd.surfaces)
            if overlap:
                raise ValueError(
                    f"Command {command_id} is both visible and blocked on: "
                    f"{', '.join(sorted(overlap))}"
                )
            if cmd.blocked_reason and not cmd.blocked_surfaces:
                raise ValueError(
                    f"Command {command_id} has a blocked_reason but no blocked_surfaces"
                )
            if cmd.params is not None:
                validate_params(command_id, cmd.params)
            for alias_path in cmd.aliases:
                path_conflict = seen_paths.get(alias_path)
                if path_conflict and path_conflict != command_id:
                    raise ValueError(
                        f"Alias {_alias_display(alias_path)} for {command_id} "
                        f"conflicts with command path {path_conflict}"
                    )
                alias_conflict = seen_aliases.get(alias_path)
                if alias_conflict and alias_conflict != command_id:
                    raise ValueError(
                        f"Alias {_alias_display(alias_path)} for {command_id} "
                        f"conflicts with alias for {alias_conflict}"
                    )
                seen_aliases[alias_path] = command_id
        # Naming canon (#131): built-in catalog only; runtime registrations
        # (tests, plugins) are exempt by design. Rules and constants:
        # core/command_naming.py + docs/private/command-style-guide.md.
        validate_command_naming(self._commands, AGENT_BLOCKED)

    def _resolve_agent(self, agent: Any | None = None) -> Any | None:
        if agent is not None:
            return agent
        try:
            from .agent import get_current_agent

            return get_current_agent()
        except Exception:  # noqa: BLE001
            return None

    def _skill_manager(self, agent: Any | None = None) -> Any | None:
        resolved = self._resolve_agent(agent)
        if resolved is None:
            return None
        return getattr(resolved, "skill_manager", None)

    def _visible_slash_skills(
        self,
        user_id: str | None,
        *,
        agent: Any | None = None,
        include_internal: bool = False,
    ) -> list[Any]:
        skill_manager = self._skill_manager(agent)
        if skill_manager is None:
            return []
        try:
            skills = skill_manager.list_installed(user_id=user_id)
        except Exception as e:  # noqa: BLE001
            logger.warning("failed to list installed skills for slash commands: %s", e)
            return []
        return [
            skill
            for skill in skills
            if include_internal or not getattr(skill, "is_internal", False)
        ]

    def list_commands(
        self,
        source: str | None = "user",
        *,
        actor: str | None = None,
        surface: str | None = None,
        is_admin: bool | None = None,
        include_hidden: bool = False,
        user_id: str | None = None,
        agent: Any | None = None,
        include_user_aliases: bool = False,
    ) -> list[CommandInfo]:
        effective_actor = (actor or _actor_from_source(source)).lower()
        effective_surface = surface or _surface_from_source(source)
        definitions = list(self._commands.values())
        infos = [
            self._to_info(cmd)
            for cmd in sorted(definitions, key=lambda c: (c.category, c.name))
            if self._is_visible(
                cmd,
                actor=effective_actor,
                surface=effective_surface,
                is_admin=is_admin,
                include_hidden=include_hidden,
            )
        ]
        # Opt-in, not keyed on user_id alone: user_id also gates skill
        # visibility, and the /help renderers pass it without wanting a
        # per-listing accounts.db read for an annotation they never show.
        if include_user_aliases and user_id:
            infos = self._with_user_aliases(infos, user_id)
        return infos

    def user_alias_status(self, row: Any) -> str | None:
        """One liveness verdict for a user-alias row (#133).

        Shared by the catalog annotation and ``/alias list`` so the two
        cannot drift. ``None`` means live; otherwise ``"inert"`` (failed its
        authoring stamp), ``"shadowed"`` (the catalog claims the name now),
        or ``"target_gone"``.
        """
        if not getattr(row, "stamp_valid", False):
            return "inert"
        if self._builtin_claims_token(row.name):
            return "shadowed"
        if row.command_id not in self._commands:
            return "target_gone"
        return None

    def _with_user_aliases(
        self, infos: list[CommandInfo], user_id: str
    ) -> list[CommandInfo]:
        """Annotate each command with the caller's own aliases for it (#133).

        Only LIVE rows ride (``user_alias_status``). A store fault degrades
        to no annotation; the catalog listing must never 500 over the alias
        table.
        """
        try:
            from .user_aliases import get_user_aliases_repo

            rows = get_user_aliases_repo().list_aliases(user_id)
        except Exception as e:  # noqa: BLE001
            logger.warning(
                "user-alias catalog annotation failed for %s: %s", user_id, e
            )
            return infos
        by_target: dict[str, list[str]] = {}
        for row in rows:
            if self.user_alias_status(row) is None:
                by_target.setdefault(row.command_id, []).append(row.name)
        if not by_target:
            return infos
        return [
            dataclasses.replace(
                info,
                user_aliases=[f"/{name}" for name in sorted(by_target[info.id])],
            )
            if info.id in by_target
            else info
            for info in infos
        ]

    def _is_visible(
        self,
        cmd: CommandDefinition,
        *,
        actor: str,
        surface: str | None,
        is_admin: bool | None,
        include_hidden: bool,
    ) -> bool:
        if cmd.hidden and not include_hidden:
            return False
        if actor == "agent" and not cmd.agent_allowed:
            return False
        if surface and surface not in cmd.surfaces:
            return False
        if cmd.requires_admin and is_admin is False:
            return False
        return True

    def _to_info(self, cmd: CommandDefinition) -> CommandInfo:
        return CommandInfo(
            name=cmd.name,
            description=cmd.description,
            usage=cmd.usage,
            category=cmd.category,
            subcommands=list(cmd.subcommands or self._subcommands_for_path(cmd.path)),
            id=cmd.id,
            path=list(cmd.path),
            aliases=[_alias_display(alias) for alias in cmd.aliases],
            scope=cmd.scope,
            surfaces=list(cmd.surfaces),
            blocked_surfaces=list(cmd.blocked_surfaces),
            blocked_reason=cmd.blocked_reason,
            agent_allowed=cmd.agent_allowed,
            requires_thread=cmd.requires_thread,
            requires_admin=cmd.requires_admin,
            mutates_state=cmd.mutates_state,
            danger_level=cmd.danger_level,
            execution_kind=cmd.execution_kind,
            note=cmd.note,
            examples=list(cmd.examples),
            params=params_to_payload(cmd.params),
        )

    def _subcommands_for_path(self, path: tuple[str, ...]) -> list[str]:
        # Displayed spellings (rule 4): this list is rendered in help cards,
        # usage errors, and GET /commands, never matched against input.
        if len(path) != 1:
            return []
        prefix = path[0]
        return sorted(
            {
                cmd.path[1].replace("_", "-")
                for cmd in self._commands.values()
                if len(cmd.path) > 1 and cmd.path[0] == prefix
            }
        )

    def _help_markdown(
        self,
        source: str | None,
        *,
        actor: str | None = None,
        surface: str | None = None,
        is_admin: bool | None = None,
        user_id: str | None = None,
        agent: Any | None = None,
    ) -> str:
        commands = self.list_commands(
            source,
            actor=actor,
            surface=surface,
            is_admin=is_admin,
            user_id=user_id,
            agent=agent,
        )
        lines = ["## Nymeria Slash Commands", ""]
        categories = sorted({cmd.category for cmd in commands}, key=str.casefold)
        for category in categories:
            lines.append(f"### {category}")
            lines.append("")
            lines.append("| Command | Usage | Description |")
            lines.append("| --- | --- | --- |")
            for cmd in [c for c in commands if c.category == category]:
                desc = cmd.description
                if cmd.aliases:
                    desc = f"{desc.rstrip('.')}. Alias: {cmd.aliases[0]}"
                if cmd.note:
                    desc = f"{desc.rstrip('.')}. {cmd.note}"
                usage = cmd.usage.replace("|", "\\|")
                lines.append(f"| `/{cmd.name}` | `{usage}` | {desc} |")
            lines.append("")
        lines.append("Values with spaces can be quoted, for example `/memory save color \"deep blue\"`.")
        return "\n".join(lines).strip()

    def _help_index_markdown(
        self,
        source: str | None,
        *,
        actor: str | None = None,
        surface: str | None = None,
        is_admin: bool | None = None,
        user_id: str | None = None,
        agent: Any | None = None,
    ) -> str:
        """Compact category index: root names only, with a drill-down hint."""
        commands = self.list_commands(
            source,
            actor=actor,
            surface=surface,
            is_admin=is_admin,
            user_id=user_id,
            agent=agent,
        )
        by_category: dict[str, set[str]] = {}
        for cmd in commands:
            # Displayed spelling (rule 4); `path` is the stored form.
            by_category.setdefault(cmd.category, set()).add(
                _display_path(cmd.path[:1])
            )
        lines = ["## Nymeria Slash Commands", ""]
        for category in sorted(by_category, key=str.casefold):
            roots = ", ".join(
                f"`/{root}`" for root in sorted(by_category[category])
            )
            lines.append(f"**{category}:** {roots}")
        lines.append("")
        lines.append(
            "Type `/help <command>` for one command's usage and subcommands, "
            "or `/help all` for the full table. Values with spaces can be "
            'quoted, for example `/memory save color "deep blue"`.'
        )
        return "\n".join(lines).strip()

    def _resolve_help_target(
        self, tokens: tuple[str, ...]
    ) -> tuple[CommandDefinition | None, str | None]:
        """Resolve /help arguments to a definition or a group-root prefix."""
        normalized = tuple(
            _normalize_token(token.lstrip("/"))
            for token in tokens
            if _normalize_token(token.lstrip("/"))
        )
        if not normalized:
            return None, None
        canonical, _, _ = self._expand_alias_prefix(normalized)
        for prefix_len in range(len(canonical), 0, -1):
            candidate = canonical[:prefix_len]
            command_id = self._path_index.get(candidate) or self._aliases.get(candidate)
            if command_id is not None:
                return self._commands[command_id], None
        root = canonical[0]
        if self._prefix_subcommands(root):
            return None, root
        return None, None

    def _command_help_markdown(
        self,
        tokens: tuple[str, ...],
        *,
        actor: str | None = None,
        surface: str | None = None,
        is_admin: bool | None = None,
    ) -> str | None:
        """One command's help card, or None when nothing matches."""
        definition, group_root = self._resolve_help_target(tokens)
        if definition is None and group_root is None:
            return None
        if definition is not None and definition.hidden:
            # A hidden command stays out of every listing, help cards
            # included; the caller renders the unknown-command error.
            return None

        title = definition.name if definition is not None else group_root
        lines = [f"## /{title}", ""]
        if definition is not None:
            lines.append(definition.description)
            lines.append("")
            lines.append(f"Usage: `{definition.usage}`")
            if definition.params:
                lines.append("")
                lines.append("| Argument | Required | Description |")
                lines.append("| --- | --- | --- |")
                for param in definition.params:
                    label = param.display
                    if param.aliases:
                        label += " (" + ", ".join(param.aliases) + ")"
                    detail = param.description
                    if param.choices:
                        valid = "Valid: " + ", ".join(param.choices) + "."
                        detail = f"{detail} {valid}".strip()
                    lines.append(
                        "| {} | {} | {} |".format(
                            label.replace("|", "\\|"),
                            "required" if param.required else "optional",
                            detail.replace("|", "\\|"),
                        )
                    )
            if definition.note:
                lines.append("")
                lines.append(definition.note)
            access = []
            if definition.requires_admin:
                access.append("admin only")
            if definition.requires_thread:
                access.append("requires an active thread")
            if not definition.agent_allowed:
                access.append("not available to the agent")
            if definition.blocked_surfaces:
                access.append(
                    "not available on " + ", ".join(definition.blocked_surfaces)
                )
            if access:
                lines.append("")
                lines.append("Access: " + "; ".join(access) + ".")
            if definition.aliases:
                aliases = ", ".join(
                    f"`{_alias_display(alias)}`" for alias in definition.aliases
                )
                lines.append("")
                lines.append(f"Aliases: {aliases}")
        else:
            lines.append(f"`/{group_root}` requires a subcommand.")

        target_path = definition.path if definition is not None else (group_root,)
        if len(target_path) == 1:
            children = [
                cmd
                for cmd in sorted(self._commands.values(), key=lambda c: c.name)
                if len(cmd.path) > 1
                and cmd.path[0] == target_path[0]
                # ``surfaces`` is a DISCOVERY filter and never governs
                # execution, so the card filters children by the enforced
                # axes only (hidden/agent/admin via _is_visible with
                # surface=None, plus blocked_surfaces). Otherwise the card
                # for /hook on telegram showed zero subcommands while the
                # generic passthrough made all of them executable there.
                and self._is_visible(
                    cmd,
                    actor=actor or "user",
                    surface=None,
                    is_admin=is_admin,
                    include_hidden=False,
                )
                and (not surface or surface not in cmd.blocked_surfaces)
            ]
            if children:
                lines.append("")
                lines.append("| Subcommand | Usage | Description |")
                lines.append("| --- | --- | --- |")
                for cmd in children:
                    usage = cmd.usage.replace("|", "\\|")
                    label = _display_path(cmd.path[1:])
                    lines.append(f"| {label} | `{usage}` | {cmd.description} |")

        if definition is not None and definition.examples:
            lines.append("")
            lines.append("Examples:")
            for example in definition.examples:
                lines.append(f"- `{example}`")
        return "\n".join(lines).strip()

    def _expand_alias_prefix(self, tokens: tuple[str, ...]) -> tuple[tuple[str, ...], int, int]:
        """Rewrite a leading alias into the command path it stands for.

        This runs BEFORE longest-prefix matching, and it is a security control,
        not a convenience. Aliases are registered against whole paths, so
        ``("hooks",)`` existed as an alias of ``("hook",)`` while
        ``("hooks", "disable")`` existed as nothing at all. Longest-prefix
        matching therefore resolved ``/hooks disable X`` to the PARENT
        definition, which is ``agent_allowed=True`` because listing hooks is
        fine, and left ``disable`` sitting in ``args`` for the parent handler to
        re-dispatch internally. The ``agent_allowed=False`` on
        ``hook.disable`` was never consulted. Same for the other five mutating
        hook subcommands, and for any future family that pairs an alias with
        restricted subcommands.

        Expanding first makes an alias a true synonym: the tokens that reach
        matching are the canonical ones, so the subcommand definition and its
        flags are what the gate sees.

        Returns the canonical tokens plus how many raw tokens the alias
        consumed and how many canonical tokens it produced, because those can
        differ (``/hook_disable`` is one raw token standing for two) and the
        caller still has to slice the RAW input to recover arguments.

        Injected aliases (#133) also produce ARGUMENT tokens after the target
        path (``/tools_core`` stands for ``tools list`` plus ``core``); they
        are part of the produced count, and ``_parse_for_registry`` binds any
        of them left past the matched path as leading arguments. An injection
        may legally compose into a DEEPER registered path, in which case the
        deeper command simply matches; every gate reads the resolved
        definition either way, so an injected alias can never widen access.
        """
        # Scan down from the longest registered PATH as well as the longest
        # alias: the break-on-path guard below must get its chance on a real
        # path even when that path is LONGER than every alias, or an alias
        # that is a proper prefix of it would hijack the path with its tail
        # demoted to arguments (the /hooks disable class of bug again, from
        # the other direction; #131 review F6).
        max_scan = max(
            max((len(alias) for alias in self._aliases), default=0),
            max((len(path) for path in self._path_index), default=0),
        )
        for alias_len in range(min(len(tokens), max_scan), 0, -1):
            candidate = tokens[:alias_len]
            if candidate in self._path_index:
                # A real path always wins over an alias, so stop here rather
                # than letting a shorter alias rewrite a genuine command.
                break
            command_id = self._aliases.get(candidate)
            if command_id is None:
                continue
            path = self._commands[command_id].path
            # An injected alias (#133) contributes argument tokens after the
            # target path; they count toward the produced length so the
            # caller's raw/canonical back-translation stays coherent.
            produced = path + self._alias_injections.get(candidate, ())
            return produced + tokens[alias_len:], alias_len, len(produced)
        return tokens, 0, 0

    def _builtin_claims_token(self, token: str) -> bool:
        """True when the catalog owns this leading token.

        A user alias must lose to every registered path and built-in alias,
        including as the ROOT of longer spellings: `/restart` has no bare
        root command, but a user alias named `restart` would still hijack
        `/restart api` (the F6 proper-prefix hazard, user-table edition).
        """
        key = (token,)
        if key in self._path_index or key in self._aliases:
            return True
        return any(path[0] == token for path in self._path_index) or any(
            alias[0] == token for alias in self._aliases
        )

    def _expand_user_alias(self, ctx: CommandContext, raw: str) -> str:
        """Rewrite a leading user-defined alias into its stored expansion.

        One expansion, never chained: the stored expansion was validated at
        create time to resolve against the CATALOG, so its first token is a
        canonical path token, not another user alias. The catalog always
        wins (`_builtin_claims_token`); a lookup or store fault degrades to
        no expansion, never to a failed dispatch. The stamp check lives in
        ``resolve_for_dispatch`` (the store's control consumption point).
        """
        user_id = getattr(ctx, "user_id", None)
        if not user_id:
            return raw
        command_text = raw.strip()
        if command_text.startswith("/"):
            command_text = command_text[1:].lstrip()
        if not command_text:
            return raw
        first = command_text.split(None, 1)[0]
        token = _normalize_token(first)
        if not token or self._builtin_claims_token(token):
            return raw
        try:
            from .user_aliases import get_user_aliases_repo

            alias = get_user_aliases_repo().resolve_for_dispatch(user_id, token)
        except Exception as e:  # noqa: BLE001 - alias faults must not break dispatch
            logger.warning("user-alias lookup failed for %s: %s", user_id, e)
            return raw
        if alias is None:
            return raw
        tail = _split_rest_after_tokens(command_text, 1)
        expansion = " ".join(alias.tokens)
        return f"/{expansion} {tail}".strip()

    def _parse_for_registry(self, raw: str) -> ParsedCommand:
        command_text = raw.strip()
        if command_text.startswith("/"):
            command_text = command_text[1:].lstrip()
        if not command_text:
            return ParsedCommand((), (), [], "", None)

        try:
            parts = shlex.split(command_text, posix=True)
        except ValueError:
            parts = command_text.split()
        tokens = tuple(_normalize_token(part) for part in parts if _normalize_token(part))
        if not tokens:
            return ParsedCommand((), (), [], "", None)

        canonical, alias_len, path_len = self._expand_alias_prefix(tokens)
        max_len = min(len(canonical), max((len(path) for path in self._path_index), default=1))
        for prefix_len in range(max_len, 0, -1):
            candidate = canonical[:prefix_len]
            command_id = self._path_index.get(candidate) or self._aliases.get(candidate)
            if command_id is None:
                continue
            definition = self._commands[command_id]
            # Back-translate the canonical match into raw tokens. An alias is
            # consumed whole or not at all, so anything matched beyond the
            # substituted path came from the raw tail one for one.
            raw_consumed = alias_len + max(0, prefix_len - path_len) if alias_len else prefix_len
            rest = _split_rest_after_tokens(command_text, raw_consumed)
            args = _split_args(rest)
            # Injected-alias leftovers (#133): expansion tokens past the
            # matched path are ARGUMENTS the alias carries (this is what a
            # plain alias structurally cannot do). They bind ahead of
            # whatever the user typed after the alias. Tokens are validated
            # single unquoted words at registration, so the rest-string
            # composition is a plain join.
            if alias_len and path_len > prefix_len:
                injected_left = list(canonical[prefix_len:path_len])
                args = injected_left + args
                rest = " ".join(injected_left + ([rest] if rest else []))
            return ParsedCommand(
                # Deliberately the tokens the user typed, not the canonical
                # ones, so "unknown subcommand for /hooks" names what they wrote.
                tokens=tokens,
                path=definition.path,
                args=args,
                rest=rest,
                definition=definition,
                matched_input_len=raw_consumed,
            )

        rest = _split_rest_after_tokens(command_text, 1)
        return ParsedCommand(
            tokens=tokens,
            path=(tokens[0],),
            args=_split_args(rest),
            rest=rest,
            definition=None,
            matched_input_len=1,
        )

    def _prefix_subcommands(self, prefix: str) -> list[str]:
        # Displayed spellings (rule 4). Only ever rendered or fed to difflib,
        # which is fuzzy enough that a hyphen costs a mistyped token nothing.
        return sorted(
            {
                path[1].replace("_", "-")
                for path in self._path_index
                if len(path) > 1 and path[0] == prefix
            }
        )

    def find_command(self, name: str) -> CommandInfo | None:
        """Look up one command (by path or alias) as a CommandInfo."""
        path = _normalize_path(name)
        if not path:
            return None
        command_id = self._path_index.get(path) or self._aliases.get(path)
        if command_id is None:
            return None
        return self._to_info(self._commands[command_id])

    def _suggest_roots(self, token: str) -> list[str]:
        """Nearest registered root names for a typo, aliases as a fallback.

        Canonical roots are tried first so a typo of "provider" suggests
        `/provider`, not the flat bot alias `/provider_set` alongside it.
        A matched root that cannot dispatch bare (a subcommand-only family
        like `restart`) expands to its full path when unique: suggesting a
        spelling that only produces another error is a dead end.
        """
        roots = sorted(
            {cmd.path[0] for cmd in self._commands.values() if not cmd.hidden}
        )
        matches = difflib.get_close_matches(token, roots, n=2, cutoff=0.6)
        if matches:
            return [self._expand_suggested_root(m) for m in matches]
        aliases = sorted({alias[0] for alias in self._aliases if len(alias) == 1})
        return difflib.get_close_matches(token, aliases, n=2, cutoff=0.6)

    def _expand_suggested_root(self, root: str) -> str:
        """Full path for a suggested root with no bare command, when unique."""
        if (root,) in self._path_index or (root,) in self._aliases:
            return root
        full_paths = {
            cmd.path
            for cmd in self._commands.values()
            if not cmd.hidden and cmd.path and cmd.path[0] == root
        }
        if len(full_paths) == 1:
            return " ".join(next(iter(full_paths)))
        return root

    @staticmethod
    def _did_you_mean(suggestions: list[str], *, prefix: str = "/") -> str:
        if not suggestions:
            return ""
        quoted = " or ".join(f"`{prefix}{s}`" for s in suggestions)
        return f" Did you mean {quoted}?"

    def _unknown_or_group_error(
        self, parsed: ParsedCommand, ctx: CommandContext | None = None
    ) -> CommandResult:
        if not parsed.tokens:
            return CommandResult(
                False, render_outcome("error", "Empty command. Try `/help`."), "", level="error"
            )

        root = parsed.tokens[0]
        valid_subcommands = self._prefix_subcommands(root)
        if valid_subcommands and len(parsed.tokens) == 1:
            valid = ", ".join(valid_subcommands)
            return CommandResult(
                False,
                render_outcome(
                    "error",
                    f"`/{root}` requires a subcommand. Valid: {valid}. "
                    f"See `/help {root}`.",
                ),
                root,
                level="error",
            )
        if valid_subcommands and len(parsed.tokens) > 1:
            if parsed.tokens[1] == "help":
                card = self._command_help_markdown(
                    (root,),
                    actor=ctx.effective_actor if ctx else "user",
                    surface=ctx.effective_surface if ctx else None,
                    is_admin=ctx.is_admin if ctx else None,
                )
                if card:
                    return CommandResult(True, card, root, level="info")
            suggestions = difflib.get_close_matches(
                parsed.tokens[1], valid_subcommands, n=2, cutoff=0.6
            )
            hint = self._did_you_mean(suggestions, prefix=f"/{root} ")
            valid = ", ".join(valid_subcommands)
            command_label = f"{root} {parsed.tokens[1]}".strip()
            return CommandResult(
                False,
                render_outcome(
                    "error",
                    f"Unknown subcommand `{parsed.tokens[1]}` for "
                    f"`/{root}`.{hint} Valid: {valid}.",
                ),
                command_label,
                level="error",
            )

        hint = self._did_you_mean(self._suggest_roots(root))
        return CommandResult(
            False,
            render_outcome("error", f"Unknown command `/{root}`.{hint} Use `/help`."),
            root,
            level="error",
        )

    async def execute(
        self,
        ctx: CommandContext,
        raw_command: str,
        *,
        api: Any | None = None,
    ) -> CommandResult:
        # User-defined aliases (#133) expand HERE and not in the parser:
        # execute() is the one dispatch seam that knows the user, and the
        # rewrite happens before parsing so every gate below reads the
        # resolved canonical definition, same as built-in aliases.
        raw_command = self._expand_user_alias(ctx, raw_command)
        parsed = self._parse_for_registry(raw_command)
        if parsed.definition is None:
            return self._unknown_or_group_error(parsed, ctx)

        definition = parsed.definition
        command_label = definition.name
        actor = ctx.effective_actor
        surface = ctx.effective_surface

        if surface and surface in definition.blocked_surfaces:
            reason = f" {definition.blocked_reason}" if definition.blocked_reason else ""
            available = ", ".join(definition.surfaces)
            return CommandResult(
                False,
                render_outcome(
                    "error",
                    f"Command `/{definition.name}` is not available "
                    f"on {surface}.{reason} Available on: {available}.",
                ),
                command_label,
                level="error",
            )

        if (
            definition.id != "help"
            and definition.executable
            and len(definition.path) == 1
            and [arg.lower() for arg in parsed.args] == ["help"]
        ):
            card = self._command_help_markdown(
                definition.path,
                actor=actor,
                surface=surface,
                is_admin=ctx.is_admin,
            )
            if card:
                return CommandResult(True, card, command_label, level="info")

        if actor == "agent" and definition.path[0] in AGENT_BLOCKED:
            return CommandResult(
                False,
                render_outcome(
                    "error",
                    f"Command `/{definition.path[0]}` is disabled for the agent "
                    "because it would interrupt or destroy the current conversation.",
                ),
                command_label,
                level="error",
            )

        if actor == "agent" and not definition.agent_allowed:
            return CommandResult(
                False,
                render_outcome(
                    "error", f"Command `/{definition.name}` is not available to the agent."
                ),
                command_label,
                level="error",
            )
        if definition.requires_admin and ctx.is_admin is False:
            return CommandResult(
                False,
                render_outcome(
                    "error", f"Command `/{definition.name}` requires an admin user."
                ),
                command_label,
                level="error",
            )
        if definition.requires_thread and not ctx.thread_id:
            return CommandResult(
                False,
                render_outcome(
                    "error", "This command requires an active thread. Send a message first."
                ),
                command_label,
                level="error",
            )
        if not definition.executable:
            # data carries the execution kind so generic bot passthroughs can
            # detect chat_stream commands structurally (no string matching)
            # and re-route them into the normal chat path.
            return CommandResult(
                False,
                render_outcome(
                    "error",
                    (
                        f"`/{definition.name}` is handled outside the command service. "
                        f"{definition.note or ''}"
                    ).strip(),
                ),
                command_label,
                level="error",
                data={"execution_kind": definition.execution_kind},
            )

        if definition.id == "help":
            help_kwargs: dict[str, Any] = dict(
                actor=actor,
                surface=surface,
                is_admin=ctx.is_admin,
            )
            args = [arg for arg in parsed.args if arg.strip()]
            if args and args[0].lower() == "all":
                markdown = self._help_markdown(
                    ctx.source,
                    user_id=ctx.user_id,
                    agent=getattr(api, "agent", None),
                    **help_kwargs,
                )
            elif args:
                card = self._command_help_markdown(tuple(args), **help_kwargs)
                if card is None:
                    target = args[0].lstrip("/")
                    hint = self._did_you_mean(
                        self._suggest_roots(_normalize_token(target))
                    )
                    return CommandResult(
                        False,
                        render_outcome(
                            "error", f"Unknown command `/{target}`.{hint} Use `/help`."
                        ),
                        command_label,
                        level="error",
                    )
                markdown = card
            else:
                markdown = self._help_index_markdown(
                    ctx.source,
                    user_id=ctx.user_id,
                    agent=getattr(api, "agent", None),
                    **help_kwargs,
                )
            return CommandResult(True, markdown, command_label, level="info")

        # COMMAND_SUBMIT lifecycle hooks (#134): the ONE fire point, after
        # parsing/aliases/gates (so a hook always sees the canonical command
        # and can never reopen a closed gate) and BEFORE the backend client
        # exists (so a deny leaks nothing), before binding (so a `rest`
        # rewrite feeds the strict binder). Notes render for human actors on
        # every post-seam exit via apply_hook_notes.
        from .command_hooks import apply_hook_notes, fire_command_submit

        hook_outcome = await fire_command_submit(ctx, definition, parsed)
        if hook_outcome.denied is not None:
            return hook_outcome.denied
        if hook_outcome.parsed is not None:
            parsed = hook_outcome.parsed
        hook_notes = hook_outcome.notes if actor != "agent" else []

        def _with_hook_notes(result: CommandResult) -> CommandResult:
            return apply_hook_notes(result, hook_notes)

        owns_api = api is None
        client = api
        if client is None:
            try:
                client = _client_for_context(ctx)
            except RuntimeError as exc:
                return _with_hook_notes(
                    CommandResult(
                        False, render_outcome("error", str(exc)), command_label, level="error"
                    )
                )

        executor = _CommandExecutor(
            api=client,
            thread_id=ctx.thread_id,
            user_id=ctx.user_id,
            actor=actor,
            is_admin=ctx.is_admin,
            service=self,
            surface=ctx.effective_surface,
        )

        method_name = "_cmd_" + "_".join(definition.path)
        method = getattr(executor, method_name, None)

        if method is None:
            return CommandResult(
                False,
                render_outcome(
                    "error",
                    f"Command `/{definition.name}` is registered but has no executor.",
                ),
                command_label,
                level="error",
            )

        try:
            if definition.params is not None:
                bound, bind_error = bind_args(
                    definition.params, parsed.args, parsed.rest
                )
                if bind_error is not None or bound is None:
                    if (
                        bind_error is not None
                        and bind_error.missing
                        and ctx.supports_forms
                        and actor != "agent"
                    ):
                        # Missing-required from a form-capable client: rescue
                        # into a generated picker (the /model bare-command UX)
                        # instead of the usage error. Only ABSENT arguments
                        # rescue; extras, typos, and invalid values stay
                        # errors for everyone, and a declaration the
                        # generator cannot express falls through to the
                        # normal error below.
                        rescue_form = await generate_param_form(
                            definition, executor
                        )
                        if rescue_form is not None:
                            return _with_hook_notes(CommandResult(
                                True,
                                (
                                    f"Select a value for `/{definition.name}`. "
                                    f"Usage: `{definition.usage}`."
                                ),
                                command_label,
                                level="info",
                                data=command_data(form=rescue_form),
                            ))
                    problem = (
                        bind_error.problem if bind_error else "Invalid arguments."
                    )
                    # A stray word on a family root is usually a mistyped
                    # subcommand: layer the Tier 1 guidance (did-you-mean,
                    # valid list) on top of the strict-extras error so the
                    # binder's strictness never reads as less helpful than
                    # the old hand parsers.
                    hint = ""
                    if (
                        bind_error is not None
                        and bind_error.unexpected
                        and len(definition.path) == 1
                    ):
                        subs = self._prefix_subcommands(definition.path[0])
                        if subs:
                            suggestions = difflib.get_close_matches(
                                _normalize_token(bind_error.unexpected),
                                subs,
                                n=2,
                                cutoff=0.6,
                            )
                            hint = self._did_you_mean(
                                suggestions, prefix=f"/{definition.path[0]} "
                            )
                            hint += (
                                " Valid subcommands: " + ", ".join(subs) + "."
                            )
                    return _with_hook_notes(CommandResult(
                        False,
                        render_outcome(
                            "error",
                            f"{problem}{hint} "
                            f"Usage: `{definition.usage}`. "
                            f"See `/help {definition.name}`.",
                        ),
                        command_label,
                        level="error",
                    ))
                raw_output = await method(bound)
            else:
                raw_output = await method(parsed.args, parsed.rest)
            data: dict[str, Any] | None = None
            level: CommandResultLevel = "info"
            if isinstance(raw_output, CommandOutput):
                data = raw_output.data
                level = raw_output.level
                raw_output = raw_output.text
            if data and "form" in data and not ctx.supports_forms:
                # Form payloads ship only to clients that declared they can
                # render them (the measured bare-/provider form is ~20KB, and
                # every other surface discards it unread). Safe by
                # construction: chain_form_output composes notes into
                # markdown, and OPTION LISTS are GUARANTEED here (#158): the
                # active tab's choices are appended to the body before the
                # payload is stripped, so a picker's choices reach every
                # formless caller without any handler inlining its own list
                # (the per-handler duty this replaces went unmet everywhere
                # but the cliproxy model step). Appending BEFORE the
                # per-surface _truncate keeps bot budgets authoritative.
                # State hints stay: the CLI applies them even where forms
                # are off, and they are small.
                body = str(raw_output or "")
                option_lines = form_options_markdown(data.get("form"), body)
                if option_lines:
                    joined = "\n".join(option_lines)
                    raw_output = f"{body.rstrip()}\n\n{joined}" if body.strip() else joined
                data = {k: v for k, v in data.items() if k != "form"} or None
            success, level, markdown = _render_result_markdown(raw_output, level)
            return _with_hook_notes(CommandResult(
                success,
                _truncate(markdown, limit=output_budget(ctx.effective_surface)),
                command_label,
                level=level,
                data=data if success else None,
            ))
        except httpx.HTTPStatusError as e:
            return _with_hook_notes(CommandResult(
                False,
                render_outcome("error", http_error_detail(e)),
                command_label,
                level="error",
            ))
        except Exception as e:
            logger.exception("command dispatch failed for /%s", definition.name)
            return _with_hook_notes(CommandResult(
                False, render_outcome("error", str(e)), command_label, level="error"
            ))
        finally:
            if owns_api and hasattr(client, "close"):
                await client.close()

    async def resolve_options(
        self,
        ctx: CommandContext,
        ref: str,
        *,
        api: Any | None = None,
    ) -> list[dict[str, Any]] | None:
        """Resolve a ``choices_ref`` option set for the calling identity.

        Returns ``None`` for an unknown ref (no resolver registered; the
        router renders that as 404), else the live option list, possibly
        empty. Client construction mirrors ``execute()`` so a resolver sees
        exactly the doors the corresponding handler would.
        """
        resolver = OPTION_RESOLVERS.get(ref)
        if resolver is None:
            return None
        owns_api = api is None
        client = api
        if client is None:
            client = _client_for_context(ctx)
        executor = _CommandExecutor(
            api=client,
            thread_id=ctx.thread_id,
            user_id=ctx.user_id,
            actor=ctx.effective_actor,
            is_admin=ctx.is_admin,
            service=self,
            surface=ctx.effective_surface,
        )
        try:
            return await resolver(executor)
        except Exception:  # noqa: BLE001 - options are an enhancement, not data.
            # Resolvers degrade to [] internally, but this endpoint must
            # never 500 a client mid-autocomplete if one slips a raise.
            logger.warning("options resolver %r failed", ref, exc_info=True)
            return []
        finally:
            if owns_api and hasattr(client, "close"):
                await client.close()


_COMMAND_SERVICE: CommandService | None = None


def get_command_service() -> CommandService:
    global _COMMAND_SERVICE
    if _COMMAND_SERVICE is None:
        _COMMAND_SERVICE = CommandService()
    return _COMMAND_SERVICE


# ─── Skill kit activation/deactivation from slash commands ─────────────────
#
# Skill kits are skills with `required_tools` in their frontmatter; activating
# one binds those tools into `ThreadConfig.temporary_tools` with the kit's
# `tool_ttl`. The standard path is the agent invoking the `Skill` meta-tool,
# but slash commands (e.g. `/orchestrate`, `/goal`) also need to activate kits.
# These helpers are shared by every slash command that wants kit semantics —
# they mirror what `skills/meta_tool.py` does for the agent-driven path.


def activate_skill_kit(
    *,
    agent: Any,
    thread_id: str,
    user_id: str,
    skill_name: str,
    reason: str = "slash command activation",
    ttl_override: str | None = None,
) -> tuple[bool, str]:
    """Activate a skill or Skill Kit on a thread from a slash command.

    Adds the skill to ``ThreadConfig.enabled_skills``. If the skill declares
    ``required_tools``, those tools are also bound into
    ``ThreadConfig.temporary_tools`` with the kit's ``tool_ttl`` or a one-shot
    override. ``bind_tools_for_thread`` queues the graph rebuild on success,
    so the next agent turn picks up the new tools automatically.

    Returns ``(ok, message)``: ``ok`` IS the outcome, so ``message`` is a
    plain body with no legacy ``[Error]:``/``[Success]:`` prefix (#132).
    Callers outside the dispatcher (chat.py, spawn_thread, the workflow
    thread verbs) quote it into their own copy, where a prefix used to
    surface mid-string.
    """
    if not thread_id:
        return False, "No active thread; cannot activate skill."

    skill_manager = getattr(agent, "skill_manager", None)
    if skill_manager is None:
        return False, "Skill manager unavailable; cannot activate skill."

    try:
        skill = skill_manager.get(skill_name, user_id=user_id)
    except Exception as e:
        return False, f"Skill manager lookup failed: {e}"

    if skill is None:
        return False, f"Skill '{skill_name}' not found."

    # Nested required skills resolve one level deep, strictly, BEFORE any
    # mutation: a kit that names a missing skill must not half-activate.
    from ..skills import expanded_required_tools, resolve_nested_skills

    nested_skills, missing_nested = resolve_nested_skills(
        skill, skill_manager, user_id
    )
    if missing_nested:
        return False, (
            f"Skill '{skill_name}' requires skills that are not "
            f"installed: {', '.join(missing_nested)}. Nothing was activated."
        )
    union_tools = expanded_required_tools(skill, nested_skills)

    def _enable_on_thread() -> str | None:
        try:
            from ..tools.skill_config import _activate_skill_on_thread

            _activate_skill_on_thread(agent, thread_id, skill_name)
            return None
        except Exception as e:  # noqa: BLE001 - surfaced to the caller
            return f"Failed to add skill to thread: {e}"

    if not skill.is_skill_kit:
        error = _enable_on_thread()
        if error:
            return False, error
        return True, f"Skill '{skill_name}' activated."

    nested_note = (
        (
            " Required skills pulled in (one level): "
            + ", ".join(s.name for s in nested_skills)
            + "."
        )
        if nested_skills
        else ""
    )

    if not union_tools:
        error = _enable_on_thread()
        if error:
            return False, error
        return True, (
            f"Skill kit '{skill_name}' activated (no tools to "
            f"bind).{nested_note}"
        )

    try:
        from ..tools.tool_search import bind_tools_for_thread
    except Exception as e:
        return False, f"tool_search unavailable: {e}"

    # Bind FIRST, enable after (the Skill() meta-tool order): a strict
    # binding failure must leave the kit fully inactive. Enabling first
    # would leave a half-activated kit whose thread-template tools still
    # surface at the next graph build despite the reported failure.
    binding = bind_tools_for_thread(
        union_tools,
        "",  # category not used; we pass explicit tool names
        thread_id,
        user_id,
        ttl=ttl_override or skill.tool_ttl,
        strict=True,
        source="slash_command",
        skill_name=skill_name,
        reason=reason,
    )
    if not binding.ok:
        return False, (
            f"Skill kit '{skill_name}' was NOT activated; tool "
            f"binding failed:\n{binding.text}"
        )

    error = _enable_on_thread()
    if error:
        # The bind succeeded but the enable write failed: roll the bound
        # tools back (best-effort) so no partial activation survives.
        try:
            tc = agent.thread_config_manager.get_config(thread_id)
            if tc is not None and tc.temporary_tools:
                for tool_name in union_tools:
                    tc.temporary_tools.pop(tool_name, None)
                agent.thread_config_manager.save_config(tc)
                agent.invalidate_thread_config_cache(thread_id)
        except Exception:  # noqa: BLE001 - rollback is best-effort
            logger.warning(
                "skill kit activation rollback failed for %s on %s",
                skill_name, thread_id, exc_info=True,
            )
        return False, error

    return True, (
        f"Skill kit '{skill_name}' activated.{nested_note}\n"
        f"{binding.text}"
    )


def deactivate_skill_kit(
    *,
    agent: Any,
    thread_id: str,
    user_id: str | None = None,
    skill_name: str,
) -> tuple[CommandResultLevel, str]:
    """Deactivate a skill or Skill Kit on a thread.

    Removes the skill from ``ThreadConfig.enabled_skills``. For Skill Kits,
    evicts required tools from ``ThreadConfig.temporary_tools``. The graph
    rebuilds on the next turn naturally as the tool set has changed.

    Returns ``(level, message)``: ``message`` is a plain body with no
    sentinel or artifact (#132). The level is authored HERE because a bool
    cannot express the three outcomes (#144 review): ``success`` for a real
    deactivation, ``info`` for the no-op ("was not active", a readout that
    must not claim ``**Done.**``), ``error`` for failures.
    """
    if not thread_id:
        return "error", "No active thread; cannot deactivate skill."

    tc = agent.thread_config_manager.get_config(thread_id)
    if tc is None:
        return "error", f"No thread config for {thread_id}."

    changed = False
    if skill_name in tc.enabled_skills:
        tc.enabled_skills = [n for n in tc.enabled_skills if n != skill_name]
        changed = True

    evicted: list[str] = []
    skill = None
    skill_manager = getattr(agent, "skill_manager", None)
    if skill_manager is not None:
        try:
            skill = skill_manager.get(skill_name, user_id=user_id)
        except Exception:
            skill = None

    if skill is not None and skill.is_skill_kit:
        # Evict the same expanded union activation bound (outer + nested
        # kits' tools, one level). Overlaps with another active kit's tools
        # are evicted too, matching the existing overlapping-kit behavior.
        from ..skills import expanded_required_tools, resolve_nested_skills

        nested_skills, _missing = resolve_nested_skills(
            skill, skill_manager, user_id
        )
        for tool_name in expanded_required_tools(skill, nested_skills):
            if tool_name in tc.temporary_tools:
                del tc.temporary_tools[tool_name]
                evicted.append(tool_name)
                changed = True

    if changed:
        if not agent.thread_config_manager.save_config(tc):
            return "error", "Failed to save thread config after deactivate."
        if hasattr(agent, "invalidate_thread_config_cache"):
            try:
                agent.invalidate_thread_config_cache(thread_id)
            except Exception:
                logger.debug(
                    "invalidate_thread_config_cache failed after deactivate",
                    exc_info=True,
                )

    label = "Skill kit" if skill is not None and skill.is_skill_kit else "Skill"
    if not changed and not evicted:
        return "info", f"{label} '{skill_name}' was not active."
    msg_parts = [f"{label} '{skill_name}' deactivated."]
    if evicted:
        msg_parts.append(f"Evicted tools: {', '.join(evicted)}.")
    return "success", " ".join(msg_parts)


def build_skill_slash_prompt(
    *,
    skill: Any,
    user_prompt: str,
    has_attachments: bool = False,
) -> str:
    """Build the model-facing prompt for `/skill` and `/kit` chat routing."""
    prompt = user_prompt.strip()
    if not prompt and has_attachments:
        prompt = "Use the attached files/images as the user request."
    elif not prompt:
        prompt = "Use this skill for the current turn."

    attachment_note = (
        "\n\nThe user attached files/images to this request. Treat them as part "
        "of the prompt and inspect them as needed."
        if has_attachments
        else ""
    )
    body = str(getattr(skill, "body", "") or "").strip() or "(No skill body.)"
    return (
        f"[Skill slash command: {skill.name}]\n\n"
        "Apply this SKILL.md body for the current turn:\n\n"
        f"{body}\n\n"
        "---\n\n"
        "User request:\n"
        f"{prompt}{attachment_note}"
    )


def prepare_skill_slash_command(
    *,
    agent: Any,
    thread_id: str,
    user_id: str,
    mode: Literal["skill", "kit"],
    rest: str,
    has_attachments: bool = False,
) -> SkillSlashResult:
    """Prepare `/skill` or `/kit` chat-stream handling.

    Returns ``should_stream=True`` when the caller should pass ``message`` into
    the normal agent stream. Returns ``should_stream=False`` for usage errors
    and deactivation responses that should be emitted directly.
    """
    args = _split_args(rest)
    if not args:
        usage = (
            "/skill <name> [prompt]"
            if mode == "skill"
            else "/kit <name> [ttl] [prompt]"
        )
        return SkillSlashResult(
            False,
            False,
            f"Usage: `{usage}`.",
        )

    skill_name = args[0].strip().lower()
    tail = _split_rest_after_tokens(rest, 1)
    tail_args = _split_args(tail)

    skill_manager = getattr(agent, "skill_manager", None)
    if skill_manager is None:
        return SkillSlashResult(
            False,
            False,
            "Skill manager unavailable.",
            skill_name,
        )
    try:
        skill = skill_manager.get(skill_name, user_id=user_id)
    except Exception as e:  # noqa: BLE001
        return SkillSlashResult(
            False,
            False,
            f"Skill manager lookup failed: {e}",
            skill_name,
        )
    if skill is None or getattr(skill, "is_internal", False):
        noun = "Skill Kit" if mode == "kit" else "Skill"
        return SkillSlashResult(
            False,
            False,
            f"{noun} '{skill_name}' not found.",
            skill_name,
        )

    is_kit = bool(getattr(skill, "is_skill_kit", False))
    if mode == "skill" and is_kit:
        return SkillSlashResult(
            False,
            False,
            f"'{skill_name}' is a Skill Kit. Use `/kit {skill_name}`.",
            skill_name,
        )
    if mode == "kit" and not is_kit:
        return SkillSlashResult(
            False,
            False,
            f"'{skill_name}' is a markdown-only skill. Use `/skill {skill_name}`.",
            skill_name,
        )

    if len(tail_args) == 1 and tail_args[0].lower() == "off":
        level, msg = deactivate_skill_kit(
            agent=agent,
            thread_id=thread_id,
            user_id=user_id,
            skill_name=skill_name,
        )
        return SkillSlashResult(
            level != "error", False, msg, skill_name, level=level
        )

    ttl_override: str | None = None
    prompt = tail
    if mode == "kit" and tail_args:
        try:
            ttl_key, _ = parse_tool_ttl(tail_args[0])
            ttl_override = ttl_key
            prompt = _split_rest_after_tokens(tail, 1)
        except ValueError:
            ttl_override = None
            prompt = tail

    ok, msg = activate_skill_kit(
        agent=agent,
        thread_id=thread_id,
        user_id=user_id,
        skill_name=skill_name,
        reason=f"/{mode} {skill_name}",
        ttl_override=ttl_override,
    )
    if not ok:
        return SkillSlashResult(False, False, msg, skill_name)

    return SkillSlashResult(
        True,
        True,
        build_skill_slash_prompt(
            skill=skill,
            user_prompt=prompt,
            has_attachments=has_attachments,
        ),
        skill_name,
        level="info",  # streams; the level is never rendered on this arm
    )


class _CommandExecutor(
    ContextCommandsMixin,
    ThreadCommandsMixin,
    LLMCommandsMixin,
    ProviderSetupCommandsMixin,
    CliproxyCommandsMixin,
    AliasCommandsMixin,
):
    """Per-request command executor with the migrated command bodies."""

    def __init__(
        self,
        api: Any,
        thread_id: str | None,
        user_id: str,
        actor: str = "user",
        is_admin: bool | None = None,
        service: "CommandService | None" = None,
        surface: str | None = None,
    ):
        self.api = api
        self.thread_id = thread_id or ""
        self.user_id = user_id
        self.actor = actor
        # Handlers never need the caller's supports_forms flag: option lists
        # for formless callers are the dispatcher's duty (#158,
        # form_options_markdown at the form-strip site), so an executor-level
        # copy of the flag would only invite a second, driftable rendering
        # path.
        # The context's admin verdict (None = unknown). Handlers use it only
        # for cosmetic gating (e.g. not attaching a form whose submit targets
        # are admin-only); authorization stays at the dispatch gate. The one
        # handler-level use that LOOKS like enforcement (/provider switch's
        # global scope) is still cosmetic: the settings write it guards is
        # independently admin-gated in the backend client.
        self.is_admin = is_admin
        # The context's effective surface, for _command_offerable's
        # blocked_surfaces axis (None = unknown, treated as unblocked).
        self.surface = surface
        # The service that dispatched this request (None when a test builds
        # the executor directly; _usage_error then falls back to the process
        # default registry).
        self._service = service

    def _require_thread(self) -> CommandOutput | None:
        if self.thread_id:
            return None
        return command_error(
            "This command requires an active thread. Send a message first."
        )

    def _resolve_thread_token(
        self, raw: Any, *, default_current: bool = False
    ) -> tuple[Optional[str], Optional[CommandOutput]]:
        """One grammar for `--thread` tokens across the catalog (#143
        consolidation of four per-handler dialects): `current` or `.` is
        the active thread and ERRORS when none is active (the caller asked
        to narrow or target; silently widening to no-filter, or rebinding
        to the default thread, would lie), `none`/`default` maps to None
        (no filter, or the caller's own spelling of the default thread),
        and anything else passes through as an id. With ``default_current``
        an ABSENT token means the active thread when one exists and stays
        None otherwise: the add-command default, deliberately unguarded so
        a threadless surface can still create TODOs.
        """
        token = str(raw or "").strip()
        if not token:
            return ((self.thread_id or None) if default_current else None), None
        folded = token.casefold()
        if folded in {"current", "."}:
            thread_error = self._require_thread()
            if thread_error is not None:
                return None, thread_error
            return self.thread_id, None
        if folded in {"none", "default"}:
            return None, None
        return token, None

    def _usage_error(self, name: str, *, hint: str | None = None) -> CommandOutput:
        """Render the standard usage error for a registered command.

        Pulls the usage string and derived subcommand list from the command
        registry (the dispatching service when known), so handler error copy
        cannot drift from the catalog: the old pattern was a hand-written
        ``Usage:`` literal per handler, and several had drifted from the
        registered usage. ``hint`` appends one command-specific sentence
        after the usage line.
        """
        service = self._service or get_command_service()
        info = service.find_command(name)
        if info is None:
            # Defensive: never raise while rendering an error message.
            return command_error(f"Usage: `/{name}`.")
        parts = [f"Usage: `{info.usage}`."]
        if info.subcommands:
            parts.append("Subcommands: " + ", ".join(info.subcommands) + ".")
        if hint:
            parts.append(hint)
        parts.append(f"See `/help {info.name}`.")
        return command_error(" ".join(parts))

    def _command_offerable(self, name: str) -> bool:
        """Whether the dispatch gate would let THIS caller run ``name``.

        The cosmetic-visibility twin of ``execute()``'s enforcement,
        derived from the registry so it cannot drift from the flags (a
        hand-written condition is a second copy of the flags and drifts the
        moment a registration changes). Covers all three gate axes: admin (blocking
        only on a definite False, mirroring the gate), agent_allowed, and
        blocked_surfaces. Used to decide which actions a form tab or an
        "Act:" line OFFERS; authorization stays in ``execute()``.
        """
        service = self._service or get_command_service()
        info = service.find_command(name)
        if info is None:
            return False
        if info.requires_admin and self.is_admin is False:
            return False
        if self.actor == "agent" and not info.agent_allowed:
            return False
        if self.surface and self.surface in info.blocked_surfaces:
            return False
        return True

    def _agent(self) -> Any | None:
        agent = getattr(self.api, "agent", None)
        if agent is not None:
            return agent
        return get_command_service()._resolve_agent()

    # ── Help ──────────────────────────────────────────────────────────────

    async def _cmd_help(self, args: list[str], rest: str) -> str:
        return get_command_service()._help_markdown(
            "user",
            user_id=self.user_id,
            agent=self._agent(),
        )

    # ── Skills ────────────────────────────────────────────────────────────

    async def _cmd_skills(self, bound: BoundArgs) -> str | CommandOutput:
        # Bare "/skills" lists. Every skills verb is a registered child, so
        # longest-prefix dispatch routes them before this handler runs; the root
        # takes zero arguments and a typo never reaches here (the dispatcher
        # answers it with did-you-mean plus the valid-subcommand list).
        return await self._cmd_skills_list(BoundArgs())

    async def _cmd_skills_list(self, bound: BoundArgs) -> str | CommandOutput:
        thread_error = self._require_thread()
        if thread_error:
            return thread_error

        agent = self._agent()
        if agent is None:
            return command_error("No current NymeriaAgent is available for skill commands.")

        service = get_command_service()
        skills = service._visible_slash_skills(self.user_id, agent=agent)
        if not skills:
            return "No user-activatable skills are installed."

        tc = agent.thread_config_manager.get_config(self.thread_id)
        active = set(tc.enabled_skills or []) if tc is not None else set()

        lines = ["Skills on this thread:", ""]
        for skill in skills:
            status = "active" if skill.name in active else "inactive"
            kind = "kit" if skill.is_skill_kit else "skill"
            ttl = f"; ttl: `{skill.tool_ttl}`" if skill.is_skill_kit else ""
            lines.append(f"- `{skill.name}` - {kind}, {status}{ttl}; {skill.description}")
        return "\n".join(lines)

    async def _cmd_skills_show(self, bound: BoundArgs) -> str | CommandOutput:
        """One show-one verb for a skill: its metadata, then its body.

        The old ``/skills inspect`` (metadata only) folded in here as an alias
        (backlog #131), so this renders the union: neither caller lost a
        field. The body is affordable alongside the table because 12,000
        chars (``OUTPUT_BUDGET_COMPACT``) is now the FLOOR on every
        surface; this command used to carry that number as its own
        one-command exemption from a 4,000-char default.
        """
        agent = self._agent()
        service = get_command_service()
        skill_manager = service._skill_manager(agent)
        if skill_manager is None:
            return command_error("Skill manager unavailable.")

        skill_name = str(bound.get("name") or "").strip().lower()
        try:
            skill = skill_manager.get(skill_name, user_id=self.user_id)
        except Exception as e:  # noqa: BLE001
            return command_error(f"Skill manager lookup failed: {e}")
        if skill is None:
            return command_error(f"Skill '{skill_name}' not found.")

        scripts = skill.list_scripts() if hasattr(skill, "list_scripts") else []
        references = skill.list_references() if hasattr(skill, "list_references") else []
        rows = [
            ("Name", skill.name),
            ("Scope", skill.scope),
            ("Kit", "yes" if getattr(skill, "is_skill_kit", False) else "no"),
            ("Required tools", ", ".join(skill.required_tools) or "-"),
            ("Allowed tools", ", ".join(skill.allowed_tools) or "-"),
            ("Scripts", ", ".join(scripts) or "-"),
            ("References", ", ".join(references) or "-"),
            ("Path", str(skill.path)),
        ]
        width = max(len(label) for label, _ in rows)
        lines = ["Skill", ""]
        for label, value in rows:
            lines.append(f"  {label:<{width}}  {value}")
        description = (skill.description or "").strip()
        if description:
            lines.append("")
            lines.append(f"Description: {description}")
        body = (getattr(skill, "body", "") or "").strip()
        lines.append("")
        lines.append(body or "(No body.)")
        return "\n".join(lines)

    async def _cmd_skills_search(self, bound: BoundArgs) -> str | CommandOutput:
        source = str(bound.get("source") or "anthropic")
        # The declared query is a repeatable positional: the bare words the
        # options left behind, joined the way the hand parser joined them.
        query = " ".join(bound.get("query") or []).strip() or None

        from ..skills.marketplace import MarketplaceError, get_fetcher

        try:
            fetcher = get_fetcher(source)
        except (NotImplementedError, MarketplaceError) as exc:
            return command_error(str(exc))

        try:
            entries = fetcher.list(query)
        except MarketplaceError as exc:
            return command_error(f"Marketplace search failed: {exc}")
        except Exception as exc:  # noqa: BLE001 - surface fetcher errors
            return command_error(f"Marketplace search failed: {exc}")

        if not entries:
            return f"No marketplace skills matched on source '{source}'."

        lines = [f"Marketplace Skills ({source}): {len(entries)} found", ""]
        for entry in entries[:50]:
            description = (entry.description or "").strip().splitlines()
            short = description[0] if description else ""
            lines.append(f"- `{entry.name}` — {short}")
        if len(entries) > 50:
            lines.append(f"... and {len(entries) - 50} more")
        return "\n".join(lines)

    async def _cmd_skills_install(self, bound: BoundArgs) -> str | CommandOutput:
        scope = str(bound.get("scope") or "user")
        source = str(bound.get("source") or "anthropic")
        name = str(bound.get("name") or "")

        agent = self._agent()
        if agent is None:
            return command_error("No current NymeriaAgent is available for skill commands.")
        skill_manager = getattr(agent, "skill_manager", None)
        if skill_manager is None:
            return command_error("Skill manager unavailable.")

        from ..skills.marketplace import MarketplaceError, get_fetcher

        try:
            fetcher = get_fetcher(source)
        except (NotImplementedError, MarketplaceError) as exc:
            return command_error(str(exc))

        target_dir = skill_manager.target_dir(
            scope,
            user_id=self.user_id if scope == "user" else None,
        )
        target_dir.mkdir(parents=True, exist_ok=True)
        try:
            skill = fetcher.fetch(name, target_dir)
        except MarketplaceError as exc:
            return command_error(f"Install failed: {exc}")
        except Exception as exc:  # noqa: BLE001
            return command_error(f"Install failed: {exc}")

        skill_manager.reload()
        return command_success(f"Installed skill '{skill.name}' (scope: {scope}).")

    async def _cmd_skills_enable(self, bound: BoundArgs) -> str | CommandOutput:
        return await self._set_skill_state(bound, enabled=True)

    async def _cmd_skills_disable(self, bound: BoundArgs) -> str | CommandOutput:
        if str(bound.get("name") or "").strip().lower() == "all":
            # `all` is a thread-only sweep: the global overlay is a named list
            # a user curates, so emptying it from here would be a surprise. The
            # refusal survives the scope-token migration (backlog #131 wave B),
            # only its spelling moved.
            if bound.get("scope") == "global":
                return command_error(
                    "`/skills disable all` deactivates the skills active on "
                    "this thread; drop the `global` scope."
                )
            return await self._deactivate_every_active_skill()
        return await self._set_skill_state(bound, enabled=False)

    async def _deactivate_every_active_skill(self) -> str | CommandOutput:
        """The ``all`` value of ``/skills disable`` (was ``/skills off all``).

        Folded off the retired depth-3 path in backlog #131; ``/skills off``
        is a whole-path alias of ``skills disable``, so the old spelling still
        arrives here with ``name="all"``.
        """
        thread_error = self._require_thread()
        if thread_error:
            return thread_error

        agent = self._agent()
        if agent is None:
            return command_error("No current NymeriaAgent is available for skill commands.")

        service = get_command_service()
        skills = service._visible_slash_skills(self.user_id, agent=agent)
        tc = agent.thread_config_manager.get_config(self.thread_id)
        active = set(tc.enabled_skills or []) if tc is not None else set()
        active_skills = [skill for skill in skills if skill.name in active]
        if not active_skills:
            return "No visible skills are active on this thread."

        lines: list[str] = []
        had_error = False
        for skill in active_skills:
            level, msg = deactivate_skill_kit(
                agent=agent,
                thread_id=self.thread_id,
                user_id=self.user_id,
                skill_name=skill.name,
            )
            had_error = had_error or level == "error"
            lines.append(f"- `{skill.name}`: {msg}")

        text = "Deactivated skills:\n" + "\n".join(lines)
        return command_error(text) if had_error else command_success(text)

    async def _set_skill_state(self, bound: BoundArgs, *, enabled: bool) -> str | CommandOutput:
        # Thread is the default scope; the trailing `global` token writes the
        # profile overlay instead. Without an active thread the thread arm
        # raises rather than silently writing globally: this list governs what
        # a conversation can reach, so a wrong-scope write is not recoverable
        # by re-running the command.
        global_scope = bound.get("scope") == "global"
        name = str(bound.get("name") or "")

        agent = self._agent()
        if agent is None:
            return command_error("No current NymeriaAgent is available for skill commands.")

        if global_scope:
            profile_manager = getattr(agent, "profile_manager", None)
            if profile_manager is None:
                return command_error("Profile manager unavailable.")
            profile = profile_manager.get_profile(self.user_id)
            current = list(getattr(profile, "enabled_global_skills", []) or [])
            if enabled:
                if name not in current:
                    current.append(name)
            else:
                current = [item for item in current if item != name]
            profile.enabled_global_skills = sorted(set(current))
            profile_manager.save_profile(profile)
            action = "Enabled globally" if enabled else "Disabled globally"
            return command_success(f"{action}: {name}")

        thread_error = self._require_thread()
        if thread_error:
            return thread_error

        tc = await self.api.get_thread_config(self.thread_id)
        enabled_skills = list((tc or {}).get("enabled_skills", []) or [])
        disabled_skills = list((tc or {}).get("disabled_skills", []) or [])
        if enabled:
            if name not in enabled_skills:
                enabled_skills.append(name)
            disabled_skills = [item for item in disabled_skills if item != name]
        else:
            if name not in disabled_skills:
                disabled_skills.append(name)
            enabled_skills = [item for item in enabled_skills if item != name]

        await self.api.update_thread_config(
            self.thread_id,
            user_id=self.user_id,
            enabled_skills=enabled_skills,
            disabled_skills=disabled_skills,
        )
        action = "Enabled" if enabled else "Disabled"
        return command_success(f"{action}: {name}")

    # ── MCP servers ───────────────────────────────────────────────────────

    async def _cmd_mcp(self, bound: BoundArgs) -> str:
        # Bare "/mcp" lists; every verb is a registered child routed before this
        # handler. The root takes zero arguments, so a typo is answered by the
        # dispatcher with did-you-mean plus the valid-subcommand list.
        return await self._cmd_mcp_list(BoundArgs())

    async def _cmd_mcp_list(self, bound: BoundArgs) -> str:
        from ..core.mcp_servers import get_mcp_server_registry

        registry = get_mcp_server_registry()
        servers = registry.get_all_servers()
        if not servers:
            return "No MCP servers configured."

        lines = [
            f"MCP Servers: {len(servers)} configured",
            "",
            "| ID | State | Tools | Name |",
            "|---|---|---|---|",
        ]
        for server in sorted(servers, key=lambda s: s.id):
            state = server.install_status or ("enabled" if server.enabled else "disabled")
            tool_count = len(server.discovered_tools or [])
            name = table_cell(server.name or server.id)
            lines.append(f"| `{server.id}` | {state} | {tool_count} | {name} |")
        return "\n".join(lines)

    async def _cmd_mcp_status(self, bound: BoundArgs) -> str | CommandOutput:
        from ..core.mcp_servers import get_mcp_server_registry

        registry = get_mcp_server_registry()
        server_id = str(bound.get("server_id") or "")
        if server_id:
            server = registry.get_server(server_id)
            if server is None:
                return command_error(f"MCP server '{server_id}' not found.")
            rows = [
                ("ID", server.id),
                ("Name", server.name or ""),
                ("Enabled", "yes" if server.enabled else "no"),
                ("Install status", server.install_status or ""),
                ("Tools", len(server.discovered_tools or [])),
                ("Last error", (server.last_error or "")[:200]),
            ]
            width = max(len(label) for label, _ in rows)
            lines = [f"MCP Server `{server.id}`", ""]
            for label, value in rows:
                lines.append(f"  {label:<{width}}  {value}")
            recent_logs = list(server.install_logs or [])[-5:]
            if recent_logs:
                lines.append("")
                lines.append("Recent logs:")
                lines.extend(f"  {line}" for line in recent_logs)
            tool_names = [
                str(getattr(tool, "name", "") or "")
                for tool in server.discovered_tools or []
                if str(getattr(tool, "name", "") or "")
            ]
            if tool_names:
                lines.append("")
                lines.append("Discovered tools:")
                lines.extend(f"  - {name}" for name in tool_names)
            return "\n".join(lines)

        servers = registry.get_all_servers()
        if not servers:
            return "No MCP servers configured."

        lines = ["MCP Servers", ""]
        for server in sorted(servers, key=lambda s: s.id):
            state = server.install_status or ("enabled" if server.enabled else "disabled")
            err = f" — error: {server.last_error}" if server.last_error else ""
            lines.append(f"- `{server.id}`: {state}{err}")
        return "\n".join(lines)

    async def _cmd_mcp_logs(self, bound: BoundArgs) -> str | CommandOutput:
        from ..core.mcp_servers import get_mcp_server_registry

        server_id = str(bound.get("server_id") or "")
        limit = max(1, int(bound.get("limit", 20)))

        registry = get_mcp_server_registry()
        server = registry.get_server(server_id)
        if server is None:
            return command_error(f"MCP server '{server_id}' not found.")

        logs = list(server.install_logs or [])
        if not logs:
            return f"No install logs for `{server_id}`."
        selected = logs[-limit:]
        lines = [f"Install logs for `{server_id}` (last {len(selected)} of {len(logs)})", ""]
        lines.extend(f"- {line}" for line in selected)
        return "\n".join(lines)

    async def _cmd_mcp_discover(self, bound: BoundArgs) -> str | CommandOutput:
        from ..core.mcp_servers import get_mcp_server_registry

        server_id = str(bound.get("server_id") or "")
        registry = get_mcp_server_registry()
        server = registry.get_server(server_id)
        if server is None:
            return command_error(f"MCP server '{server_id}' not found.")

        agent = self._agent()
        try:
            discovered = registry.discover_tools(server_id)
        except Exception as exc:  # noqa: BLE001 - discovery errors surface as markdown
            server.install_status = "failed"
            server.enabled = False
            server.last_error = str(exc)
            registry.save_server(server)
            if agent is not None and hasattr(agent, "reload_mcp_server_tools"):
                agent.reload_mcp_server_tools()
            return command_error(f"Tool discovery failed for `{server_id}`: {exc}")

        if agent is not None and hasattr(agent, "reload_mcp_server_tools"):
            agent.reload_mcp_server_tools()
        count = len(discovered)
        names = [
            str(getattr(tool, "name", "") or "")
            for tool in discovered
            if str(getattr(tool, "name", "") or "")
        ]
        suffix = f": {', '.join(names)}" if names else "."
        return command_success(
            f"Discovered {count} tool{'s' if count != 1 else ''} "
            f"for `{server_id}`{suffix}"
        )

    async def _cmd_mcp_test(self, bound: BoundArgs) -> str | CommandOutput:
        from ..core.mcp_servers import get_mcp_server_registry

        server_id = str(bound.get("server_id") or "")
        registry = get_mcp_server_registry()
        server = registry.get_server(server_id)
        if server is None:
            return command_error(f"MCP server '{server_id}' not found.")

        try:
            result = registry.test_connection(server_id)
        except Exception as exc:  # noqa: BLE001
            return command_error(f"MCP test failed for `{server_id}`: {exc}")

        result_dict = result if isinstance(result, dict) else {}
        status = str(result_dict.get("status") or "ok")
        tools_count = result_dict.get("tools_count") or result_dict.get("toolsCount") or ""
        error = str(result_dict.get("error") or "")
        if status.casefold() in {"ok", "success", "connected"} and not error:
            suffix = f" ({tools_count} tools)" if str(tools_count) else ""
            return command_success(f"MCP test passed: `{server_id}`{suffix}")
        return command_error(f"MCP test failed for `{server_id}`: {error or result}")

    async def _cmd_mcp_delete(self, bound: BoundArgs) -> str | CommandOutput:
        from ..core.mcp_servers import get_mcp_server_registry

        server_id = str(bound.get("server_id") or "")
        registry = get_mcp_server_registry()
        if not registry.delete_server(server_id):
            return command_error(f"MCP server '{server_id}' not found.")

        agent = self._agent()
        if agent is not None and hasattr(agent, "reload_mcp_server_tools"):
            agent.reload_mcp_server_tools()
        return command_success(f"Removed MCP server `{server_id}`.")

    async def _cmd_mcp_retry(self, bound: BoundArgs) -> str | CommandOutput:
        from ..core.mcp_runtime import MCPInstallPlan
        from ..core.mcp_servers import get_mcp_server_registry

        server_id = str(bound.get("server_id") or "")
        registry = get_mcp_server_registry()
        server = registry.get_server(server_id)
        if server is None:
            return command_error(f"MCP server '{server_id}' not found.")
        if not server.install_plan:
            return command_error(f"MCP server `{server_id}` has no install plan to retry.")

        # The full retry flow uses _run_mcp_install on the API router with
        # admin-confirmation, credential bindings, and thread auto-enable.
        # Surface a guidance message rather than re-implementing it half-way
        # here; the desktop UI exposes the rich retry flow.
        plan = MCPInstallPlan.from_dict(server.install_plan)
        try:
            discovered = registry.discover_tools(server_id)
        except Exception as exc:  # noqa: BLE001
            return command_error(f"Retry failed for `{server_id}`: {exc}")
        server.install_status = "ready" if discovered else "draft"
        server.last_error = ""
        registry.save_server(server)
        agent = self._agent()
        if agent is not None and hasattr(agent, "reload_mcp_server_tools"):
            agent.reload_mcp_server_tools()
        names = [
            str(getattr(tool, "name", "") or "")
            for tool in discovered
            if str(getattr(tool, "name", "") or "")
        ]
        suffix = f": {', '.join(names)}" if names else "."
        return command_success(
            f"Retried `{server_id}` "
            f"(runtime: {plan.runtime_type}); "
            f"discovered {len(discovered)} tool(s){suffix}"
        )

    # ── Event triggers ────────────────────────────────────────────────────

    def _trigger_manager(self) -> Any:
        from ..tools.triggers import _get_trigger_manager

        return _get_trigger_manager()

    async def _cmd_triggers(self, bound: BoundArgs) -> str | CommandOutput:
        # Bare "/triggers" lists; every verb is a registered child routed first.
        # The root takes zero arguments, so a typo is answered by the dispatcher
        # with did-you-mean plus the valid-subcommand list.
        return await self._cmd_triggers_list(BoundArgs())

    async def _cmd_triggers_list(self, bound: BoundArgs) -> str | CommandOutput:
        enabled_only = bool(bound.get("enabled_only"))
        thread_id, thread_error = self._resolve_thread_token(bound.get("thread"))
        if thread_error is not None:
            return thread_error

        manager = self._trigger_manager()
        triggers = manager.get_triggers(self.user_id) or []

        if enabled_only:
            triggers = [t for t in triggers if t.enabled]
        if thread_id:
            triggers = [t for t in triggers if t.thread_id == thread_id]
        if not triggers:
            return "No triggers found."

        lines = [
            f"Triggers: {len(triggers)} total",
            "",
            "| ID | Status | Source | Action | Name |",
            "|---|---|---|---|---|",
        ]
        for t in sorted(triggers, key=lambda item: item.id):
            status = "enabled" if t.enabled else "disabled"
            action_type = getattr(t.action, "type", "?") if t.action else "?"
            lines.append(
                f"| `{t.id}` | {status} | {t.source_type} | {action_type} "
                f"| {table_cell(t.name)} |"
            )
        return "\n".join(lines)

    async def _cmd_triggers_enable(self, bound: BoundArgs) -> str | CommandOutput:
        return await self._set_trigger_enabled(bound, enabled=True)

    async def _cmd_triggers_disable(self, bound: BoundArgs) -> str | CommandOutput:
        return await self._set_trigger_enabled(bound, enabled=False)

    async def _set_trigger_enabled(self, bound: BoundArgs, *, enabled: bool) -> str | CommandOutput:
        trigger_id = str(bound.get("trigger_id") or "")
        manager = self._trigger_manager()
        if not manager.update_trigger(self.user_id, trigger_id, enabled=enabled):
            return command_error(f"Trigger '{trigger_id}' not found.")
        action = "Enabled" if enabled else "Disabled"
        return command_success(f"{action} trigger `{trigger_id}`.")

    async def _cmd_triggers_delete(self, bound: BoundArgs) -> str | CommandOutput:
        trigger_id = str(bound.get("trigger_id") or "")
        manager = self._trigger_manager()
        if not manager.delete_trigger(self.user_id, trigger_id):
            return command_error(f"Trigger '{trigger_id}' not found.")
        manager.delete_executions_for_triggers(self.user_id, [trigger_id])
        return command_success(f"Deleted trigger `{trigger_id}`.")

    async def _cmd_triggers_history(self, bound: BoundArgs) -> str:
        limit = max(1, int(bound.get("limit", 20)))
        trigger_id = str(bound.get("trigger_id") or "") or None

        manager = self._trigger_manager()
        executions = manager.get_executions(
            self.user_id,
            trigger_id=trigger_id,
            limit=limit,
        )
        if not executions:
            scope = f" for `{trigger_id}`" if trigger_id else ""
            return f"No trigger executions found{scope}."

        lines = [
            f"Trigger executions: {len(executions)}"
            + (f" for `{trigger_id}`" if trigger_id else ""),
            "",
        ]
        for entry in executions:
            timestamp = entry.get("triggered_at") or entry.get("timestamp") or ""
            tid = entry.get("trigger_id") or "?"
            status = entry.get("status") or entry.get("result") or "?"
            summary = (entry.get("summary") or entry.get("message") or "")[:120]
            lines.append(f"- {timestamp} `{tid}` — {status}: {summary}")
        return "\n".join(lines)

    # ── Lifecycle hooks ────────────────────────────────────────────────────

    def _hook_manager(self) -> Any:
        # Share the tool's singleton (and its mtime cache) so the command, the
        # agent tool, and the per-turn resolver all see one HookManager.
        from ..tools.hooks import _get_hook_manager

        return _get_hook_manager()

    def _resolve_hook(self, prefix: str) -> tuple[Any, CommandOutput | None]:
        """Resolve a hook by id prefix. Returns ``(hook, error)``.

        Errors on >1 match (8-char hex prefixes collide) so an edit/delete
        cannot silently hit the wrong hook.
        """
        hooks = self._hook_manager().get_hooks(self.user_id) or []
        matches = [h for h in hooks if h.id.startswith(prefix)]
        if not matches:
            return None, command_error(f"No hook matching '{prefix}'.")
        if len(matches) > 1:
            ids = ", ".join(sorted(h.id for h in matches))
            return None, command_error(
                f"'{prefix}' matches multiple hooks: {ids}. Use a longer id."
            )
        return matches[0], None

    async def _cmd_hook(self, bound: BoundArgs) -> str | CommandOutput:
        # Bare "/hook" lists. Direct ``/hook <sub>``, the ``/hooks <sub>``
        # plural alias, and the ``detail`` synonym of show all resolve to a
        # registered subcommand path and are gated on ITS flags, so nothing
        # else reaches here: the root takes zero arguments, and strict binding
        # is now the fail-closed behavior that the removed
        # ``_subcommand_denied_for_actor`` backstop used to provide (a stray
        # token is a dispatcher usage error, so it can never execute a
        # restricted subcommand; history: commit b0194056).
        return await self._cmd_hook_list(BoundArgs())

    async def _cmd_hook_list(self, bound: BoundArgs) -> str | CommandOutput:
        """List hooks, optionally narrowed by scope and/or by thread.

        The two narrowings compose and answer different questions: the
        trailing scope token says WHICH KIND of hook (``global`` for the
        global-scoped ones, ``thread`` for the ones this conversation sees),
        ``--thread`` says WHICH thread. Absent scope means unfiltered, because
        a list command's default is everything (backlog #131 wave B moved this
        off a ``--global`` flag; the flag is gone, not aliased).
        """
        enabled_only = bool(bound.get("enabled_only"))
        scope = bound.get("scope")
        global_only = scope == "global"
        thread_token = str(bound.get("thread") or "")
        if scope == "thread" and not thread_token:
            thread_token = "current"
        thread_id, thread_error = self._resolve_thread_token(thread_token)
        if thread_error is not None:
            return thread_error
        hooks = self._hook_manager().get_hooks(self.user_id) or []
        if enabled_only:
            hooks = [h for h in hooks if h.enabled]
        if global_only:
            hooks = [h for h in hooks if h.scope == "global"]
        if thread_id:
            hooks = [h for h in hooks if h.scope == "global" or h.thread_id == thread_id]
        if not hooks:
            return "No hooks found."
        lines = [
            f"Hooks: {len(hooks)} total",
            "",
            "| ID | State | Event | Action | Scope | Name |",
            "|---|---|---|---|---|---|",
        ]
        for h in sorted(hooks, key=lambda item: item.id):
            state = "enabled" if h.enabled else "disabled"
            scope = "global" if h.scope == "global" else f"thread:{h.thread_id or '?'}"
            lines.append(
                f"| `{h.id}` | {state} | {h.event} | {h.logic.action} "
                f"| {scope} | {table_cell(h.name)} |"
            )
        return "\n".join(lines)

    async def _cmd_hook_templates(self, bound: BoundArgs) -> str:
        from .hook_templates import load_templates

        templates = load_templates()
        if not templates:
            return "No bundled hook templates available."
        lines = [
            f"Bundled hook templates: {len(templates)}",
            "",
            "| ID | Event | Action | Default scope | Description |",
            "|---|---|---|---|---|",
        ]
        for t in templates:
            hook = t.hook
            lines.append(
                f"| `{t.id}` | {hook.get('event', '?')} | "
                f"{hook.get('action', 'inject_context')} | "
                f"{hook.get('scope', 'global')} | {table_cell(t.description or t.title)} |"
            )
        lines.append("")
        lines.append("Install one with `/hook install <id>`.")
        return "\n".join(lines)

    async def _cmd_hook_install(self, bound: BoundArgs) -> str | CommandOutput:
        from ..tools.hooks import _logic_preview
        from ..tools.utils import is_admin
        from .hook_templates import install_template

        template_id = str(bound.get("template_id") or "")
        text = str(bound.get("text") or "")
        disabled = bool(bound.get("disabled"))
        scope_value = bound.get("scope")
        if scope_value == "thread" and not self.thread_id:
            return command_error(
                "A thread-scoped install needs an active thread. "
                "Use --scope global or send a message first."
            )
        try:
            hook, created = install_template(
                self._hook_manager(),
                self.user_id,
                template_id,
                # The current thread is the binding whenever the effective
                # scope (override or template default) is thread-scoped.
                scope=scope_value,
                thread_id=self.thread_id or None,
                text=text or None,
                enabled=False if disabled else None,
                created_by="user",
                is_admin=is_admin(self.user_id, agent=self._agent()),
            )
        except Exception as e:  # noqa: BLE001 - surface validation as a human string
            return command_error(str(e))
        if hook is None:
            return command_error("Hook limit reached (max 50).")
        scope_desc = "all threads" if hook.scope == "global" else f"thread {hook.thread_id}"
        if not created:
            return (
                f"Template '{template_id}' is already installed as hook "
                f"`{hook.id}` for {scope_desc}. Use /hook edit or /hook delete to "
                "change or reinstall it."
            )
        return command_success(
            f"Installed template '{template_id}' as hook '{hook.name}' "
            f"({hook.id}) on {hook.event} for {scope_desc}: "
            f"{_logic_preview(hook.logic)}."
        )

    def _gated_action_error(self, action: str) -> CommandOutput | None:
        """Admin + flag gate for run_command on the command surface (or None).

        Admin is resolved from the account repo (fail-closed), mirroring the
        agent tool. The bare local CLI has no account and resolves to non-admin,
        so a run_command hook cannot be authored from an unauthenticated shell.
        """
        from ..tools.utils import is_admin
        from .hook_manager import run_command_authoring_error
        reason = run_command_authoring_error(
            action, is_admin=is_admin(self.user_id, agent=self._agent())
        )
        return command_error(reason) if reason else None

    async def _cmd_hook_create(self, bound: BoundArgs) -> str | CommandOutput:
        from ..tools.hooks import _logic_preview

        from .hook_manager import EVENT_ACTIONS, TEXT_ACTIONS, params_from_fields

        # The declared name is a repeatable positional: the bare words the
        # options left behind, joined the way the hand parser joined them.
        name = " ".join(bound.get("name") or []).strip()
        if not name:
            return command_error("create requires a name.")
        event = str(bound.get("event") or "")
        if event not in EVENT_ACTIONS:
            return command_error(f"--event must be one of: {', '.join(EVENT_ACTIONS)}.")
        action = str(bound.get("action") or "")
        legal = EVENT_ACTIONS.get(event, set())
        if action not in legal:
            return command_error(
                f"action '{action}' is not valid for event '{event}'. "
                f"Valid: {', '.join(sorted(legal))}."
            )
        gate = self._gated_action_error(action)
        if gate:
            return gate
        text = str(bound.get("text") or "")
        url = str(bound.get("url") or "")
        command = str(bound.get("command") or "")
        workflow = str(bound.get("workflow") or "")
        sets_raw = bound.get("set") or []
        # Per-action required-field prechecks (friendlier than a pydantic error).
        if action in TEXT_ACTIONS and not text:
            return command_error(f"{action} requires --text.")
        if action == "webhook" and not url:
            return command_error("webhook requires --url.")
        if action == "rewrite_arg" and not sets_raw:
            return command_error("rewrite_arg requires at least one --set arg=value.")
        if action == "run_command" and not command:
            return command_error("run_command requires --command.")
        if action == "run_workflow" and not workflow:
            return command_error("run_workflow requires --workflow <workflow_id>.")
        case_sensitive = bool(bound.get("case_sensitive"))
        conditions, cerr = _parse_hook_conditions(bound.get("cond") or [], case_sensitive)
        if cerr:
            return command_error(cerr)
        fire_conditions, ferr = _parse_hook_conditions(
            bound.get("fire_cond") or [], case_sensitive
        )
        if ferr:
            return command_error(ferr)
        updates_map, uerr = _parse_hook_sets(sets_raw)
        if uerr:
            return command_error(uerr)
        timeout_val, terr = _parse_hook_timeout(str(bound.get("timeout") or ""))
        if terr:
            return command_error(terr)
        workflow_params_val, wperr = _parse_hook_workflow_params(
            str(bound.get("workflow_params") or "")
        )
        if wperr:
            return command_error(wperr)
        params = params_from_fields(
            action,
            text=text or None,
            conditions=conditions or None,
            reason=str(bound.get("reason") or "") or None,
            updates=updates_map or None,
            url=url or None,
            command=command or None,
            timeout_seconds=timeout_val,
            workflow_id=workflow or None,
            workflow_params=workflow_params_val,
            on_fault=bound.get("on_fault"),
        )
        scope = str(bound.get("scope") or "thread")
        if scope == "thread" and not self.thread_id:
            return command_error(
                "A thread-scoped hook needs an active thread. "
                "Use --scope global or send a message first."
            )
        thread_id = self.thread_id if scope == "thread" else ""
        try:
            hook = self._hook_manager().add_hook(
                self.user_id,
                name=name,
                event=event,
                action=action,
                params=params,
                matcher=str(bound.get("matcher") or "") or None,
                fire_conditions=fire_conditions or None,
                once=bool(bound.get("once")),
                single_use=bool(bound.get("single_use")),
                scope=scope,
                thread_id=thread_id,
                enabled=not bound.get("disabled"),
                created_by="user",
            )
        except Exception as e:  # noqa: BLE001 - surface validation as a human string
            return command_error(str(e))
        if hook is None:
            return command_error("Hook limit reached (max 50).")
        scope_desc = "all threads" if hook.scope == "global" else f"thread {hook.thread_id}"
        return command_success(
            f"Created hook '{hook.name}' ({hook.id}) on {hook.event} "
            f"for {scope_desc}: {_logic_preview(hook.logic)}."
        )

    def _hook_detail(self, ref: str) -> str | CommandOutput:
        """Render one hook by id or prefix (shared with the ``detail`` synonym)."""
        hook, error = self._resolve_hook(ref)
        if hook is None:
            return error or command_error(f"No hook matching '{ref}'.")
        from ..tools.hooks import render_hook_detail

        return render_hook_detail(hook)

    async def _cmd_hook_show(self, bound: BoundArgs) -> str | CommandOutput:
        return self._hook_detail(str(bound.get("id") or ""))

    async def _cmd_hook_test(self, bound: BoundArgs) -> str | CommandOutput:
        ref = str(bound.get("id") or "")
        hook, error = self._resolve_hook(ref)
        if hook is None:
            return error or command_error(f"No hook matching '{ref}'.")
        from ..tools.hooks import render_hook_test

        return render_hook_test(hook)

    async def _cmd_hook_history(self, bound: BoundArgs) -> str | CommandOutput:
        limit = max(1, int(bound.get("limit", 20)))
        hook_id = None
        ref = str(bound.get("id") or "")
        if ref:
            hook, err = self._resolve_hook(ref)
            if hook is None:
                return err or command_error(f"No hook matching '{ref}'.")
            hook_id = hook.id

        entries = self._hook_manager().get_executions(
            self.user_id, hook_id=hook_id, limit=limit
        )
        if not entries:
            scope = f" for `{hook_id}`" if hook_id else ""
            return (
                f"No hook executions recorded{scope}. A hook that never "
                "appears here never fired; a `no_op` entry fired and produced "
                "nothing."
            )
        lines = [
            f"Hook executions: {len(entries)}"
            + (f" for `{hook_id}`" if hook_id else "")
            + " (newest first)",
            "",
            "| Time | Hook | Event | Status | Detail |",
            "|---|---|---|---|---|",
        ]
        for e in entries:
            ts = str(e.get("timestamp") or "")
            event = str(e.get("event") or "")
            if e.get("tool_name"):
                event += f" ({e['tool_name']})"
            detail = table_cell(e.get("detail"), limit=80)
            lines.append(
                f"| {ts} | `{e.get('hook_id') or '?'}` | {event} "
                f"| {e.get('status', '?')} | {detail} |"
            )
        return "\n".join(lines)

    # ── Hook approvals (require_approval holds) ────────────────────────────

    def _visible_hook_approvals(self) -> list[dict]:
        """Pending approval records this caller may see (admins see all)."""
        from ..tools.utils import is_admin
        from .hook_approvals import list_pending

        if is_admin(self.user_id, agent=self._agent()):
            return list_pending()
        return list_pending(self.user_id)

    async def _cmd_hook_approvals(self, bound: BoundArgs) -> str:
        records = self._visible_hook_approvals()
        if not records:
            return "No pending hook approvals."
        lines = [
            f"Pending hook approvals: {len(records)}",
            "",
            "| ID | Tool | Prompt | Expires | Thread |",
            "|---|---|---|---|---|",
        ]
        for r in records:
            prompt = table_cell(r.get("prompt"), limit=60)
            lines.append(
                f"| `{r.get('record_id')}` | {r.get('tool_name') or '?'} "
                f"| {prompt} | {r.get('expires_at') or '?'} "
                f"| {r.get('thread_id') or '?'} |"
            )
        lines.append("")
        lines.append("Resolve with /hook approve <id> [note] or /hook deny <id> [note].")
        return "\n".join(lines)

    async def _cmd_hook_approve(self, bound: BoundArgs) -> str | CommandOutput:
        return await self._resolve_hook_approval(bound, approved=True)

    async def _cmd_hook_deny(self, bound: BoundArgs) -> str | CommandOutput:
        return await self._resolve_hook_approval(bound, approved=False)

    async def _resolve_hook_approval(
        self, bound: BoundArgs, *, approved: bool
    ) -> str | CommandOutput:
        """Shared approve/deny path: prefix-resolve, authorize, wake the hold.

        Mirrors the REST endpoint's semantics (owner-or-admin; a resolve with
        no live waiter cleans the stale record). Runs in the API process, the
        single agent runtime, so the coordinator wake always lands in-process.
        """
        from .hook_approvals import (
            delete_record,
            get_hook_approval_coordinator,
            publish_resolved_event,
        )

        prefix = str(bound.get("id") or "")
        note = str(bound.get("note") or "").strip()
        visible = self._visible_hook_approvals()
        matches = [r for r in visible if str(r.get("record_id") or "").startswith(prefix)]
        if not matches:
            return command_error(f"No pending approval matching '{prefix}'.")
        if len(matches) > 1:
            ids = ", ".join(sorted(str(r.get("record_id")) for r in matches))
            return command_error(
                f"'{prefix}' matches multiple approvals: {ids}. "
                "Use a longer id."
            )
        record = matches[0]
        record_id = str(record.get("record_id") or "")
        woke = get_hook_approval_coordinator().resolve(
            record_id, approved=approved, resolved_by=self.user_id, note=note
        )
        if not woke:
            delete_record(record_id)
            publish_resolved_event(record, outcome="stale", resolved_by=self.user_id)
            return command_error(
                f"Approval `{record_id}` is no longer pending (it timed "
                "out, was resolved elsewhere, or its turn ended)."
            )
        decision = "Approved" if approved else "Denied"
        return command_success(
            f"{decision} `{record.get('tool_name') or 'tool call'}` "
            f"({record_id})."
        )

    async def _cmd_hook_enable(self, bound: BoundArgs) -> str | CommandOutput:
        return await self._set_hook_enabled(str(bound.get("id") or ""), enabled=True)

    async def _cmd_hook_disable(self, bound: BoundArgs) -> str | CommandOutput:
        return await self._set_hook_enabled(str(bound.get("id") or ""), enabled=False)

    async def _set_hook_enabled(self, ref: str, *, enabled: bool) -> str | CommandOutput:
        hook, error = self._resolve_hook(ref)
        if hook is None:
            return error or command_error(f"No hook matching '{ref}'.")
        if not self._hook_manager().update_hook(self.user_id, hook.id, enabled=enabled):
            return command_error(f"No hook matching '{ref}'.")
        return command_success(f"{'Enabled' if enabled else 'Disabled'} hook `{hook.id}`.")

    async def _cmd_hook_delete(self, bound: BoundArgs) -> str | CommandOutput:
        # The declared ``--yes`` flag is accepted for CLI muscle memory (and
        # the Discord cog sends it) but no longer prompts: backend handlers
        # cannot prompt, so danger_level drives any frontend confirmation.
        ref = str(bound.get("id") or "")
        hook, error = self._resolve_hook(ref)
        if hook is None:
            return error or command_error(f"No hook matching '{ref}'.")
        from .hook_manager import is_system_hook_id
        if not self._hook_manager().delete_hook(self.user_id, hook.id):
            if is_system_hook_id(hook.id):
                # Nothing stored: the system hook is already pristine.
                return (
                    f"System hook `{hook.id}` is already at its built-in "
                    "defaults (nothing to reset)."
                )
            return command_error(f"No hook matching '{ref}'.")
        if is_system_hook_id(hook.id):
            return command_success(f"Reset system hook `{hook.id}` to its built-in defaults.")
        return command_success(f"Deleted hook `{hook.id}`.")

    async def _cmd_hook_edit(self, bound: BoundArgs) -> str | CommandOutput:
        from .hook_manager import build_update_kwargs

        ref = str(bound.get("id") or "")
        hook, error = self._resolve_hook(ref)
        if hook is None:
            return error or command_error(f"No hook matching '{ref}'.")
        conds_raw = bound.get("cond") or []
        sets_raw = bound.get("set") or []
        fire_conds_raw = bound.get("fire_cond") or []
        case_sensitive = bool(bound.get("case_sensitive"))
        # The bare tokens are key=value scalar edits.
        edit_keys = {
            "name", "enabled", "event", "matcher", "action", "text", "url", "reason",
            "command", "timeout", "once", "single_use",
            "workflow", "workflow_params", "on_fault",
        }
        kv: dict[str, str] = {}
        for token in bound.get("fields") or []:
            if "=" not in token:
                return command_error(
                    f"unexpected argument '{token}' "
                    "(use key=value or --cond/--set)."
                )
            key, _, value = token.partition("=")
            key = key.strip().lower()
            if key in ("scope", "thread_id"):
                return command_error(
                    "cannot re-scope a hook via edit; delete and recreate instead."
                )
            if key not in edit_keys:
                return command_error(
                    f"unknown field '{key}'. "
                    f"Editable: {', '.join(sorted(edit_keys))}."
                )
            kv[key] = value
        # Switching TO a gated action AND any behavior edit of an existing
        # gated hook is admin + flag gated; enabled/name-only edits stay
        # ungated (shared rule in ``gated_update_action``).
        from .hook_manager import gated_update_action
        touched = set(kv) - {"action"}
        if conds_raw:
            touched.add("conditions")
        if sets_raw:
            touched.add("updates")
        if fire_conds_raw:
            touched.add("fire_conditions")
        requested_action = kv["action"].strip().lower() if "action" in kv else None
        gate_on = gated_update_action(hook.logic.action, requested_action, touched)
        if gate_on is not None:
            gate = self._gated_action_error(gate_on)
            if gate:
                return gate
        conditions, cerr = _parse_hook_conditions(conds_raw, case_sensitive)
        if cerr:
            return command_error(cerr)
        fire_conditions, ferr = _parse_hook_conditions(fire_conds_raw, case_sensitive)
        if ferr:
            return command_error(ferr)
        updates_map, uerr = _parse_hook_sets(sets_raw)
        if uerr:
            return command_error(uerr)
        timeout_val, terr = _parse_hook_timeout(kv.get("timeout", ""))
        if terr:
            return command_error(terr)
        workflow_params_val, wperr = _parse_hook_workflow_params(
            kv.get("workflow_params", "")
        )
        if wperr:
            return command_error(wperr)
        on_fault_val, oferr = _parse_hook_on_fault(kv.get("on_fault", ""))
        if oferr:
            return command_error(oferr)
        scalars: dict = {}
        if "name" in kv:
            scalars["name"] = kv["name"]
        if "event" in kv:
            scalars["event"] = kv["event"]
        if "matcher" in kv:
            scalars["matcher"] = kv["matcher"] or None
        if "enabled" in kv:
            scalars["enabled"] = coerce_value(kv["enabled"])
        if "once" in kv:
            # Raw coerced value (same idiom as enabled=): update_hook re-runs
            # model_validate, whose lax bool coercion handles yes/no/1/0 and
            # rejects garbage instead of bool("no") silently being True.
            scalars["once"] = coerce_value(kv["once"])
        if "single_use" in kv:
            scalars["single_use"] = coerce_value(kv["single_use"])
        if fire_conds_raw:
            scalars["fire_conditions"] = fire_conditions
        update_kwargs = build_update_kwargs(
            hook,
            action=kv.get("action"),
            text=kv.get("text"),
            conditions=conditions if conds_raw else None,
            reason=kv.get("reason"),
            updates=updates_map if sets_raw else None,
            url=kv.get("url"),
            command=kv.get("command"),
            timeout_seconds=timeout_val,
            workflow_id=kv.get("workflow"),
            workflow_params=workflow_params_val,
            on_fault=on_fault_val,
            scalars=scalars,
        )
        if not update_kwargs:
            return command_error("No updates provided.")
        try:
            ok = self._hook_manager().update_hook(self.user_id, hook.id, **update_kwargs)
        except Exception as e:  # noqa: BLE001
            return command_error(str(e))
        if not ok:
            return command_error(f"No hook matching '{ref}'.")
        return command_success(f"Updated hook `{hook.id}`.")

    # ── Account ───────────────────────────────────────────────────────────

    def _accounts_repo(self) -> Any | None:
        agent = self._agent()
        if agent is None:
            return None
        return getattr(agent, "accounts_repo", None)

    async def _cmd_account(self, bound: BoundArgs) -> str | CommandOutput:
        # Bare "/account" shows the current user. Every account verb is a
        # registered child, so longest-prefix dispatch routes it before this
        # handler runs; the root takes zero arguments and a typo is answered by
        # the dispatcher with did-you-mean plus the valid-subcommand list.
        return await self._cmd_account_show(BoundArgs())

    async def _cmd_account_show(self, bound: BoundArgs) -> str | CommandOutput:
        repo = self._accounts_repo()
        if repo is None:
            return command_error("Account repository unavailable.")
        user = repo.get_user_by_id(self.user_id)
        if user is None:
            return command_error(f"User '{self.user_id}' not found.")
        rows = [
            ("Selected user", self.user_id),
            ("ID", getattr(user, "id", "")),
            ("Email", getattr(user, "email", "")),
            ("Display name", getattr(user, "display_name", "")),
            ("Role", getattr(user, "role", "")),
        ]
        width = max(len(label) for label, _ in rows)
        lines = ["Current account", ""]
        for label, value in rows:
            lines.append(f"  {label:<{width}}  {value}")
        return "\n".join(lines)

    async def _cmd_account_tokens(self, bound: BoundArgs) -> str | CommandOutput:
        repo = self._accounts_repo()
        if repo is None:
            return command_error("Account repository unavailable.")
        tokens = repo.list_tokens_for_user(self.user_id) or []
        if not tokens:
            return "No API tokens issued."
        lines = [
            f"API tokens: {len(tokens)}",
            "",
            "| Prefix | Label | Created | Last used | Revoked |",
            "|---|---|---|---|---|",
        ]
        for token in tokens:
            prefix = token.hash_prefix
            label = table_cell(getattr(token, "label", ""))
            created = getattr(token, "created_at", "") or ""
            last_used = getattr(token, "last_used_at", "") or ""
            revoked = getattr(token, "revoked_at", "") or ""
            lines.append(f"| `{prefix}` | {label} | {created} | {last_used} | {revoked} |")
        return "\n".join(lines)

    async def _cmd_account_tokens_issue(self, bound: BoundArgs) -> str | CommandOutput:
        repo = self._accounts_repo()
        if repo is None:
            return command_error("Account repository unavailable.")
        label = str(bound.get("label") or "").strip() or None
        try:
            raw_token = repo.issue_token(self.user_id, label=label)
        except Exception as exc:  # noqa: BLE001
            return command_error(f"Could not issue token: {exc}")
        prefix = raw_token[:8] if isinstance(raw_token, str) else ""
        lines = [
            f"Issued API token (prefix `{prefix}`).",
            "",
            "Raw token (shown only once — save it now):",
            "",
            "```",
            str(raw_token),
            "```",
        ]
        return command_success("\n".join(lines))

    async def _cmd_account_tokens_revoke(self, bound: BoundArgs) -> str | CommandOutput:
        prefix = str(bound.get("prefix") or "")
        repo = self._accounts_repo()
        if repo is None:
            return command_error("Account repository unavailable.")
        revoked = bool(repo.revoke_token(self.user_id, prefix))
        if not revoked:
            return command_error(f"No matching token for prefix `{prefix}`.")
        return command_success(f"Revoked token `{prefix}`.")

    async def _cmd_account_platforms(self, bound: BoundArgs) -> str | CommandOutput:
        repo = self._accounts_repo()
        if repo is None:
            return command_error("Account repository unavailable.")
        platforms = repo.list_platforms_for_user(self.user_id) or []
        if not platforms:
            return "No linked chat platforms."
        lines = [
            f"Linked platforms: {len(platforms)}",
            "",
            "| Provider | Provider user ID | Created |",
            "|---|---|---|",
        ]
        for p in platforms:
            provider = table_cell(getattr(p, "provider", ""))
            puid = table_cell(getattr(p, "provider_user_id", ""))
            created = getattr(p, "created_at", "") or ""
            lines.append(f"| {provider} | `{puid}` | {created} |")
        return "\n".join(lines)

    # ── Activity / notifications ──────────────────────────────────────────

    async def _cmd_activity(self, bound: BoundArgs) -> str | CommandOutput:
        # Bare "/activity" lists. Both verbs are registered children routed
        # before this handler, and `recent` is a whole-path alias of
        # `activity list`; the root takes zero arguments and a typo is answered
        # by the dispatcher with did-you-mean plus the valid-subcommand list.
        return await self._cmd_activity_list(BoundArgs())

    async def _cmd_activity_list(self, bound: BoundArgs) -> str | CommandOutput:
        from ..core.activity_log import ActivityType, get_activity_log

        limit = max(1, int(bound.get("limit", 20)))
        activity_type_str = str(bound.get("type") or "") or None
        thread_id, thread_error = self._resolve_thread_token(bound.get("thread"))
        if thread_error is not None:
            return thread_error

        type_filter = None
        if activity_type_str:
            try:
                type_filter = ActivityType(activity_type_str)
            except ValueError:
                return command_error(f"Invalid activity type: {activity_type_str}")

        log = get_activity_log()
        entries = log.get_entries(
            self.user_id,
            limit=limit,
            activity_type=type_filter,
            thread_id=thread_id,
        )
        if not entries:
            return "No recent activity."

        lines = [
            f"Recent activity: {len(entries)}",
            "",
            "| Time | Type | Thread | Message |",
            "|---|---|---|---|",
        ]
        for entry in entries:
            # Stored aware UTC; shown in the user's zone, named. And the thread
            # id stays WHOLE: an 8-char cut collapses every telegram_<chat> and
            # discord_<guild>_<channel> row to the same stub, in a table whose
            # purpose is telling rows apart.
            ts = format_user_time_compact(getattr(entry, "timestamp", None))
            etype = getattr(entry.type, "value", str(entry.type)) if entry.type else ""
            tid = getattr(entry, "thread_id", "") or ""
            msg = table_cell(getattr(entry, "message", ""), limit=80)
            lines.append(f"| {ts} | {etype} | `{tid}` | {msg} |")
        return "\n".join(lines)

    async def _cmd_activity_notifications(self, bound: BoundArgs) -> str:
        from ..core.notifications import get_notification_store

        store = get_notification_store()
        notifications = store.get_all(self.user_id, limit=50) or []
        unread = store.get_unread_count(self.user_id)
        if not notifications:
            return f"No notifications. (Unread: {unread})"

        lines = [
            f"Notifications ({unread} unread): {len(notifications)} total",
            "",
            "| ID | Read | Summary |",
            "|---|---|---|",
        ]
        for n in notifications:
            nid = (getattr(n, "id", "") or "")[:8]
            read = "yes" if getattr(n, "read", False) else "no"
            summary = table_cell(getattr(n, "summary", ""), limit=80)
            lines.append(f"| `{nid}` | {read} | {summary} |")
        return "\n".join(lines)

    # ── Doctor (server-side diagnostics) ──────────────────────────────────

    async def _cmd_doctor(self, bound: BoundArgs) -> str:
        # Both sections are registered children routed before this handler, so
        # the root takes zero arguments and runs the pair; a typo is answered by
        # the dispatcher with did-you-mean plus the valid-subcommand list.
        #
        # The root is a READOUT of both sections, so it composes their BODIES:
        # a section that could not be read (doctor model on a settings error)
        # returns a typed error whose text is itself the diagnostic, and
        # composing the rendered value instead would print its artifact
        # mid-body (the pre-#132 bug: a literal sentinel before "Model").
        sections = [
            await self._cmd_doctor_auth(BoundArgs()),
            await self._cmd_doctor_model(BoundArgs()),
        ]
        return "\n\n".join(
            section.text if isinstance(section, CommandOutput) else section
            for section in sections
        )

    async def _cmd_doctor_auth(self, bound: BoundArgs) -> str:
        repo = self._accounts_repo()
        if repo is None:
            return "Auth\n  Selected user  " + self.user_id
        user = repo.get_user_by_id(self.user_id)
        rows = [
            ("Selected user", self.user_id),
            ("Resolved ID", getattr(user, "id", "") if user else ""),
            ("Display name", getattr(user, "display_name", "") if user else ""),
            ("Role", getattr(user, "role", "") if user else ""),
        ]
        width = max(len(label) for label, _ in rows)
        lines = ["Auth", ""]
        for label, value in rows:
            lines.append(f"  {label:<{width}}  {value}")
        return "\n".join(lines)

    async def _cmd_doctor_model(self, bound: BoundArgs) -> str | CommandOutput:
        try:
            settings = await self.api.get_settings(user_id=self.user_id)
        except Exception as exc:  # noqa: BLE001
            return command_error(f"Could not load settings: {exc}")
        settings_dict = settings if isinstance(settings, dict) else {}
        rows = [
            ("Provider", settings_dict.get("llm_provider", "")),
            ("Model", settings_dict.get("llm_model", "")),
            ("Base URL", str(settings_dict.get("llm_base_url", ""))[:80]),
            ("Context", settings_dict.get("llm_context_length") or "auto"),
            ("Ollama num_ctx", settings_dict.get("llm_ollama_num_ctx") or "auto"),
            ("Status", "available"),
        ]
        width = max(len(label) for label, _ in rows)
        lines = ["Model", ""]
        for label, value in rows:
            lines.append(f"  {label:<{width}}  {value}")
        return "\n".join(lines)

    # ── Status / inspection ───────────────────────────────────────────────

    async def _cmd_status(self, bound: BoundArgs) -> str | CommandOutput:
        thread_error = self._require_thread()
        if thread_error:
            return thread_error
        settings, ctx, tools_data, todos, thread_cfg = await asyncio.gather(
            self.api.get_settings(),
            self.api.get_context_stats(self.thread_id),
            self.api.get_default_tools(self.user_id),
            self.api.list_todos(self.user_id),
            self.api.get_thread_config(self.thread_id),
            return_exceptions=True,
        )
        settings = _dict_result(settings)
        ctx = _dict_result(ctx)
        tools_data = _dict_result(tools_data)
        todos = _dict_list_result(todos)
        thread_cfg = _dict_result(thread_cfg)

        model = settings.get("llm_model", "?")
        provider = settings.get("llm_provider", "?")
        base_url = settings.get("llm_base_url")
        # Function-local: this module must not pull the vendored package at
        # import time. `cliproxy.py` is the single authority on what a CLIProxy
        # URL looks like, so the "(via CLIProxy)" label cannot disagree with the
        # code that actually routes to it (the open-coded substring test this
        # replaces missed a proxy addressed by IP, which matches on port).
        from ..vendor.react_agent.cliproxy import looks_like_cliproxy_url

        if base_url and looks_like_cliproxy_url(base_url):
            provider = f"{provider} (via CLIProxy)"
        thinking = settings.get("llm_extended_thinking", False)
        effort = settings.get("llm_reasoning_effort")
        think_str = "off"
        if thinking and str(effort or "").lower() != "off":
            think_str = f"on ({effort})" if effort else "on"

        total = ctx.get("total_tokens", 0)
        limit = ctx.get("context_limit", 0)
        pct = ctx.get("usage_percentage", 0)
        compactions = ctx.get("compaction_count", 0)
        ctx_mode = ctx.get("context_management") or settings.get("context_management", "?")

        default_count = len(tools_data.get("default_tools", []))
        available_count = len(tools_data.get("available_tools", []))

        task_parts = []
        if todos:
            t_pending = sum(1 for t in todos if t.get("status") == "pending")
            t_in_prog = sum(1 for t in todos if t.get("status") == "in_progress")
            if t_pending:
                task_parts.append(f"{t_pending} pending")
            if t_in_prog:
                task_parts.append(f"{t_in_prog} in progress")
        tasks_str = " / ".join(task_parts) if task_parts else "none"

        # Model block honesty (#159): the single global line is the common
        # case and stays byte-identical. A thread override (model OR
        # provider axis: /provider switch <p> thread writes provider-only)
        # or an active fallback hold adds its own line, and with more than
        # one line shown exactly one carries the (active) marker, resolving
        # the precedence the LLM-build path actually applies: hold > thread
        # override > global.
        override = _thread_override_text(
            _dict_result(thread_cfg.get("llm_config")), settings
        )
        hold = self._hold_from_thread_config(thread_cfg)
        model_lines = [f"{model} | {provider} | thinking: {think_str}"]
        if override:
            model_lines.append(override)
        if hold is not None:
            model_lines.append(f"fallback hold: {self._hold_summary(hold)}")
        if len(model_lines) > 1:
            model_lines[0] = f"global: {model_lines[0]}"
            label, _sep, rest = model_lines[-1].partition(": ")
            model_lines[-1] = f"{label} (active): {rest}"

        lines = [
            "Nymeria Status",
            "",
            "Model",
            *[f"  {line}" for line in model_lines],
            "",
            "Context",
            f"  {fmt_tokens(total)} / {fmt_tokens(limit)} tokens ({pct}%)",
            f"  mode: {ctx_mode}"
            + (f" | {compactions} compaction{'s' if compactions != 1 else ''}" if compactions else ""),
            "",
            "Tools",
            f"  {default_count} core / {available_count} available",
            "",
            "Tasks",
            f"  {tasks_str}",
            "",
            f"thread: {self.thread_id}",
            f"user:   {self.user_id}",
        ]
        return "\n".join(lines)

    async def _cmd_thread(self, bound: BoundArgs) -> str | CommandOutput:
        thread_error = self._require_thread()
        if thread_error:
            return thread_error
        stats = await self.api.get_context_stats(self.thread_id)
        lines = [
            "Thread Info",
            f"  thread id: {self.thread_id}",
            f"  tokens: {fmt_tokens(stats.get('total_tokens', 0))} / "
            f"{fmt_tokens(stats.get('context_limit', 0))} "
            f"({stats.get('usage_percentage', 0)}%)",
            f"  compactions: {stats.get('compaction_count', 0)}",
            f"  mode: {stats.get('context_management', 'unknown')}",
        ]
        return "\n".join(lines)

    async def _cmd_context(self, bound: BoundArgs) -> str | CommandOutput:
        thread_error = self._require_thread()
        if thread_error:
            return thread_error
        ctx, thread_cfg, settings, categories, tools_data = await asyncio.gather(
            self.api.get_context_stats(self.thread_id),
            self.api.get_thread_config(self.thread_id),
            self.api.get_settings(),
            self.api.get_tool_categories(),
            self.api.get_default_tools(self.user_id),
            return_exceptions=True,
        )
        # Read the stats result BEFORE coercing. _dict_result turns any
        # exception into {}, which is the resilience this gather wants (one
        # flaky sub-call must not take the whole breakdown down) but it also
        # swallowed the 404 that means "there is no such thread", so an
        # unknown or foreign id rendered a confident, entirely fictional
        # report instead of the refusal /thread switch gives for the same id.
        if _is_not_found(ctx):
            return command_error(f"No thread matching '{self.thread_id}'.")
        ctx = _dict_result(ctx)
        thread_cfg = _optional_dict_result(thread_cfg)
        settings = _dict_result(settings)
        categories = _dict_result(categories)
        tools_data = _dict_result(tools_data)

        effective_model = ctx.get("model") or settings.get("llm_model", "?")
        provider = settings.get("llm_provider", "?")
        base_url = settings.get("llm_base_url")
        # Function-local: this module must not pull the vendored package at
        # import time. `cliproxy.py` is the single authority on what a CLIProxy
        # URL looks like, so the "(via CLIProxy)" label cannot disagree with the
        # code that actually routes to it (the open-coded substring test this
        # replaces missed a proxy addressed by IP, which matches on port).
        from ..vendor.react_agent.cliproxy import looks_like_cliproxy_url

        if base_url and looks_like_cliproxy_url(base_url):
            provider = f"{provider} (via CLIProxy)"

        lines = ["Context Breakdown", "", "Model", f"  {effective_model} | {provider}"]
        if thread_cfg:
            override = _thread_override_text(
                _dict_result(thread_cfg.get("llm_config")), settings
            )
            if override:
                lines.append(f"  {override}")

        total = ctx.get("total_tokens", 0)
        limit = ctx.get("context_limit", 0)
        pct = ctx.get("usage_percentage", 0)
        compactions = ctx.get("compaction_count", 0)
        ctx_mode = ctx.get("context_management") or settings.get("context_management", "?")
        cumulative = ctx.get("cumulative_tokens", 0)

        lines.append("")
        lines.append("Context Window")
        if not limit:
            # A window of zero capacity is not a reading, it is the absence of
            # one: the stats call failed and 0 is the fallback for both halves.
            # Say so rather than printing "0 / 0 tokens (0%)" as a measurement.
            token_line = "  unavailable (no context stats for this thread)"
        else:
            token_line = f"  {fmt_tokens(total)} / {fmt_tokens(limit)} tokens ({pct}%)"
        if cumulative:
            token_line += f" (cumulative: {fmt_tokens(cumulative)})"
        lines.append(token_line)
        if compactions:
            lines.append(f"  {compactions} compaction{'s' if compactions != 1 else ''}")
        mode_line = f"  mode: {ctx_mode}"
        if ctx_mode == "auto_compact":
            # Report the threshold that GOVERNS. compact_threshold is the
            # percentage knob and is inert unless compact_threshold_mode says
            # "percentage", so printing it unconditionally told a tokens-mode
            # deployment that compaction fires at 35% of a 1M window when it
            # actually fires at 200k, contradicting /status and /usage. The
            # trigger from context stats is the runtime-resolved one (already
            # clamped to the model's context), so it is preferred over the
            # raw setting.
            mode = str(settings.get("compact_threshold_mode") or "tokens")
            trigger_tokens = ctx.get("compact_trigger_tokens") or settings.get(
                "compact_threshold_tokens"
            )
            threshold = settings.get("compact_threshold")
            if mode == "tokens" and trigger_tokens:
                mode_line += f" (threshold {fmt_tokens(trigger_tokens)} tokens)"
            elif mode == "percentage" and threshold:
                mode_line += f" (threshold {int(threshold * 100)}%)"
        lines.append(mode_line)

        default_tools = _string_set_result(tools_data.get("default_tools"))
        available_tools = _dict_list_result(tools_data.get("available_tools"))
        raw_cats = _dict_result(categories.get("categories"))
        cats = {
            cat_name: _string_set_result(cat_tools)
            for cat_name, cat_tools in raw_cats.items()
        }
        disabled: set[str] = set()
        extra_enabled: set[str] = set()
        if thread_cfg:
            disabled = _string_set_result(thread_cfg.get("disabled_tools"))
            extra_enabled = _string_set_result(thread_cfg.get("enabled_tools"))
        effective = (default_tools - disabled) | extra_enabled

        lines.append("")
        lines.append("Tools")
        lines.append(f"  {len(effective)} enabled (of {len(available_tools)} available)")
        for cat_name in sorted(cats):
            cat_tools = cats[cat_name]
            enabled_in_cat = len(cat_tools & effective)
            total_in_cat = len(cat_tools)
            if enabled_in_cat == total_in_cat:
                lines.append(f"  {cat_name}: {total_in_cat}")
            else:
                lines.append(f"  {cat_name}: {enabled_in_cat}/{total_in_cat}")

        lines.append("")
        lines.append("Thread Overrides")
        override_lines: list[str] = []
        if thread_cfg:
            instructions = thread_cfg.get("instructions")
            if instructions:
                override_lines.append(f"  instructions: {len(instructions)} chars")
            if disabled:
                override_lines.append(f"  disabled: {', '.join(sorted(disabled))}")
            if extra_enabled:
                override_lines.append(f"  enabled: {', '.join(sorted(extra_enabled))}")
            # An llm_config entry is nullable and null means inherit, so the
            # non-null ones ARE the overrides. Leaving them out let a thread
            # pinned to another model, provider or reasoning effort report
            # "none", which is the worst answer from the section a user reads
            # when one thread behaves unlike the rest (same family as #236).
            llm_overrides = {
                key: value
                for key, value in _dict_result(thread_cfg.get("llm_config")).items()
                if value is not None
            }
            if llm_overrides:
                rendered = ", ".join(f"{k}={v}" for k, v in sorted(llm_overrides.items()))
                override_lines.append(f"  llm: {rendered}")
            sequential = thread_cfg.get("sequential_tool_execution")
            if sequential is not None:
                override_lines.append(
                    f"  sequential tools: {'on' if sequential else 'off'}"
                )
        if not override_lines:
            # Backstop: the config's own verdict wins over this list. Whatever
            # this section has not learned to name, it must not answer "none"
            # against a config that says otherwise.
            if thread_cfg and thread_cfg.get("has_customizations"):
                override_lines.append(
                    "  set, but not itemized here (see /thread config)"
                )
            else:
                override_lines.append("  none (using global defaults)")
        lines.extend(override_lines)

        lines.append("")
        lines.append(f"thread: {self.thread_id}")
        return "\n".join(lines)

    # ── Model / thinking ──────────────────────────────────────────────────

    async def _cmd_model(self, bound: BoundArgs) -> str | CommandOutput:
        name = str(bound.get("name") or "").strip()
        scope = str(bound.get("scope") or "")
        if not name:
            # Bare /model, and /model <scope>: both show. Before the trailing
            # scope token was declared, "/model global" set the model to the
            # literal string "global".
            settings = await self.api.get_settings()
            tc = await self.api.get_thread_config(self.thread_id) if self.thread_id else None
            llm_cfg = (tc or {}).get("llm_config") or {}
            thread_model = llm_cfg.get("model")
            lines = [
                f"global: {settings.get('llm_model', '?')} ({settings.get('llm_provider', '?')})"
            ]
            if thread_model:
                lines.append(f"this thread: {thread_model} (override)")
            else:
                lines.append("this thread: using global default")
            lines.append("set with: /model <name> [global|thread]")
            text = "\n".join(lines)
            form = await self._model_picker_form(settings, thread_model, scope=scope)
            if form is None:
                return text
            return command_info(text, data=command_data(form=form))

        if not bound.get("force"):
            # Deny an off-list model with near-matches: a typo used to be
            # written verbatim and only surfaced as a provider error on the
            # next turn. The list is best effort, so an unlistable provider
            # (custom base URL, bare-id proxy) still writes as before, and
            # --force is the documented escape.
            known = await self._available_model_ids()
            if known and name not in known:
                close = difflib.get_close_matches(name, known, n=3, cutoff=0.6)
                suggestion = f" Did you mean: {', '.join(close)}?" if close else ""
                return command_error(
                    f"Unknown model: {name}.{suggestion}"
                    " Use --force to set it anyway."
                )

        # B14: a claude-* id on an openai-routed CLIProxy base URL serves
        # but silently loses the Claude OAuth treatment; say so with the
        # success rather than letting identity drift be the first evidence.
        # The note keys on the scope actually WRITTEN (bare scope = global,
        # matching the branch below).
        drift_note = ""
        if name.casefold().startswith("claude"):
            drift_note = await self._claude_on_openai_cliproxy_note(
                "thread" if scope == "thread" else "global"
            )

        if scope == "thread":
            thread_error = self._require_thread()
            if thread_error:
                return thread_error
            await self.api.update_thread_config(
                self.thread_id, user_id=self.user_id, llm_config={"model": name}
            )
            return command_success(
                f"Model for this thread set to {name}.{drift_note}",
                data=command_data(state={"model": name}),
            )
        # A bare scope writes the GLOBAL default, which update_settings gates to
        # admins. That gate stays where it is; this only replaces the message,
        # because the generic "Admin only" 403 dead-ends the caller on the one
        # command whose narrower form they ARE allowed to run. `/provider`
        # already offers non-admins thread scope only; `/model` was the outlier
        # (found live 2026-08-26). `is False` and not falsy: the agent context
        # is None and keeps its access, per the sibling gates in this file.
        if self.is_admin is False:
            return command_error(
                "Setting the global default model is admin only. To change the "
                f"model for this conversation instead, use `/model {name} thread`."
            )
        result = await self.api.update_settings(user_id=self.user_id, llm_model=name)
        msg = f"Global model set to {name}."
        if result.get("restart_required"):
            msg += " (restart required to take effect)"
        return command_success(msg + drift_note)

    async def _available_model_ids(self) -> list[str]:
        """Model ids the active provider currently lists, or [] when unknown.

        Best effort by design, like the picker: a provider with no listing
        endpoint must not be able to block a legitimate model change.
        """

        try:
            models = await self.api.list_available_models()
            ids = [
                str(entry.get("id") or entry.get("name") or "")
                for entry in models or []
            ]
        except Exception:  # noqa: BLE001 - the guard degrades, never blocks.
            logger.debug("model guard: list_available_models failed", exc_info=True)
            return []
        return [model_id for model_id in ids if model_id]

    async def _model_picker_form(
        self,
        settings: dict[str, Any],
        thread_model: str | None,
        *,
        scope: str = "",
    ) -> dict[str, Any] | None:
        """Build the declarative model-picker form, or None when unavailable.

        Best effort: if the provider exposes no model list the bare ``/model``
        keeps its plain info output and rich clients simply get no form.
        ``scope`` is the explicitly typed scope when there is one, so
        ``/model global`` submits into the scope the user named.
        """

        current = str(thread_model or settings.get("llm_model", "") or "")
        options = await resolve_models(self, current=current)
        if not options:
            return None
        scope = scope or ("thread" if self.thread_id else "global")
        # One payload builder shared with the /provider switch handoff.
        return model_select_form(options, scope)

    async def _cmd_model_list(self, bound: BoundArgs) -> str:
        models = await self.api.list_available_models()
        settings = await self.api.get_settings()
        current = settings.get("llm_model", "")
        if not models:
            return "No models returned from provider."
        lines = [
            f"Available Models: {len(models)} from {settings.get('llm_provider', '?')}"
        ]

        def _row(m: dict) -> str:
            model_id = m.get("id") or m.get("name", "?")
            ctx_len = m.get("context_length") or m.get("context_window")
            ctx_str = f" | {fmt_tokens(ctx_len)} ctx" if ctx_len else ""
            marker = " (current)" if model_id == current else ""
            return f"- {model_id}{marker}{ctx_str}"

        shown = models[:25]
        # Group by source only against a CLIProxy pool (it spans every
        # logged-in subscription, and the flat view is how Claude ids read
        # as a bug on a Gemini route) and only when the DISPLAYED rows span
        # more than one source. Direct providers carry owned_by too
        # (OpenAI: system/openai/openai-internal) and there it is noise,
        # so the gate is the runtime's own URL predicate. Headings and
        # rows stay at column 0: the outcome renderer dedents the first
        # body line, so indented group blocks would render lopsided.
        from ..vendor.react_agent.cliproxy import looks_like_cliproxy_url

        base_url = str(settings.get("llm_base_url") or "")
        via_cliproxy = bool(base_url) and looks_like_cliproxy_url(base_url)
        owners = {str(m.get("owned_by") or "") for m in shown}
        if via_cliproxy and len({owner for owner in owners if owner}) > 1:
            groups: dict[str, list[dict]] = {}
            for m in shown:
                groups.setdefault(str(m.get("owned_by") or ""), []).append(m)
            for owner in sorted(groups, key=lambda o: (o == "", o)):
                lines.append(f"[{owner or 'unattributed'}]")
                lines.extend(_row(m) for m in groups[owner])
        else:
            lines.extend(_row(m) for m in shown)
        if len(models) > 25:
            lines.append(f"(showing 25 of {len(models)})")
        return "\n".join(lines)

    async def _cmd_fast(self, bound: BoundArgs) -> str | CommandOutput:
        return await self._apply_tier_command("fast", bound)

    async def _cmd_smart(self, bound: BoundArgs) -> str | CommandOutput:
        return await self._apply_tier_command("smart", bound)

    async def _cmd_fast_set(self, bound: BoundArgs) -> str | CommandOutput:
        return await self._set_tier_model("fast", bound)

    async def _cmd_smart_set(self, bound: BoundArgs) -> str | CommandOutput:
        return await self._set_tier_model("smart", bound)

    async def _set_tier_model(self, tier: str, bound: BoundArgs) -> str | CommandOutput:
        """Shared /fast set and /smart set handler: store one tier value."""
        from ..config.model_tiers import is_tier_alias

        model_id = str(bound.get("model") or "").strip()
        if is_tier_alias(model_id):
            return command_error(
                f"Cannot set the {tier} tier to another tier alias "
                f"({model_id}). Use a model id or provider:model."
            )
        key = "llm_fast_model" if tier == "fast" else "llm_smart_model"
        result = await self.api.update_settings(
            user_id=self.user_id, **{key: model_id}
        )
        msg = f"{tier.capitalize()} model set to {model_id}."
        if result.get("restart_required"):
            msg += " (restart required to take effect)"
        return command_success(msg)

    async def _apply_tier_command(self, tier: str, bound: BoundArgs) -> str | CommandOutput:
        """Shared /fast and /smart handler: show / toggle / on / off."""
        from ..config.model_tiers import plan_tier_switch, resolve_tier

        label = tier.capitalize()
        # `set` is a registered child with its own schema, so the only tokens
        # that reach the root are the toggle words and typos.
        sub = str(bound.get("mode") or "").lower()
        if sub not in {"", "on", "off"}:
            return self._usage_error(tier)
        settings = await self.api.get_settings()

        if not self.thread_id:
            resolved = resolve_tier(tier, settings)
            if resolved is None or not resolved[1]:
                return command_error(f"{label} model is not configured.")
            return f"{label} tier resolves to {resolved[1]} ({resolved[0]})."

        # Resolve against the thread's effective provider so an unset tier honors
        # a per-thread provider override (matching the CLI handler).
        tc = await self.api.get_thread_config(self.thread_id)
        llm_cfg = (tc or {}).get("llm_config") or {}
        cur_provider = str(llm_cfg.get("provider") or settings.get("llm_provider") or "")
        cur_model = str(llm_cfg.get("model") or settings.get("llm_model") or "").strip()

        plan = plan_tier_switch(
            tier,
            settings,
            cur_provider=cur_provider,
            cur_model=cur_model,
            mode=sub or "toggle",
        )
        if plan is None:
            return command_error(f"{label} model is not configured.")
        target_provider, target_model, enabled = plan

        await self.api.update_thread_config(
            self.thread_id,
            user_id=self.user_id,
            llm_config={"provider": target_provider, "model": target_model},
        )
        mode = label if enabled else "default"
        return command_success(
            f"This thread switched to {mode} model "
            f"({target_model}, {target_provider})."
        )

    async def _cmd_background(self, bound: BoundArgs) -> str:
        """Show the global background/utility model tier.

        Unlike /fast and /smart this never switches the thread's agent model: the
        background tier is a utility model (extraction now, more later), so it is
        global-only with no thread toggle. The set, set-url, and clear verbs are
        registered children with their own schemas, and `show` is a whole-path
        alias of the root, so this takes zero arguments.
        """
        from ..config.model_tiers import resolve_tier

        settings = await self.api.get_settings()

        configured = str(settings.get("llm_background_model") or "").strip()
        base_url = str(settings.get("llm_background_base_url") or "").strip()
        resolved = resolve_tier("background", settings)
        lines = []
        if configured:
            lines.append(f"Background model: {configured}")
        else:
            lines.append("Background model: (unset, falls back to the main model)")
        if resolved and resolved[1]:
            lines.append(f"Resolves to: {resolved[1]} ({resolved[0]})")
        if base_url:
            lines.append(f"Base URL override: {base_url}")
        return "\n".join(lines)

    async def _cmd_background_set(self, bound: BoundArgs) -> str | CommandOutput:
        from ..config.model_tiers import is_tier_alias

        model_id = str(bound.get("model") or "").strip()
        if is_tier_alias(model_id):
            return command_error(
                "Cannot set the background tier to another tier alias "
                f"({model_id}). Use a model id or provider:model."
            )
        result = await self.api.update_settings(
            user_id=self.user_id, llm_background_model=model_id
        )
        msg = f"Background model set to {model_id}."
        if result.get("restart_required"):
            msg += " (restart required to take effect)"
        return command_success(msg)

    async def _cmd_background_set_url(self, bound: BoundArgs) -> str | CommandOutput:
        base_url = str(bound.get("base_url") or "").strip()
        old_value = self._current_setting_value("llm_background_base_url")
        result = await self.api.update_settings(
            user_id=self.user_id, llm_background_base_url=base_url
        )
        # The owner alert keys on WHICH SETTING changed, never on which
        # command spelling changed it: this write is the same egress move as
        # `/env set LLM_BACKGROUND_BASE_URL` and must alert identically.
        self._alert_applied_agent_settings(
            result,
            {"llm_background_base_url": base_url},
            {"llm_background_base_url": old_value},
        )
        msg = f"Background base URL set to {base_url}."
        if result.get("restart_required"):
            msg += " (restart required to take effect)"
        return command_success(msg)

    async def _cmd_background_clear(self, bound: BoundArgs) -> str | CommandOutput:
        # Empty strings clear both keys in the env file (mirrors how the
        # frontend clears a tier field); None would be filtered out.
        old_value = self._current_setting_value("llm_background_base_url")
        result = await self.api.update_settings(
            user_id=self.user_id,
            llm_background_model="",
            llm_background_base_url="",
        )
        # Clearing the base URL is a settings change too: an agent silently
        # reverting an owner-set URL must be as loud as setting one.
        self._alert_applied_agent_settings(
            result,
            {"llm_background_base_url": ""},
            {"llm_background_base_url": old_value},
        )
        return command_success("Background model cleared (falls back to the main model).")

    # /fallback, /think and the /provider family live in
    # command_executor_llm.py (LLMCommandsMixin).

    # ── Settings ──────────────────────────────────────────────────────────
    #
    # The near-duplicate /config family folded into /settings in backlog
    # #131: every `config` spelling is now a whole-path alias of the command
    # below, and the bare root IS the show view (so `settings show` is an
    # alias of the root rather than a command of its own).

    async def _cmd_settings(self, bound: BoundArgs) -> str:
        settings = await self.api.get_settings()
        effort = settings.get("llm_reasoning_effort")
        base_url = settings.get("llm_base_url")

        lines = ["Settings", "", "LLM"]
        lines.append(f"  provider: {settings.get('llm_provider', '?')}")
        lines.append(f"  model: {settings.get('llm_model', '?')}")
        lines.append(f"  temperature: {settings.get('llm_temperature', '?')}")
        lines.append(f"  thinking: {'on' if settings.get('llm_extended_thinking') else 'off'}")
        if effort:
            lines.append(f"  reasoning effort: {effort}")
        if base_url:
            lines.append(f"  base url: {base_url}")

        lines.append("")
        lines.append("Context")
        lines.append(f"  mode: {settings.get('context_management', '?')}")
        if settings.get("compact_threshold_mode") == "percentage":
            threshold = settings.get("compact_threshold", 0) or 0
            lines.append(f"  compact threshold: {int(threshold * 100)}%")
        else:
            lines.append(
                f"  compact threshold: {settings.get('compact_threshold_tokens', '?')} tokens"
            )
        lines.append(f"  keep messages: {settings.get('compact_keep_messages', '?')}")
        lines.append(f"  memory char limit: {settings.get('memory_char_limit', '?')}")
        lines.append(f"  memory max entries: {settings.get('memory_max_entries', '?')}")
        compact_model = settings.get("compact_model")
        if compact_model:
            lines.append(f"  compact model: {compact_model}")

        lines.append("")
        lines.append("System")
        lines.append(f"  log level: {settings.get('log_level', '?')}")
        lines.append(f"  watchdog: {'on' if settings.get('watchdog_enabled') else 'off'}")
        if settings.get("watchdog_enabled"):
            lines.append(f"  watchdog interval: {settings.get('watchdog_interval_minutes', '?')}m")
        return "\n".join(lines)

    async def _cmd_settings_get(self, bound: BoundArgs) -> str | CommandOutput:
        key = str(bound.get("key") or "")
        settings = await self.api.get_settings()
        if key in settings:
            return f"{key} = {settings[key]}"
        available = ", ".join(sorted(settings.keys())[:30])
        return command_error(f"Unknown setting '{key}'. Available: {available}")

    async def _cmd_settings_set(self, bound: BoundArgs) -> str | CommandOutput:
        from ..api.schemas.settings import resolve_settings_field_name

        # One spelling rule with /env set: field names and env-var names both
        # resolve; the applier rejects genuinely unknown keys.
        key = resolve_settings_field_name(str(bound.get("key") or ""))
        blocked = self._agent_settings_write_block(key)
        if blocked is not None:
            return blocked
        parsed = coerce_value(str(bound.get("value") or ""))
        old_value = self._current_setting_value(key)
        result = await self.api.update_settings(user_id=self.user_id, **{key: parsed})
        return self._render_settings_write(key, parsed, result, old_value)

    def _agent_settings_write_block(self, key: str) -> CommandOutput | None:
        """Gate-integrity carve-out (#157) for the GLOBAL master switches.

        Non-human actors cannot write `AGENT_WRITE_BLOCKED_SETTINGS` through
        the command surface, the way auth_write cannot touch system
        credentials. Scope honesty: this covers the global `hooks_enabled`
        only; the per-thread override remains a designed agent capability
        (thread config is agent-writable by design). Everything else stays
        agent-writable (vault posture: containment is loudness and
        reversibility, not blocking). Keyed on actor != "user" rather than
        == "agent" so a future "system" actor fails conservatively closed.
        """
        if self.actor == "user":
            return None
        from ..api.schemas.settings import AGENT_WRITE_BLOCKED_SETTINGS

        if key not in AGENT_WRITE_BLOCKED_SETTINGS:
            return None
        return command_error(
            f"{key} controls the gating machinery and cannot be changed from an "
            "agent turn. Ask the user to run this command themselves."
        )

    def _current_setting_value(self, key: str) -> Any:
        """Best-effort pre-write read so the owner alert can name the value
        being replaced (a "revert if unexpected" alert is not actionable
        without it). None on any failure; the alert then omits the clause."""
        try:
            from ..config import get_settings

            return getattr(get_settings(), key, None)
        except Exception:  # noqa: BLE001 - advisory read only
            return None

    def _alert_applied_agent_settings(
        self, result: dict, new_values: dict, old_values: dict | None = None
    ) -> None:
        """Owner alerts for agent-issued writes that APPLIED to sensitive keys.

        Called by EVERY handler that writes settings (the shared
        `_render_settings_write` tail plus the /background family, which
        renders its own copy): the alert must key on which setting changed,
        never on which command spelling changed it. Alerts only for
        actor="agent" (a future "system" actor is a deliberate platform
        write, not an injectable turn).
        """
        if self.actor != "agent":
            return
        from ..api.schemas.settings import AGENT_WRITE_ALERT_SETTINGS

        applied = set(result.get("updated") or []) & AGENT_WRITE_ALERT_SETTINGS
        for key in applied:
            self._alert_agent_settings_write(
                key, new_values.get(key), (old_values or {}).get(key)
            )

    def _alert_agent_settings_write(self, key: str, value: Any, old_value: Any) -> None:
        """One owner alert for one agent-issued sensitive-settings write (#157).

        send_owner_alert is deliberately not silenceable by thread
        notification levels and never raises, but it does SYNCHRONOUS
        network I/O (per-destination 30s timeouts), and this seam runs on
        the API event loop, so dispatch is fire-and-forget on the default
        executor (asyncio.run and the API lifespan both drain it on
        shutdown). The inner closure carries its own try/except: an alert is
        containment, not a gate, and must never fail the write it reports
        on. Secret-named values (old and new) are withheld from the message;
        non-secret URL values are echoed as pasted, so a URL embedding
        userinfo reaches the owner's external channels verbatim.
        """
        try:
            from ..api.routers.settings import _is_secret_setting_key

            secret = _is_secret_setting_key(key)
            shown = "(value withheld: secret)" if secret else str(value)
            was = ""
            if old_value is not None and str(old_value) != "":
                was = (
                    " (replacing a previous value)"
                    if secret
                    else f" (was {old_value})"
                )
            surface = self.surface or "unknown surface"
            message = (
                f"An agent changed server setting {key} to {shown}{was} "
                f"via {surface} on thread {self.thread_id or 'unknown'}. "
                "Review with /env show and revert with /env set if this was "
                "not expected."
            )
            user_id = self.user_id
            thread_id = self.thread_id

            def _dispatch() -> None:
                try:
                    from ..config import get_settings
                    from .notification_dispatch import send_owner_alert

                    send_owner_alert(
                        message,
                        get_settings(),
                        user_id=user_id,
                        thread_id=thread_id,
                    )
                except Exception as e:  # noqa: BLE001 - never fail the write
                    logger.error(
                        "Agent settings-write alert failed for %s: %s", key, e
                    )

            try:
                loop = asyncio.get_running_loop()
            except RuntimeError:
                # Sync context: the established worker-side calling shape.
                _dispatch()
            else:
                loop.run_in_executor(None, _dispatch)
        except Exception as e:  # noqa: BLE001 - never fail the write
            logger.error("Agent settings-write alert failed for %s: %s", key, e)

    def _render_settings_write(
        self, key: str, parsed: Any, result: dict, old_value: Any = None
    ) -> str | CommandOutput:
        """Honest outcome for a settings write, shared by /settings set and /env set.

        The applier can accept a request yet apply nothing (an explicit None
        outside _CLEARABLE_NULL_SETTINGS is filtered); reporting "set" without
        consulting ``updated`` would be the same silent-drop lie the applier's
        unknown-key rejection exists to kill. Server warnings ride the typed
        warning level (#132), not hand-authored text in a success body.
        """
        if key not in (result.get("updated") or []):
            if parsed is None:
                return command_error(
                    f"{key} was not applied: this setting does not support "
                    "clearing. Set a real value instead."
                )
            return command_error(f"{key} was not applied.")
        self._alert_applied_agent_settings(result, {key: parsed}, {key: old_value})
        msg = f"{key} set to {parsed}."
        if result.get("restart_required"):
            msg += " (restart required to take effect)"
        warnings = [str(w) for w in (result.get("warnings") or [])]
        if warnings:
            return command_warning("\n".join([msg] + warnings))
        return command_success(msg)

    # ── Env ───────────────────────────────────────────────────────────────

    async def _cmd_env(self, bound: BoundArgs) -> str:
        # Bare "/env" shows the variables; get and set are registered children
        # routed before this handler, so the root takes zero arguments and a
        # typo is answered by the dispatcher with did-you-mean plus the
        # valid-subcommand list. The root carries `env show`'s admin gate.
        return await self._cmd_env_show(BoundArgs())

    async def _cmd_env_show(self, bound: BoundArgs) -> str:
        data = await self.api.get_env_vars(user_id=self.user_id)
        entries = data.get("entries", [])
        by_cat: dict[str, list] = {}
        for e in entries:
            by_cat.setdefault(e["category"], []).append(e)
        total_set = sum(1 for e in entries if e["is_set"])
        lines = [f"Environment Variables: {len(entries)} total ({total_set} set)"]
        for cat, items in by_cat.items():
            lines.append("")
            lines.append(f"{cat}")
            for e in items:
                if e["is_set"]:
                    mark = "[secret]" if e["is_secret"] else "[set]   "
                    lines.append(f"  {mark} {e['name']} = {e['value']}")
                else:
                    lines.append(f"  [unset]  {e['name']}")
        lines.append("")
        lines.append("Use /env get <key> for unmasked values.")
        return "\n".join(lines)

    async def _cmd_env_get(self, bound: BoundArgs) -> str | CommandOutput:
        key = str(bound.get("key") or "")
        try:
            data = await self.api.get_env_var(key, user_id=self.user_id)
        except httpx.HTTPStatusError as e:
            if e.response is not None and e.response.status_code == 404:
                return command_error(f"Unknown variable '{key}'.")
            raise
        val = data.get("value")
        # /env speaks env-var names: label with the canonical spelling the
        # user would put in a file, not the internal field name.
        label = data.get("env_var") or data.get("name", key)
        if val:
            return f"{label} = {val}"
        return f"{label} is not set."

    async def _cmd_env_set(self, bound: BoundArgs) -> str | CommandOutput:
        from ..api.schemas.settings import resolve_settings_field_name

        # /env speaks env-var names (NYMERIA_PUBLIC_URL), the update model
        # speaks field names (nymeria_public_url); the shared resolver maps
        # the divergent spellings (the S3 family) and case-folds the rest.
        # Without it an uppercase spelling of a real setting silently missed
        # the model. Mirror telegram: env set uses update_settings; the
        # applier rejects unknown keys with a 400 the dispatcher renders.
        key = resolve_settings_field_name(str(bound.get("key") or ""))
        blocked = self._agent_settings_write_block(key)
        if blocked is not None:
            return blocked
        parsed = coerce_value(str(bound.get("value") or ""))
        old_value = self._current_setting_value(key)
        result = await self.api.update_settings(user_id=self.user_id, **{key: parsed})
        return self._render_settings_write(key, parsed, result, old_value)

    # ── Tools ─────────────────────────────────────────────────────────────

    async def _resolve_tool_names(self, name: str):
        name_key = name.lower().strip().replace("-", "_")
        cat_data = await self.api.get_tool_categories()
        categories = cat_data.get("categories", {})
        if name_key in categories:
            return (categories[name_key], True, name_key, None)
        data = await self.api.get_default_tools(self.user_id)
        available = data.get("available_tools", [])
        all_names = {t["name"] for t in available}
        if name_key in all_names:
            return ([name_key], False, None, None)
        cat_list = ", ".join(sorted(categories))
        return ([], False, None, f"Unknown tool or category '{name}'. Categories: {cat_list}")

    async def _cmd_tools(self, bound: BoundArgs) -> str | CommandOutput:
        # Bare "/tools" is the enabled readout, the same view bare
        # "/tools list" gives. Every verb is a registered child routed before
        # this handler, so the root takes zero arguments and a typo is
        # answered by the dispatcher with did-you-mean plus the
        # valid-subcommand list.
        return await self._tools_enabled_markdown()

    async def _cmd_tools_list(self, bound: BoundArgs) -> str | CommandOutput:
        """One listing verb over the four views the family used to register.

        ``enabled``, ``optional`` and ``core`` are the named views; anything
        else is read as a tool category (a live set from the tools API, which
        is why the filter declares no choices). Backlog #131 folded the four
        commands into this one; the old paths survive as whole-path aliases,
        and `core`/`optional` degrade to this default until an alias can
        inject a value.
        """
        filter_val = str(bound.get("filter") or "enabled").strip().lower()
        if filter_val == "core":
            return await self._tools_core_markdown()
        if filter_val == "optional":
            return await self._tools_optional_markdown()
        if filter_val in ("enabled", ""):
            return await self._tools_enabled_markdown()
        return await self._tools_category_markdown(str(bound.get("filter") or ""))

    async def _tools_core_markdown(self) -> str:
        data = await self.api.get_default_tools(self.user_id)
        default_names = set(data.get("default_tools", []))
        available = data.get("available_tools", [])
        lines = [f"Core Tools: {len(default_names)} tools"]
        for t in available:
            if t.get("name") in default_names:
                desc = (t.get("description") or "").split("\n")[0][:60]
                if desc:
                    lines.append(f"  {t['name']}: {desc}")
                else:
                    lines.append(f"  {t['name']}")
        return "\n".join(lines)

    async def _tools_optional_markdown(self) -> str | CommandOutput:
        thread_error = self._require_thread()
        if thread_error:
            return thread_error
        data = await self.api.get_default_tools(self.user_id)
        default_names = set(data.get("default_tools", []))
        available = data.get("available_tools", [])
        tc = await self.api.get_thread_config(self.thread_id)
        thread_extras = set(tc.get("enabled_tools", [])) if tc else set()

        cats: dict[str, list] = {}
        for t in available:
            if t.get("name") not in default_names:
                cat = t.get("category", "other")
                cats.setdefault(cat, []).append(t)

        total = sum(len(v) for v in cats.values())
        lines = [f"Optional Tools: {total} tools in {len(cats)} categories"]
        for cat_name in sorted(cats):
            entries = cats[cat_name]
            active = sum(1 for t in entries if t["name"] in thread_extras)
            names = ", ".join(t["name"] for t in entries)
            tag = f" ({active} enabled on this thread)" if active else ""
            lines.append("")
            lines.append(f"{cat_name} ({len(entries)}){tag}")
            lines.append(f"  {names}")
        return "\n".join(lines)

    async def _tools_enabled_markdown(self) -> str | CommandOutput:
        thread_error = self._require_thread()
        if thread_error:
            return thread_error
        data = await self.api.get_default_tools(self.user_id)
        data = _dict_result(data)
        default_names = _string_set_result(data.get("default_tools"))
        available = _dict_list_result(data.get("available_tools"))
        tc = await self.api.get_thread_config(self.thread_id)
        tc = _optional_dict_result(tc)
        thread_extras: set[str] = _string_set_result(tc.get("enabled_tools")) if tc else set()
        thread_disabled: set[str] = _string_set_result(tc.get("disabled_tools")) if tc else set()
        live_temp = live_temporary_tools(tc)
        all_enabled = (default_names | thread_extras | live_temp) - thread_disabled

        lines = [f"Enabled Tools on this thread: {len(all_enabled)} active"]
        core_active: list[str] = sorted(n for n in all_enabled if n in default_names)
        lines.append("")
        lines.append(f"Core ({len(core_active)}):")
        lines.append(f"  {', '.join(core_active) if core_active else 'none'}")

        disabled_core: list[str] = sorted(thread_disabled & default_names)
        if disabled_core:
            lines.append("")
            lines.append(f"Core disabled on this thread ({len(disabled_core)}):")
            lines.append(f"  {', '.join(disabled_core)}")

        optional_active: list[str] = sorted(n for n in all_enabled if n not in default_names)
        if optional_active:
            avail_by_name = {t["name"]: t for t in available}
            lines.append("")
            lines.append(f"Optional enabled ({len(optional_active)}):")
            for name in optional_active:
                t = avail_by_name.get(name, {})
                desc = (t.get("description") or "").split("\n")[0][:60]
                # Flag TTL'd tools (e.g. Skill Kit required_tools) so they're
                # not mistaken for permanent enables.
                suffix = " [temporary/TTL]" if name in live_temp else ""
                if desc:
                    lines.append(f"  {name}: {desc}{suffix}")
                else:
                    lines.append(f"  {name}{suffix}")
        else:
            lines.append("")
            lines.append("Optional enabled: none")
        return "\n".join(lines)

    async def _tools_category_markdown(self, cat_name: str) -> str | CommandOutput:
        thread_error = self._require_thread()
        if thread_error:
            return thread_error
        data = await self.api.get_default_tools(self.user_id)
        default_names = set(data.get("default_tools", []))
        available = data.get("available_tools", [])
        tc = await self.api.get_thread_config(self.thread_id)
        thread_extras = set(tc.get("enabled_tools", [])) if tc else set()
        thread_disabled = set(tc.get("disabled_tools", [])) if tc else set()
        all_enabled = (default_names | thread_extras) - thread_disabled

        cat_key = cat_name.lower().strip().replace("-", "_")
        cats: dict[str, list] = {}
        for t in available:
            cat = t.get("category", "other")
            cats.setdefault(cat, []).append(t)
        if cat_key not in cats:
            return command_error(
                f"Unknown category '{cat_name}'. "
                f"Available: {', '.join(sorted(cats))}"
            )

        entries = cats[cat_key]
        lines = [f"Tools in category '{cat_key}': {len(entries)} tools"]
        for t in entries:
            tool_name = t["name"]
            enabled = tool_name in all_enabled
            is_default = tool_name in default_names
            mark = "[on] " if enabled else "[off]"
            tag = " (core)" if is_default else ""
            desc = (t.get("description") or "").split("\n")[0][:60]
            if desc:
                lines.append(f"  {mark} {tool_name}{tag}: {desc}")
            else:
                lines.append(f"  {mark} {tool_name}{tag}")
        return "\n".join(lines)

    async def _cmd_tools_enable(self, bound: BoundArgs) -> str | CommandOutput:
        thread_error = self._require_thread()
        if thread_error:
            return thread_error
        name = str(bound.get("name") or "")
        tool_names, is_category, cat_name, error = await self._resolve_tool_names(name)
        if error:
            return command_error(error)
        tc = await self.api.get_thread_config(self.thread_id)
        current_enabled = set(tc.get("enabled_tools", [])) if tc else set()
        current_disabled = set(tc.get("disabled_tools", [])) if tc else set()
        new_enabled = current_enabled | set(tool_names)
        new_disabled = current_disabled - set(tool_names)
        await self.api.update_thread_config(
            self.thread_id,
            user_id=self.user_id,
            enabled_tools=sorted(new_enabled),
            disabled_tools=sorted(new_disabled),
        )
        if is_category:
            return command_success(f"Enabled category '{cat_name}' ({len(tool_names)} tools).")
        return command_success(f"Enabled tool '{tool_names[0]}'.")

    async def _cmd_tools_disable(self, bound: BoundArgs) -> str | CommandOutput:
        thread_error = self._require_thread()
        if thread_error:
            return thread_error
        name = str(bound.get("name") or "")
        tool_names, is_category, cat_name, error = await self._resolve_tool_names(name)
        if error:
            return command_error(error)
        tc = await self.api.get_thread_config(self.thread_id)
        current_enabled = set(tc.get("enabled_tools", [])) if tc else set()
        current_disabled = set(tc.get("disabled_tools", [])) if tc else set()
        new_enabled = current_enabled - set(tool_names)
        new_disabled = current_disabled | set(tool_names)
        await self.api.update_thread_config(
            self.thread_id,
            user_id=self.user_id,
            enabled_tools=sorted(new_enabled),
            disabled_tools=sorted(new_disabled),
        )
        if is_category:
            return command_success(f"Disabled category '{cat_name}' ({len(tool_names)} tools).")
        return command_success(f"Disabled tool '{tool_names[0]}'.")

    # ── Memory ────────────────────────────────────────────────────────────

    async def _cmd_memory(self, bound: BoundArgs) -> str:
        # Bare "/memory" lists; every verb is a registered child routed before
        # this handler, so the root takes zero arguments and a typo is
        # answered by the dispatcher with did-you-mean plus the
        # valid-subcommand list.
        return await self._cmd_memory_list(BoundArgs())

    async def _cmd_memory_list(self, bound: BoundArgs) -> str:
        memories = await self.api.list_memories(self.user_id)
        if not memories:
            return "No memories saved yet."
        lines = [f"Memories: {len(memories)} stored"]
        for mem in memories[:25]:
            value = mem.get("value", "")
            preview = (value[:200] + "...") if len(value) > 200 else value
            lines.append(f"  {mem.get('key', '?')}: {preview}")
        if len(memories) > 25:
            lines.append(f"(showing 25 of {len(memories)})")
        return "\n".join(lines)

    async def _cmd_memory_save(self, bound: BoundArgs) -> str | CommandOutput:
        key = str(bound.get("key") or "")
        value = str(bound.get("value") or "")
        await self.api.save_memory(self.user_id, key, value)
        return command_success(f"Saved memory '{key}'.")

    async def _cmd_memory_delete(self, bound: BoundArgs) -> str | CommandOutput:
        key = str(bound.get("key") or "")
        try:
            await self.api.forget_memory(self.user_id, key)
        except httpx.HTTPStatusError as e:
            if e.response is not None and e.response.status_code == 404:
                return command_error(f"No memory found with key '{key}'.")
            raise
        return command_success(f"Forgot memory '{key}'.")

    async def _cmd_memory_search(self, bound: BoundArgs) -> str:
        query = str(bound.get("query") or "")
        results = await self.api.search_memories(self.user_id, query)
        if not results:
            return f"No memories matching '{query}'."
        lines = [f"Memory search for '{query}': {len(results)} results"]
        for mem in results[:25]:
            value = mem.get("value", "")
            preview = (value[:200] + "...") if len(value) > 200 else value
            lines.append(f"  {mem.get('key', '?')}: {preview}")
        # The header counts every match, so a silent cut reads as the whole
        # set. Matches the sibling listing above and every other capped
        # listing in this file.
        if len(results) > 25:
            lines.append(f"(showing 25 of {len(results)})")
        return "\n".join(lines)

    async def _cmd_memory_limit(self, bound: BoundArgs) -> str | CommandOutput:
        from .memory_limits import (
            MAX_MEMORY_CHAR_LIMIT,
            get_effective_thread_memory_char_limit,
            get_global_memory_char_limit,
            profile_memory_text_from_records,
        )
        from ..tools.thread_notes import read_notepad

        def parse_limit(raw: str) -> int | None:
            try:
                value = int(raw)
            except (TypeError, ValueError):
                return None
            if value < 1 or value > MAX_MEMORY_CHAR_LIMIT:
                return None
            return value

        agent = self._agent()
        settings = getattr(agent, "settings", None) if agent is not None else None
        if settings is None:
            try:
                settings = await self.api.get_settings(user_id=self.user_id)
            except TypeError:
                settings = await self.api.get_settings()
            except Exception:  # noqa: BLE001
                settings = get_settings()

        # Value first, scope trailing (backlog #131 wave B). The binder pops a
        # trailing `global`/`thread` word BEFORE assigning positionals, so the
        # scope can never steal the value; with neither present this is the
        # both-scopes readout.
        scope = bound.get("scope")
        value = bound.get("value")

        if scope is None and value is None:
            memories = await self.api.list_memories(self.user_id)
            global_limit = get_global_memory_char_limit(settings)
            lines = [
                "Memory Limits",
                f"  global: {len(profile_memory_text_from_records(memories))} / {global_limit} chars",
            ]
            if self.thread_id:
                manager = getattr(agent, "thread_config_manager", None) if agent is not None else None
                effective_limit = get_effective_thread_memory_char_limit(
                    self.thread_id,
                    settings=settings,
                    thread_config_manager=manager,
                )
                try:
                    thread_cfg = await self.api.get_thread_config(self.thread_id)
                except Exception:  # noqa: BLE001
                    thread_cfg = {}
                override = (thread_cfg or {}).get("memory_char_limit")
                source = f"override {override}" if override is not None else "inherits global"
                notepad = read_notepad(self.thread_id) or ""
                lines.append(
                    f"  thread: {len(notepad)} / {effective_limit} chars ({source})"
                )
            return "\n".join(lines)

        if scope == "global":
            if value is None:
                return self._usage_error(
                    "memory limit", hint="`global` takes a character count."
                )
            limit = parse_limit(value)
            if limit is None:
                return command_error(f"Limit must be an integer from 1 to {MAX_MEMORY_CHAR_LIMIT}.")
            result = await self.api.update_settings(
                user_id=self.user_id,
                memory_char_limit=limit,
            )
            msg = f"Global memory character limit set to {limit}."
            if result.get("restart_required"):
                msg += " (restart required to take effect)"
            return command_success(msg)

        # Thread is the default scope and the only other one, so this arm
        # serves both the explicit `thread` token and a bare value.
        thread_error = self._require_thread()
        if thread_error:
            return thread_error
        if value is None:
            return self._usage_error(
                "memory limit",
                hint="`thread` takes a character count, or `inherit` to follow the global limit.",
            )
        # `global` was a third synonym for `inherit` here until wave B; it
        # names the SCOPE now, so it can no longer name a value.
        if value.lower() in ("inherit", "default"):
            await self.api.update_thread_config(
                self.thread_id,
                clear_memory_char_limit=True,
                user_id=self.user_id,
            )
            return command_success("This thread now inherits the global memory character limit.")
        limit = parse_limit(value)
        if limit is None:
            return command_error(f"Limit must be an integer from 1 to {MAX_MEMORY_CHAR_LIMIT}.")
        await self.api.update_thread_config(
            self.thread_id,
            memory_char_limit=limit,
            user_id=self.user_id,
        )
        return command_success(f"This thread's memory character limit set to {limit}.")

    async def _cmd_sequential_tools(self, bound: BoundArgs) -> str | CommandOutput:
        """Show / set sequential (ordered, one-at-a-time) tool execution.

        No args shows status; `on`/`off`/`inherit` set this thread's override;
        a trailing `global` writes the global default instead. Deterministic
        counterpart to the run_tools_in_order control tool (precedence:
        control_tool OR thread OR global).

        Thread is the default scope with no fallback to global (backlog #131
        wave B): this is an operator setting, and inferring "global" from the
        absence of a thread would let `/sequential-tools on` change every
        conversation's behavior from a threadless surface.
        """
        agent = self._agent()
        settings = getattr(agent, "settings", None) if agent is not None else None
        if settings is None:
            try:
                settings = await self.api.get_settings(user_id=self.user_id)
            except TypeError:
                settings = await self.api.get_settings()
            except Exception:  # noqa: BLE001
                settings = get_settings()

        def global_flag() -> bool:
            if isinstance(settings, dict):
                return bool(settings.get("sequential_tool_execution", False))
            return bool(getattr(settings, "sequential_tool_execution", False))

        def fmt(value: bool) -> str:
            return "on" if value else "off"

        mode = str(bound.get("mode") or "").lower()
        scope = str(bound.get("scope") or "")

        if not mode:
            if scope:
                # A scope with nothing to write; the status readout below is
                # scope-wide already, so name the missing value (the /think
                # precedent).
                return self._usage_error(
                    "sequential-tools",
                    hint="Name `on` or `off` to write that scope.",
                )
            g = global_flag()
            lines = ["Sequential tool execution", f"  global: {fmt(g)}"]
            if self.thread_id:
                try:
                    thread_cfg = await self.api.get_thread_config(self.thread_id)
                except Exception:  # noqa: BLE001
                    thread_cfg = {}
                override = (thread_cfg or {}).get("sequential_tool_execution")
                if override is None:
                    lines.append(f"  thread: {fmt(g)} (inherits global)")
                else:
                    lines.append(f"  thread: {fmt(bool(override))} (override)")
            return "\n".join(lines)

        if scope == "global":
            # There is no global "inherit": the global value IS what a thread
            # inherits, so `inherit global` names nothing to write.
            if mode not in ("on", "off"):
                return self._usage_error(
                    "sequential-tools",
                    hint="The `global` scope takes `on` or `off`.",
                )
            enabled = mode == "on"
            result = await self.api.update_settings(
                user_id=self.user_id,
                sequential_tool_execution=enabled,
            )
            msg = f"Global sequential tool execution turned {fmt(enabled)}."
            if isinstance(result, dict) and result.get("restart_required"):
                msg += " (restart required to take effect)"
            return command_success(msg)

        if mode in ("inherit", "default"):
            thread_error = self._require_thread()
            if thread_error:
                return thread_error
            await self.api.update_thread_config(
                self.thread_id,
                clear_sequential_tool_execution=True,
                user_id=self.user_id,
            )
            return command_success(
                "This thread now inherits the global sequential tool execution "
                "setting."
            )

        if mode in ("on", "off"):
            thread_error = self._require_thread()
            if thread_error:
                return thread_error
            enabled = mode == "on"
            await self.api.update_thread_config(
                self.thread_id,
                sequential_tool_execution=enabled,
                user_id=self.user_id,
            )
            label = "sequential" if enabled else "concurrent"
            return command_success(
                f"This thread's tool execution set to {label} "
                f"(override {fmt(enabled)})."
            )

        # Defensive tail: the declared choices cover every arm above, so the
        # binder rejects an unknown mode before this handler runs.
        return self._usage_error("sequential-tools")

    # ── TODOs ─────────────────────────────────────────────────────────────

    async def _cmd_todos(self, bound: BoundArgs) -> str | CommandOutput:
        # Bare "/todos" lists; every verb is a registered child routed before
        # this handler, so the root takes zero arguments and a typo is
        # answered by the dispatcher with did-you-mean plus the
        # valid-subcommand list. "/tasks" is a whole-path alias of
        # `todos list`, so it arrives with the filter it was given.
        return await self._cmd_todos_list(BoundArgs())

    def _resolve_todo_prefix(
        self, items: list[dict], token: str
    ) -> dict | CommandOutput:
        """Resolve a TODO by exact id or unique id prefix.

        An ambiguous prefix errors listing the candidates (ported from the
        CLI-local family, #143): the old first-match pick could silently
        act on the wrong TODO. An empty token is refused for the same
        reason: ``startswith("")`` matches everything, so a store with one
        TODO would "resolve" it.
        """
        if not token:
            return command_error("A TODO id (or unique id prefix) is required.")
        matches = [i for i in items if i.get("id", "") == token]
        if not matches:
            matches = [i for i in items if i.get("id", "").startswith(token)]
        if not matches:
            return command_error(f"No TODO found matching '{token}'.")
        if len(matches) > 1:
            preview = "; ".join(
                f"{i.get('id', '')[:8]} {i.get('task', '')[:40]}"
                for i in matches[:5]
            )
            return command_error(
                f"Ambiguous TODO id '{token}' matches {len(matches)}: {preview}"
            )
        return matches[0]

    async def _cmd_todos_list(self, bound: BoundArgs) -> str | CommandOutput:
        filter_val = str(bound.get("filter") or "active").lower()
        thread_id, thread_error = self._resolve_thread_token(bound.get("thread"))
        if thread_error is not None:
            return thread_error
        # filter_status="all": both clients default to ACTIVE-only, which
        # would make the done/all branches below filter an already-truncated
        # list (the pre-#143 bug this handler shipped with; the retired
        # CLI-local family passed the filter through and was the only
        # surface that answered `done` truthfully).
        items = await self.api.list_todos(
            self.user_id, filter_status="all", thread_id=thread_id
        )
        if filter_val == "active":
            items = [i for i in items if i.get("status") != "done"]
        elif filter_val != "all":
            items = [i for i in items if i.get("status") == filter_val]
        if not items:
            return f"No {filter_val} TODOs."
        lines = [f"TODOs ({filter_val}): {len(items)} items"]
        for item in items[:25]:
            st = item.get("status", "pending")
            task = item.get("task", "")[:80]
            todo_id = item.get("id", "")[:8]
            parts = [f"id={todo_id}", f"status={st}"]
            if item.get("scheduled_for"):
                # Local, zone named: the schedule was PARSED in the user's
                # timezone, so slicing the stored UTC string showed a different
                # wall clock than they typed (see format_user_time_compact).
                parts.append(f"fires={format_user_time_compact(item['scheduled_for'])}")
            if item.get("recurrence"):
                parts.append(f"repeat={item['recurrence']}")
            if item.get("schedule_paused_at"):
                parts.append(
                    f"paused ({item.get('consecutive_failures', 0)} failures;"
                    f" reschedule to resume)"
                )
            elif item.get("consecutive_failures"):
                parts.append(f"failures={item['consecutive_failures']}")
            lines.append(f"- {task}")
            lines.append("  " + " | ".join(parts))
        if len(items) > 25:
            lines.append(f"(showing 25 of {len(items)})")
        return "\n".join(lines)

    async def _cmd_todos_add(self, bound: BoundArgs) -> str | CommandOutput:
        from .todo_constants import validate_recurrence

        text = " ".join(bound.get("task") or []).strip()
        schedule = str(bound.get("schedule") or "").strip()
        notes = str(bound.get("notes") or "").strip() or None
        repeat = str(bound.get("repeat") or "").strip() or None
        # Legacy pipe grammar (task | schedule | repeat | notes) re-collects
        # from the joined task tokens, but ONLY when no option flag was
        # given: with a flag present the task passes verbatim, so a task
        # containing a literal `|` is never silently truncated (review
        # catch, #143). The two grammars do not mix.
        if "|" in text and not (schedule or notes or repeat):
            parts = [p.strip() for p in text.split("|")]
            text = parts[0]
            if len(parts) > 1 and parts[1]:
                schedule = parts[1]
            if len(parts) > 2 and parts[2]:
                repeat = parts[2]
            if len(parts) > 3 and parts[3]:
                notes = parts[3]
        if not text:
            return self._usage_error("todos add")
        if repeat:
            try:
                repeat = validate_recurrence(repeat)
            except ValueError as exc:
                return command_error(str(exc))
        thread_id, thread_error = self._resolve_thread_token(
            bound.get("thread"), default_current=True
        )
        if thread_error is not None:
            return thread_error
        # `none`/`off`/`clear` creates a plain checklist item that never
        # fires; an absent schedule keeps the historical 1d default.
        unscheduled = schedule.casefold() in {"none", "off", "clear"}
        result = await self.api.add_todo(
            user_id=self.user_id,
            task=text,
            scheduled_for=None if unscheduled else (schedule or "1d"),
            notes=notes,
            recurrence=repeat,
            thread_id=thread_id,
        )
        todo_id = result.get("id", "")[:8]
        scheduled = result.get("scheduled_for", "")
        out = [f"Created TODO {todo_id}: {text}"]
        if scheduled:
            out.append(f"Fires: {format_user_time_compact(scheduled)}")
        if repeat:
            out.append(f"Repeats: {repeat}")
        return command_success("\n".join(out))

    # Patch keys -> the words the edit confirmation speaks (raw field names
    # like clear_recurrence are internals, not user copy).
    _TODO_PATCH_WORDS = {
        "task": "task",
        "status": "status",
        "notes": "notes",
        "scheduled_for": "schedule",
        "clear_schedule": "schedule",
        "thread_id": "thread",
        "recurrence": "repeat",
        "clear_recurrence": "repeat",
    }

    async def _cmd_todos_edit(self, bound: BoundArgs) -> str | CommandOutput:
        from .todo_constants import validate_recurrence

        items = await self.api.list_todos(self.user_id, filter_status="all")
        match = self._resolve_todo_prefix(items, str(bound.get("todo_id") or ""))
        if isinstance(match, CommandOutput):
            return match
        patch: dict[str, Any] = {}
        new_task = " ".join(bound.get("task") or []).strip()
        if new_task:
            patch["task"] = new_task
        if bound.get("status"):
            patch["status"] = str(bound.get("status"))
        if "notes" in bound.values:
            patch["notes"] = str(bound.get("notes"))
        # The clear words the sibling verbs take (`/todos schedule <id>
        # clear`) work on the flags too, one spelling family-wide.
        schedule = str(bound.get("schedule") or "").strip()
        if bound.get("clear_schedule") or schedule.casefold() in {
            "clear",
            "none",
            "off",
        }:
            patch["clear_schedule"] = True
        elif schedule:
            patch["scheduled_for"] = schedule
        repeat = str(bound.get("repeat") or "").strip()
        if bound.get("clear_repeat") or repeat.casefold() in {"clear", "none", "off"}:
            patch["clear_recurrence"] = True
        elif repeat:
            try:
                patch["recurrence"] = validate_recurrence(repeat)
            except ValueError as exc:
                return command_error(str(exc))
        if "thread" in bound.values:
            thread_id, thread_error = self._resolve_thread_token(bound.get("thread"))
            if thread_error is not None:
                return thread_error
            patch["thread_id"] = thread_id or f"default-{self.user_id}"
        if not patch:
            return self._usage_error(
                "todos edit", hint="No TODO updates were provided."
            )
        updated = await self.api.update_todo(self.user_id, match["id"], **patch)
        changed = ", ".join(
            sorted({self._TODO_PATCH_WORDS.get(key, key) for key in patch})
        )
        return command_success(
            f"Updated TODO {updated.get('id', '')[:8]} ({changed}): "
            f"{updated.get('task', '')[:80]}"
        )

    async def _cmd_todos_schedule(self, bound: BoundArgs) -> str | CommandOutput:
        items = await self.api.list_todos(self.user_id, filter_status="all")
        match = self._resolve_todo_prefix(items, str(bound.get("todo_id") or ""))
        if isinstance(match, CommandOutput):
            return match
        when = " ".join(bound.get("when") or []).strip()
        task = str(match.get("task", ""))[:60]
        if when.casefold() in {"clear", "none", "off"}:
            updated = await self.api.update_todo(
                self.user_id, match["id"], clear_schedule=True
            )
            return command_success(
                f"Schedule cleared: {updated.get('id', '')[:8]} {task}"
            )
        updated = await self.api.update_todo(
            self.user_id, match["id"], scheduled_for=when
        )
        fires = format_user_time_compact(updated.get("scheduled_for"))
        return command_success(
            f"Schedule updated: {updated.get('id', '')[:8]} {task} fires {fires}"
        )

    async def _cmd_todos_repeat(self, bound: BoundArgs) -> str | CommandOutput:
        from .todo_constants import validate_recurrence

        items = await self.api.list_todos(self.user_id, filter_status="all")
        match = self._resolve_todo_prefix(items, str(bound.get("todo_id") or ""))
        if isinstance(match, CommandOutput):
            return match
        interval = str(bound.get("interval") or "").strip()
        task = str(match.get("task", ""))[:60]
        if interval.casefold() in {"clear", "none", "off"}:
            updated = await self.api.update_todo(
                self.user_id, match["id"], clear_recurrence=True
            )
            return command_success(
                f"Recurrence cleared: {updated.get('id', '')[:8]} {task}"
            )
        try:
            canonical = validate_recurrence(interval)
        except ValueError as exc:
            return command_error(str(exc))
        updated = await self.api.update_todo(
            self.user_id, match["id"], recurrence=canonical
        )
        return command_success(
            f"Recurrence updated: {updated.get('id', '')[:8]} {task} "
            f"repeats {updated.get('recurrence') or canonical}"
        )

    async def _cmd_todos_complete(self, bound: BoundArgs) -> str | CommandOutput:
        items = await self.api.list_todos(self.user_id, filter_status="all")
        match = self._resolve_todo_prefix(items, str(bound.get("todo_id") or ""))
        if isinstance(match, CommandOutput):
            return match
        result = await self.api.complete_todo(self.user_id, match["id"])
        task = match.get("task", "")
        recurrence = result.get("recurrence")
        if recurrence and result.get("status") == "pending":
            next_fire = format_user_time_compact(result.get("scheduled_for"))
            return command_success(
                f"Completed '{task}'. Rescheduled ({recurrence}): "
                f"next fire {next_fire}."
            )
        return command_success(f"Completed '{task}'.")

    async def _cmd_todos_delete(self, bound: BoundArgs) -> str | CommandOutput:
        items = await self.api.list_todos(self.user_id, filter_status="all")
        match = self._resolve_todo_prefix(items, str(bound.get("todo_id") or ""))
        if isinstance(match, CommandOutput):
            return match
        await self.api.delete_todo(self.user_id, match["id"])
        return command_success(f"Deleted '{match.get('task', '')}'.")

    # ── Notepad ───────────────────────────────────────────────────────────

    async def _cmd_notepad(self, bound: BoundArgs) -> str | CommandOutput:
        # Bare "/notepad" reads; every verb is a registered child routed
        # before this handler, so the root takes zero arguments and a typo is
        # answered by the dispatcher with did-you-mean plus the
        # valid-subcommand list.
        return await self._cmd_notepad_read(BoundArgs())

    async def _cmd_notepad_read(self, bound: BoundArgs) -> str | CommandOutput:
        thread_error = self._require_thread()
        if thread_error:
            return thread_error
        from ..tools.thread_notes import read_notepad
        content = read_notepad(self.thread_id)
        if not content:
            return "Notepad is empty."
        return f"Notepad ({len(content)} chars):\n\n{content}"

    async def _cmd_notepad_write(self, args: list[str], rest: str) -> str | CommandOutput:
        thread_error = self._require_thread()
        if thread_error:
            return thread_error
        raw = rest
        if not raw:
            return command_error("Usage: /notepad write <content>  (or: replace:<content>)")
        from ..tools.thread_notes import write_notepad
        if raw.lower().startswith("replace:"):
            write_mode = "replace"
            content = raw[len("replace:"):].strip()
        elif raw.lower().startswith("append:"):
            # append is already the default; the prefix is advertised in the
            # registered usage, so strip it instead of writing it literally.
            write_mode = "append"
            content = raw[len("append:"):].strip()
        else:
            write_mode = "append"
            content = raw
        result = write_notepad(self.thread_id, content, mode=write_mode)
        # This site PARSES the tool-channel sentinel protocol, which the #132
        # sweep leaves in place: write_notepad is a TOOL whose contract is its
        # prefixed string, so the relay maps that outcome onto a command level
        # rather than forwarding a foreign channel's sentinel to a surface.
        if result.startswith("[Saved]:"):
            return command_success(result.removeprefix("[Saved]:").strip())
        if result.startswith("[Error]:"):
            return command_error(result.removeprefix("[Error]:").strip())
        return result

    async def _cmd_notepad_clear(self, bound: BoundArgs) -> str | CommandOutput:
        thread_error = self._require_thread()
        if thread_error:
            return thread_error
        from ..tools.thread_notes import delete_notepad
        if delete_notepad(self.thread_id):
            return command_success("Notepad cleared.")
        return "Notepad was already empty."

    # ── Thread lifecycle ──────────────────────────────────────────────────

    async def _cmd_stop(self, args: list[str], rest: str) -> str | CommandOutput:
        from .pending_prompt_queue import restored_prompts_notice

        thread_error = self._require_thread()
        if thread_error:
            return thread_error
        result = await self.api.stop_thread(self.thread_id)
        if result.get("status") == "stopping":
            holder = result.get("holder") or "current turn"
            held = result.get("held_seconds", 0)
            message = (
                f"Stop requested. {holder} has been running for "
                f"{held:.0f}s; will halt at the next iteration boundary."
            )
            notice = restored_prompts_notice(result.get("restored_prompts") or [])
            if notice:
                message = f"{message}\n\n{notice}"
            return command_success(message)
        return "Thread is idle; nothing to stop."

    async def _cmd_clear(self, args: list[str], rest: str) -> str | CommandOutput:
        thread_error = self._require_thread()
        if thread_error:
            return thread_error
        await self.api.clear_thread(self.thread_id)
        return command_success("Conversation history cleared. Notepad and tool config preserved.")

    async def _cmd_prune(self, bound: BoundArgs) -> str | CommandOutput:
        thread_error = self._require_thread()
        if thread_error:
            return thread_error
        mode = str(bound.get("mode") or "full")
        result = await self.api.prune_thread(self.thread_id, mode=mode)
        if not result.get("success"):
            reason = result.get("reason", "unknown error")
            return command_error(f"Could not prune: {reason}")
        pruned = int(result.get("pruned_count", 0))
        saved = int(result.get("chars_saved", 0))
        already = int(result.get("skipped_already_pruned", 0))
        too_short = int(result.get("skipped_too_short", 0))
        if pruned == 0:
            if already or too_short:
                return (
                    f"Nothing to prune. Already-pruned: {already}, "
                    f"too small to compress: {too_short}."
                )
            return "Nothing to prune - no tool results found in this thread."
        word = "result" if pruned == 1 else "results"
        return command_success(
            f"Pruned {pruned} tool {word} ({mode}), "
            f"reclaimed {saved:,} chars."
        )

    async def _cmd_restart_api(self, args: list[str], rest: str) -> str | CommandOutput:
        await self.api.restart_api()
        return command_success("Restarting API server...")
