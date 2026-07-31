"""Custom tool loader and executor.

Loads custom tool definitions from JSON files and converts them to
LangChain @tool functions. Supports HTTP, MCP, Python, and workflow tool
implementations.

HTTP tools make REST API calls with parameter interpolation.
MCP tools communicate with Model Context Protocol servers via JSON-RPC.
Python tools execute configured source code in a child process.
Workflow tools run nym-SDK source through the core/workflows engine
(subprocess + verb RPC), gated on per-revision admin approval.

Based on MCP best practices 2025-2026:
- Environment variable interpolation for secrets
- Structured error handling
- Request timeout and retry logic
"""

import asyncio
import json
import logging
import re
import threading
import time
from pathlib import Path
from typing import TYPE_CHECKING, Any, Dict, List, Literal, Optional, Set, cast

import httpx
from langchain_core.runnables import RunnableConfig
from langchain_core.tools import BaseTool, StructuredTool

from ..config import get_settings
from .credential_vault import UNATTRIBUTED_ACTOR, Actor
from ..tools.definitions.custom_tool_schema import CustomToolDefinition, HTTPToolConfig
from ..tools.metadata import (
    clear_custom_tool_metadata,
    register_custom_tool_metadata,
    unregister_custom_tool_metadata,
)
from .secret_interpolation import (
    interpolate_env_vars_with_names,
    resolve_credential_refs,
)
from .storage_paths import (
    FileFingerprint,
    capture_fingerprint,
    scan_fingerprint_map,
    write_text_atomic,
)
from .time_utils import utc_now

logger = logging.getLogger(__name__)

if TYPE_CHECKING:
    from .mcp_manager import MCPServerManager

# Env-reference interpolation (``${env:VAR}``) lives in ``secret_interpolation``;
# it is imported above. Parameter interpolation (``${param_name}``) is local.
PARAM_PATTERN = re.compile(r"\$\{([a-zA-Z_][a-zA-Z0-9_]*)\}")


class CustomToolLoader:
    """Loads and manages custom tool definitions.

    Watches a directory for JSON tool definition files and converts
    them to LangChain tools that can be registered with the agent.
    """

    # Minimum seconds between external-edit stat scans (hot-load debounce).
    # Class attribute so tests can zero it.
    EXTERNAL_REFRESH_INTERVAL_SECONDS: float = 2.0

    def __init__(self, tools_dir: Optional[Path] = None):
        """Initialize the custom tool loader.

        Args:
            tools_dir: Directory containing tool definition JSON files.
                       Defaults to data/custom_tools/ in project root.
        """
        settings = get_settings()
        self.tools_dir = tools_dir or settings.custom_tools_dir
        self.tools_dir.mkdir(parents=True, exist_ok=True)

        # Guards the caches and the disk-fingerprint bookkeeping below.
        self._lock = threading.RLock()

        # Cache of loaded tool definitions
        self._definitions: Dict[str, CustomToolDefinition] = {}

        # Cache of created LangChain tools
        self._tools: Dict[str, BaseTool] = {}

        # Hot-load bookkeeping (resource-filesystem-layout plan, slice 2).
        # _disk_sigs: filename -> FileFingerprint recorded whenever a
        # definition file is parsed through this loader, so manager-driven
        # writes never register as external edits. _file_ids maps filename ->
        # the definition id it produced (a raw edit may change the id inside
        # the file). _pending_registry_sync collects ids whose BaseTool
        # changed via an external edit; the agent drains it to re-register.
        self._disk_sigs: Dict[str, FileFingerprint] = {}
        self._file_ids: Dict[str, str] = {}
        self._pending_registry_sync: Set[str] = set()
        self._last_freshness_check = 0.0

    @property
    def mcp_manager(self) -> "MCPServerManager":
        """Get the shared MCP server manager."""
        from .mcp_manager import get_mcp_manager

        return get_mcp_manager()

    def load_all(self) -> List[BaseTool]:
        """Load all custom tools from the tools directory.

        Returns:
            List of LangChain tools created from definitions.
        """
        with self._lock:
            self._definitions.clear()
            self._tools.clear()
            self._disk_sigs.clear()
            self._file_ids.clear()
            self._pending_registry_sync.clear()
            clear_custom_tool_metadata()

            tools = []
            for json_file in self.tools_dir.glob("*.json"):
                try:
                    tool = self._load_tool_file(json_file)
                    if tool:
                        tools.append(tool)
                except Exception as e:
                    logger.error(f"Failed to load tool from {json_file}: {e}")

        logger.info(f"Loaded {len(tools)} custom tool(s) from {self.tools_dir}")
        return tools

    def refresh_if_stale(self, force: bool = False) -> List[str]:
        """Pick up external (non-manager) edits to the definition files.

        Stat-scans the tools dir (debounced) and re-parses only files whose
        fingerprint changed, dropping definitions whose files disappeared.
        Returns the affected tool ids; the corresponding registry re-sync is
        the agent's job (see ``drain_registry_sync`` and
        ``agent_tools.sync_external_resource_edits``).
        """
        now = time.monotonic()
        with self._lock:
            if not force and now - self._last_freshness_check < self.EXTERNAL_REFRESH_INTERVAL_SECONDS:
                return []
            self._last_freshness_check = now

        changed_ids: List[str] = []
        with self._lock:
            try:
                entries = [(f.name, f) for f in self.tools_dir.glob("*.json")]
            except OSError:
                return []
            current, stale, removed = scan_fingerprint_map(entries, self._disk_sigs)
            if not stale and not removed:
                return []

            for name in removed:
                self._disk_sigs.pop(name, None)
                tool_id = self._file_ids.pop(name, None)
                if tool_id:
                    self._definitions.pop(tool_id, None)
                    self._tools.pop(tool_id, None)
                    unregister_custom_tool_metadata(tool_id)
                    changed_ids.append(tool_id)
            for name in stale:
                old_id = self._file_ids.pop(name, None)
                if old_id:
                    self._definitions.pop(old_id, None)
                    self._tools.pop(old_id, None)
                    unregister_custom_tool_metadata(old_id)
                    changed_ids.append(old_id)
                self._load_tool_file(self.tools_dir / name)
                new_id = self._file_ids.get(name)
                if new_id and new_id not in changed_ids:
                    changed_ids.append(new_id)

            self._pending_registry_sync.update(changed_ids)
            logger.info(
                "Custom tool store: external edit detected (%d changed, %d removed "
                "file(s)); refreshed ids %s",
                len(stale), len(removed), sorted(changed_ids),
            )

        _mark_tool_search_dirty_safely()
        _log_external_edit_safely(
            "custom_tools",
            f"{len(stale)} changed, {len(removed)} removed file(s); "
            f"tool ids {sorted(changed_ids)}",
        )
        return changed_ids

    def drain_registry_sync(self) -> Set[str]:
        """Return and clear the tool ids awaiting agent registry re-sync."""
        with self._lock:
            pending = self._pending_registry_sync
            self._pending_registry_sync = set()
            return pending

    def get_tool(self, tool_id: str) -> Optional[BaseTool]:
        """Get the cached BaseTool for a definition id (no freshness check)."""
        with self._lock:
            return self._tools.get(tool_id)

    def _load_tool_file(self, file_path: Path) -> Optional[BaseTool]:
        """Load a single tool definition file.

        Args:
            file_path: Path to the JSON definition file.

        Returns:
            LangChain tool or None if loading failed.
        """
        fingerprint = capture_fingerprint(file_path)
        if fingerprint is not None:
            self._disk_sigs[file_path.name] = fingerprint
        # Unreadable stat: the refresh sweep will retry the file.
        try:
            data = json.loads(file_path.read_text(encoding="utf-8"))
            definition = CustomToolDefinition(**data)
        except Exception as e:  # noqa: BLE001 - unparseable file, quarantine it
            from .storage_paths import quarantine_corrupt_file

            quarantine = quarantine_corrupt_file(file_path)
            if quarantine is not None:
                self._disk_sigs.pop(file_path.name, None)
                self._file_ids.pop(file_path.name, None)
            logger.error(
                "Error loading tool from %s: %s (%s)",
                file_path,
                e,
                f"corrupt file preserved at {quarantine}"
                if quarantine
                else "quarantine rename failed; file left in place",
                exc_info=True,
            )
            _log_external_edit_safely(
                "custom_tools",
                f"corrupt file {file_path.name} "
                + (f"quarantined as quarantine/{quarantine.name}" if quarantine
                   else "could not be quarantined"),
            )
            return None
        try:
            self._file_ids[file_path.name] = definition.id

            if not definition.enabled:
                logger.debug(f"Skipping disabled tool: {definition.id}")
                unregister_custom_tool_metadata(definition.id)
                return None

            self._definitions[definition.id] = definition

            # Create the appropriate tool based on implementation type
            if definition.implementation_type == "http":
                tool = self._create_http_tool(definition)
            elif definition.implementation_type == "mcp":
                tool = self._create_mcp_tool(definition)
            elif definition.implementation_type == "python":
                tool = self._create_python_tool(definition)
            elif definition.implementation_type == "workflow":
                tool = self._create_workflow_tool(definition)
            else:
                logger.error(f"Unknown implementation type: {definition.implementation_type}")
                return None

            self._tools[definition.id] = tool
            register_custom_tool_metadata(definition.id, definition.description)
            return tool

        except Exception as e:
            # The file parsed; the tool object could not be built. Keep the
            # file in place (it may be valid for a newer/older codebase) and
            # skip it.
            logger.error(f"Error loading tool from {file_path}: {e}", exc_info=True)
            return None

    def _create_http_tool(self, definition: CustomToolDefinition) -> BaseTool:
        """Create a LangChain tool from an HTTP tool definition.

        Args:
            definition: The custom tool definition.

        Returns:
            A LangChain StructuredTool.
        """
        config = definition.http_config
        assert config is not None

        # `run_config` is populated by LangChain, which finds it by its
        # RunnableConfig ANNOTATION rather than by name
        # (`langchain_core.tools.base._get_runnable_config_param`), so the
        # generated args_schema below stays untouched and the model never sees
        # this parameter.
        #
        # The caller is the right actor here, not the tool's author:
        # CustomToolDefinition carries no owner, so these tools are global, and
        # the vault's owner check is a statement about who is asking.
        async def execute_http(run_config: RunnableConfig, **kwargs: Any) -> str:
            """Execute the HTTP tool with given parameters."""
            return await execute_http_tool(
                config,
                kwargs,
                actor=_caller_actor(run_config),
                target_type="custom_tool",
                target_id=definition.id,
            )

        def execute_http_sync(run_config: RunnableConfig, **kwargs: Any) -> str:
            """Synchronous twin of ``execute_http``."""
            return _sync_execute_http(
                config,
                kwargs,
                actor=_caller_actor(run_config),
                target_type="custom_tool",
                target_id=definition.id,
            )

        return StructuredTool.from_function(
            func=execute_http_sync,
            coroutine=execute_http,
            name=definition.id,
            description=definition.description,
            args_schema=_create_pydantic_schema(definition.id, definition.parameters),
        )

    def _create_mcp_tool(self, definition: CustomToolDefinition) -> BaseTool:
        """Create a LangChain tool from an MCP tool definition.

        Args:
            definition: The custom tool definition.

        Returns:
            A LangChain StructuredTool.
        """
        config = definition.mcp_config
        assert config is not None

        # Capture mcp_manager reference for the closure
        mcp_manager = self.mcp_manager

        async def execute_mcp(**kwargs: Any) -> str:
            """Execute the MCP tool with given parameters."""
            return await mcp_manager.call_tool(config, kwargs)

        return StructuredTool.from_function(
            func=lambda **kwargs: mcp_manager.call_tool_sync(config, kwargs),
            coroutine=execute_mcp,
            name=definition.id,
            description=definition.description,
            args_schema=_create_pydantic_schema(definition.id, definition.parameters),
        )

    def _create_python_tool(self, definition: CustomToolDefinition) -> BaseTool:
        """Create a subprocess-backed LangChain tool from Python source.

        The closures capture only the tool id and re-fetch the definition from
        this loader at call time, so the execution-time approval gate
        (``python_execution_gate``) sees the current source and approval: an
        edited or unapproved record fails closed on the next call with no
        reload (mirrors ``_create_workflow_tool``). This is the sole
        agent-facing execution surface; the admin test surfaces call the
        executor directly and stay admin-gated at authoring.
        """
        assert definition.python_config is not None
        tool_id = definition.id
        loader = self

        def _gated_config():
            """Re-fetch + gate; return (config, None) or (None, error_text)."""
            from .python_custom_tools import python_execution_gate

            defn = loader.get_definition(tool_id)
            if defn is None or defn.python_config is None:
                return None, f"[Error]: python tool {tool_id!r} is no longer available"
            gate_error = python_execution_gate(defn.python_config, defn.parameters)
            if gate_error:
                return None, f"[Error]: approval_required - {gate_error}"
            return defn.python_config, None

        async def execute_python(**kwargs: Any) -> str:
            from .python_custom_tools import execute_python_tool

            cfg, error = _gated_config()
            if error is not None:
                return error
            assert cfg is not None
            return await execute_python_tool(cfg, kwargs, target_id=tool_id)

        def _run_sync(**kwargs: Any) -> str:
            cfg, error = _gated_config()
            if error is not None:
                return error
            assert cfg is not None
            return _sync_execute_python(cfg, kwargs, target_id=tool_id)

        return StructuredTool.from_function(
            func=_run_sync,
            coroutine=execute_python,
            name=definition.id,
            description=definition.description,
            args_schema=_create_pydantic_schema(definition.id, definition.parameters),
        )

    def _create_workflow_tool(self, definition: CustomToolDefinition) -> BaseTool:
        """Create a nym-SDK workflow tool (subprocess + verb RPC engine).

        The coroutine re-reads the definition from this loader at call time
        (the execution-time approval re-gate), so the closure captures only
        the id. The ``config`` parameter's ``RunnableConfig`` annotation makes
        langchain inject the run config (the bash.py pattern), which carries
        ``user_id``/``thread_id``/``workflow_depth``.

        Both entry points are bound (like every other custom-tool type): a
        coroutine-only StructuredTool raises ``NotImplementedError`` from
        ``tool.invoke()`` on the SYNC tool-node path, which is live via
        ``POST /chat/sync`` and the in-process webhook bots. The sync wrapper
        runs the engine under ``asyncio.run`` on the calling worker thread
        (no running loop there); the trigger ``_fire_run_workflow`` path
        proves the engine's per-run RPC listener works under exactly this
        shape.
        """
        from langchain_core.runnables import RunnableConfig

        tool_id = definition.id
        loader = self

        async def execute_workflow_tool(
            config: RunnableConfig = None,  # type: ignore[assignment]
            **kwargs: Any,
        ) -> str:
            from .workflows.tool_runtime import run_workflow_tool

            return await run_workflow_tool(loader, tool_id, kwargs, config)

        def execute_workflow_tool_sync(
            config: RunnableConfig = None,  # type: ignore[assignment]
            **kwargs: Any,
        ) -> str:
            return asyncio.run(execute_workflow_tool(config=config, **kwargs))

        return StructuredTool.from_function(
            func=execute_workflow_tool_sync,
            coroutine=execute_workflow_tool,
            name=definition.id,
            description=definition.description,
            args_schema=_create_pydantic_schema(definition.id, definition.parameters),
        )

    def get_definition(self, tool_id: str) -> Optional[CustomToolDefinition]:
        """Get a tool definition by ID (picking up external file edits).

        Args:
            tool_id: The tool identifier.

        Returns:
            The tool definition or None.
        """
        self.refresh_if_stale()
        with self._lock:
            return self._definitions.get(tool_id)

    def get_all_definitions(self) -> List[CustomToolDefinition]:
        """Get all loaded tool definitions (picking up external file edits).

        Returns:
            List of all tool definitions.
        """
        self.refresh_if_stale()
        with self._lock:
            return list(self._definitions.values())

    def save_definition(self, definition: CustomToolDefinition) -> Path:
        """Save a tool definition to a JSON file.

        Args:
            definition: The tool definition to save.

        Returns:
            Path to the saved file.
        """
        # Update timestamp
        definition.updated_at = utc_now()

        file_path = self.tools_dir / f"{definition.id}.json"
        with self._lock:
            # Atomic: `_load_tool_file` QUARANTINES a file that no longer
            # parses, so a torn write here would move the prior good bytes
            # aside rather than merely fail.
            write_text_atomic(file_path, definition.model_dump_json(indent=2))

            # Reload to update cache (also re-fingerprints the file, so this
            # manager-driven write never registers as an external edit).
            self._definitions.pop(definition.id, None)
            self._tools.pop(definition.id, None)
            unregister_custom_tool_metadata(definition.id)
            self._load_tool_file(file_path)

        _mark_tool_search_dirty_safely()
        logger.info(f"Saved tool definition: {definition.id}")
        return file_path

    def delete_definition(self, tool_id: str) -> bool:
        """Delete a tool definition.

        Args:
            tool_id: The tool identifier.

        Returns:
            True if deleted, False if not found.
        """
        file_path = self.tools_dir / f"{tool_id}.json"

        if not file_path.exists():
            return False

        with self._lock:
            file_path.unlink()

            # Remove from caches
            self._definitions.pop(tool_id, None)
            self._tools.pop(tool_id, None)
            self._disk_sigs.pop(file_path.name, None)
            self._file_ids.pop(file_path.name, None)
            unregister_custom_tool_metadata(tool_id)

        _mark_tool_search_dirty_safely()
        logger.info(f"Deleted tool definition: {tool_id}")
        return True

    def shutdown(self) -> None:
        """Shutdown the loader and cleanup resources."""
        # The MCP connection manager is process-wide and may also be used by
        # managed MCP server tools, so this loader does not own its lifecycle.
        return None


def interpolate_params(template: str, params: Dict[str, Any]) -> str:
    """Replace ${param_name} placeholders with parameter values.

    Args:
        template: String containing parameter placeholders.
        params: Dictionary of parameter values.

    Returns:
        String with placeholders replaced by parameter values.
    """
    def replace_param(match: re.Match) -> str:
        param_name = match.group(1)
        value = params.get(param_name)
        if value is None:
            return match.group(0)  # Keep original if not found
        # JSON encode non-string values for body templates
        if isinstance(value, (dict, list)):
            return json.dumps(value)
        if isinstance(value, bool):
            return "true" if value else "false"
        return str(value)

    return PARAM_PATTERN.sub(replace_param, template)


# An actor that matches no user, so the vault's owner check denies every
# user-owned record while system-owned ones still resolve.
#
# Deliberately NOT a fallback to the usual `get_user_id(config)` helper: that
# defaults to "default", which is the BOOTSTRAP ADMIN, so an unattributed call
# would silently become a privileged one. Failing closed on an unknown caller is
# the whole point of requiring the actor in the first place.
_UNATTRIBUTED_ACTOR = UNATTRIBUTED_ACTOR


def _caller_actor(run_config: Optional[RunnableConfig]) -> Actor:
    """Resolve the invoking user from a run config, failing closed."""
    if not run_config:
        return _UNATTRIBUTED_ACTOR
    configurable = run_config.get("configurable") or {}
    return configurable.get("user_id") or _UNATTRIBUTED_ACTOR


async def execute_http_tool(
    config: HTTPToolConfig,
    params: Dict[str, Any],
    *,
    actor: Actor,
    target_type: str = "custom_http_tool",
    target_id: Optional[str] = None,
) -> str:
    """Execute an HTTP tool with the given parameters.

    Async wrapper that offloads the synchronous core to a worker thread, mirroring
    the Python-tool path (``execute_python_tool`` -> ``_sync_execute_python_tool``).

    Args:
        config: HTTP tool configuration.
        params: Parameter values for interpolation.

    Returns:
        Response content as a string.
    """
    return await asyncio.to_thread(
        _sync_http_request,
        config,
        params,
        actor=actor,
        target_type=target_type,
        target_id=target_id,
    )


def _sync_http_request(
    config: HTTPToolConfig,
    params: Dict[str, Any],
    *,
    actor: Actor,
    target_type: str = "custom_http_tool",
    target_id: Optional[str] = None,
) -> str:
    """Synchronous core for HTTP tool execution.

    Does parameter/env/credential interpolation and the (synchronous) HTTP request.
    Both the async wrapper (``execute_http_tool`` via ``asyncio.to_thread``) and the
    sync tool entry point (``_sync_execute_http``) call this, so neither needs an
    event loop.

    Args:
        config: HTTP tool configuration.
        params: Parameter values for interpolation.

    Returns:
        Response content as a string.
    """
    try:
        used_env_secrets: set[str] = set()
        used_credentials: set[str] = set()
        redact_values: set[str] = set()

        def resolve_credentials(value: str) -> str:
            return resolve_credential_refs(
                value,
                actor=actor,
                target_type=target_type,
                target_id=target_id,
                used_credentials=used_credentials,
                redact_values=redact_values,
            )

        # Interpolate URL
        url = interpolate_params(config.url, params)
        url, used = interpolate_env_vars_with_names(url)
        used_env_secrets.update(used)
        url = resolve_credentials(url)

        # Interpolate headers
        headers = {}
        for key, value in config.headers.items():
            interpolated, used = interpolate_env_vars_with_names(interpolate_params(value, params))
            headers[key] = resolve_credentials(interpolated)
            used_env_secrets.update(used)

        # Interpolate query params
        query_params = {}
        for key, value in config.query_params.items():
            interpolated, used = interpolate_env_vars_with_names(interpolate_params(value, params))
            query_params[key] = resolve_credentials(interpolated)
            used_env_secrets.update(used)

        # Interpolate body
        body = None
        if config.body_template:
            body_str = interpolate_params(config.body_template, params)
            body_str, used = interpolate_env_vars_with_names(body_str)
            used_env_secrets.update(used)
            body_str = resolve_credentials(body_str)
            try:
                body = json.loads(body_str)
            except json.JSONDecodeError:
                body = body_str

        from ..tools.http_api import _http_request_impl

        result = _http_request_impl(
            method=config.method,
            url=url,
            headers=headers,
            query=query_params if query_params else None,
            body=body,
            timeout_seconds=config.timeout_seconds,
            follow_redirects=True,
            response_format=config.response_format,
            max_response_chars=200_000,
            audit_tool_name="custom_http_tool",
            used_env_secrets=sorted(used_env_secrets),
            used_credentials=sorted(used_credentials),
            redact_values=sorted(redact_values),
        )

        if not result.get("ok"):
            error = result.get("error") or {}
            message = error.get("message") or str(error) or "Request failed"
            if result.get("response", {}).get("status_code"):
                return (
                    f"[Error]: HTTP {result['response']['status_code']} - "
                    f"{result.get('body_preview') or result.get('body') or message}"
                )
            return f"[Error]: {error.get('type', 'request_failed')} - {message}"

        data = result.get("body")
        if data is None and result.get("body_preview"):
            return str(result["body_preview"])

        if config.response_path and isinstance(data, (dict, list)):
            extracted = _extract_json_path(data, config.response_path)
            if extracted is _PATH_NOT_FOUND:
                return (
                    f"[Error]: response_path '{config.response_path}' did not match "
                    "the response body"
                )
            data = extracted

        return json.dumps(data, indent=2) if isinstance(data, (dict, list)) else str(data)

    except httpx.TimeoutException:
        return f"[Error]: Request timed out after {config.timeout_seconds} seconds"
    except ValueError as e:
        return f"[Error]: Configuration error - {e}"
    except Exception as e:
        logger.error(f"HTTP tool execution failed: {e}", exc_info=True)
        return f"[Error]: Request failed - {str(e)}"


def _sync_execute_http(
    config: HTTPToolConfig,
    params: Dict[str, Any],
    *,
    actor: Actor,
    target_type: str = "custom_http_tool",
    target_id: Optional[str] = None,
) -> str:
    """Synchronous entry point for HTTP tool execution (StructuredTool ``func``).

    Calls the synchronous core directly so it works on any thread, including a
    worker thread with no event loop (the previous ``get_event_loop().run_until_complete``
    raised there and inside an already-running loop).
    """
    return _sync_http_request(
        config,
        params,
        actor=actor,
        target_type=target_type,
        target_id=target_id,
    )


def _sync_execute_python(
    config: Any,
    params: Dict[str, Any],
    *,
    target_id: Optional[str] = None,
) -> str:
    """Synchronous wrapper for subprocess-backed Python tool execution."""
    from .python_custom_tools import _sync_execute_python_tool

    return _sync_execute_python_tool(config, params, target_id=target_id)


# Sentinel returned by ``_extract_json_path`` when a ``response_path`` segment
# cannot be resolved. The caller surfaces an error instead of the unfiltered
# body, so a misconfigured path cannot silently leak the whole payload.
_PATH_NOT_FOUND = object()


def _extract_json_path(data: Any, path: str) -> Any:
    """Extract a value from JSON data using a simple path syntax.

    Supports:
    - $.field - Get a field from root
    - $.field.nested - Get nested field
    - $.field[0] - Get array element
    - $.field[*] - Get all array elements

    Args:
        data: JSON data (dict or list).
        path: JSONPath-like expression.

    Returns:
        The extracted value, or the ``_PATH_NOT_FOUND`` sentinel when the path
        does not resolve against ``data`` (missing field, traversal into a
        non-dict, an out-of-bounds or non-list index, or a path that is not in
        ``$.``-prefixed form). A legitimately-present ``null`` value resolves to
        ``None`` and is therefore distinct from the not-found sentinel.
    """
    if not path.startswith("$."):
        return _PATH_NOT_FOUND

    parts = path[2:].split(".")
    current = data

    for part in parts:
        if not part:
            continue

        # Array access must be exactly "field[<int>]" or "field[*]". An anchored
        # full match (not re.match) rejects malformed segments such as
        # "field[0][1]" or "field[0]extra", which a prefix match would accept
        # while silently dropping the trailing characters. The field group
        # `[^\[]+` is always present when the pattern matches; a bare "[0]" has no
        # field and is treated as malformed below.
        array_match = re.fullmatch(r"([^\[]+)\[(\d+|\*)\]", part)
        if array_match:
            field = array_match.group(1)
            index = array_match.group(2)

            if isinstance(current, dict) and field in current:
                current = current[field]
            else:
                return _PATH_NOT_FOUND

            if not isinstance(current, list):
                return _PATH_NOT_FOUND
            if index == "*":
                pass  # Keep the full list
            else:
                idx = int(index)
                if idx >= len(current):
                    return _PATH_NOT_FOUND
                current = current[idx]
        elif "[" in part or "]" in part:
            # A bracketed segment that is not a clean "field[index]" is a
            # malformed array access (e.g. "field[0][1]", "field[abc]", "[0]");
            # signal not-found rather than treating it as a literal field name.
            return _PATH_NOT_FOUND
        else:
            # Regular field access.
            if isinstance(current, dict) and part in current:
                current = current[part]
            else:
                return _PATH_NOT_FOUND

    return current


def _create_pydantic_schema(tool_id: str, parameters: Dict[str, Any]) -> type:
    """Create a Pydantic model for tool parameters.

    Args:
        tool_id: Tool identifier for the model name.
        parameters: Parameter definitions.

    Returns:
        A Pydantic model class.
    """
    from pydantic import Field, create_model

    # Map JSON Schema types to Python types.
    type_map = {
        "string": str,
        "integer": int,
        "number": float,
        "boolean": bool,
        "array": list,
        "object": dict,
    }

    fields: dict[str, Any] = {}
    for name, param in parameters.items():
        python_type = type_map.get(param.type, str)

        # Honor a declared enum by constraining the field to the allowed
        # values (mirrors the MCP args-schema builder in mcp_servers.py), so
        # custom tools both advertise the allowed values to the model and
        # reject out-of-enum input. Falls back to the plain type if Literal
        # construction fails (e.g. an unhashable value).
        if param.enum:
            try:
                python_type = cast(Any, Literal)[tuple(param.enum)]
            except Exception:
                logger.debug(
                    "Could not build a Literal for the enum on parameter %r of "
                    "tool %r; falling back to the base type",
                    name,
                    tool_id,
                    exc_info=True,
                )

        # Create field with optional default
        if param.required:
            fields[name] = (python_type, Field(description=param.description))
        else:
            default = param.default
            fields[name] = (Optional[python_type], Field(default=default, description=param.description))

    # Create a dynamic Pydantic model
    model_name = f"{tool_id.title().replace('-', '').replace('_', '')}Args"
    return create_model(model_name, **fields)


# Global loader instance
_loader: Optional[CustomToolLoader] = None


def get_custom_tool_loader() -> CustomToolLoader:
    """Get or create the global custom tool loader."""
    global _loader
    if _loader is None:
        _loader = CustomToolLoader()
    return _loader


def load_custom_tools() -> List[BaseTool]:
    """Load all custom tools.

    Returns:
        List of LangChain tools.
    """
    return get_custom_tool_loader().load_all()


def reload_custom_tools() -> int:
    """Reload all custom tools.

    Returns:
        Number of tools loaded.
    """
    tools = get_custom_tool_loader().load_all()
    _mark_tool_search_dirty_safely()
    return len(tools)


def _mark_tool_search_dirty_safely() -> None:
    """Best-effort invalidation for the shared tool search catalog."""
    try:
        from .tool_search_index import mark_tool_search_dirty

        mark_tool_search_dirty()
    except Exception:
        logger.debug("Failed to mark tool search index dirty", exc_info=True)


def _log_external_edit_safely(store: str, detail: str, user_id: str = "default") -> None:
    """Best-effort external-edit audit line (never raises into a load path)."""
    try:
        from .activity_log import log_external_edit

        log_external_edit(store, detail, user_id=user_id)
    except Exception:
        logger.debug("Failed to record external-edit audit line", exc_info=True)


def shutdown_custom_tools() -> None:
    """Shutdown custom tools and cleanup resources."""
    global _loader
    if _loader:
        _loader.shutdown()
        _loader = None
