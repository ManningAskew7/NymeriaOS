"""Custom tool loader and executor.

Loads custom tool definitions from JSON files and converts them to
LangChain @tool functions. Supports HTTP, MCP, and Python tool implementations.

HTTP tools make REST API calls with parameter interpolation.
MCP tools communicate with Model Context Protocol servers via JSON-RPC.
Python tools execute configured source code in a child process.

Based on MCP best practices 2025-2026:
- Environment variable interpolation for secrets
- Structured error handling
- Request timeout and retry logic
"""

import asyncio
import json
import logging
import os
import re
from pathlib import Path
from typing import TYPE_CHECKING, Any, Dict, List, Optional

import httpx
from langchain_core.tools import BaseTool, StructuredTool

from ..config import get_settings
from ..tools.definitions.custom_tool_schema import CustomToolDefinition, HTTPToolConfig
from ..tools.metadata import (
    clear_custom_tool_metadata,
    register_custom_tool_metadata,
    unregister_custom_tool_metadata,
)
from .time_utils import utc_now

logger = logging.getLogger(__name__)

if TYPE_CHECKING:
    from .mcp_manager import MCPServerManager

# Regex for environment variable interpolation: ${env:VAR_NAME}
ENV_VAR_PATTERN = re.compile(r"\$\{env:([A-Z_][A-Z0-9_]*)\}")

# Regex for parameter interpolation: ${param_name}
PARAM_PATTERN = re.compile(r"\$\{([a-zA-Z_][a-zA-Z0-9_]*)\}")


class CustomToolLoader:
    """Loads and manages custom tool definitions.

    Watches a directory for JSON tool definition files and converts
    them to LangChain tools that can be registered with the agent.
    """

    def __init__(self, tools_dir: Optional[Path] = None):
        """Initialize the custom tool loader.

        Args:
            tools_dir: Directory containing tool definition JSON files.
                       Defaults to data/custom_tools/ in project root.
        """
        settings = get_settings()
        self.tools_dir = tools_dir or settings.custom_tools_dir
        self.tools_dir.mkdir(parents=True, exist_ok=True)

        # Cache of loaded tool definitions
        self._definitions: Dict[str, CustomToolDefinition] = {}

        # Cache of created LangChain tools
        self._tools: Dict[str, BaseTool] = {}

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
        self._definitions.clear()
        self._tools.clear()
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

    def _load_tool_file(self, file_path: Path) -> Optional[BaseTool]:
        """Load a single tool definition file.

        Args:
            file_path: Path to the JSON definition file.

        Returns:
            LangChain tool or None if loading failed.
        """
        try:
            data = json.loads(file_path.read_text(encoding="utf-8"))
            definition = CustomToolDefinition(**data)

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
            else:
                logger.error(f"Unknown implementation type: {definition.implementation_type}")
                return None

            self._tools[definition.id] = tool
            register_custom_tool_metadata(definition.id, definition.description)
            return tool

        except Exception as e:
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

        async def execute_http(**kwargs: Any) -> str:
            """Execute the HTTP tool with given parameters."""
            return await execute_http_tool(
                config,
                kwargs,
                target_type="custom_tool",
                target_id=definition.id,
            )

        return StructuredTool.from_function(
            func=lambda **kwargs: _sync_execute_http(
                config,
                kwargs,
                target_type="custom_tool",
                target_id=definition.id,
            ),
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
        """Create a subprocess-backed LangChain tool from Python source."""
        config = definition.python_config
        assert config is not None

        async def execute_python(**kwargs: Any) -> str:
            from .python_custom_tools import execute_python_tool

            return await execute_python_tool(
                config,
                kwargs,
                target_id=definition.id,
            )

        return StructuredTool.from_function(
            func=lambda **kwargs: _sync_execute_python(
                config,
                kwargs,
                target_id=definition.id,
            ),
            coroutine=execute_python,
            name=definition.id,
            description=definition.description,
            args_schema=_create_pydantic_schema(definition.id, definition.parameters),
        )

    def get_definition(self, tool_id: str) -> Optional[CustomToolDefinition]:
        """Get a tool definition by ID.

        Args:
            tool_id: The tool identifier.

        Returns:
            The tool definition or None.
        """
        return self._definitions.get(tool_id)

    def get_all_definitions(self) -> List[CustomToolDefinition]:
        """Get all loaded tool definitions.

        Returns:
            List of all tool definitions.
        """
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
        file_path.write_text(
            definition.model_dump_json(indent=2),
            encoding="utf-8",
        )

        # Reload to update cache
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

        file_path.unlink()

        # Remove from caches
        self._definitions.pop(tool_id, None)
        self._tools.pop(tool_id, None)
        unregister_custom_tool_metadata(tool_id)

        _mark_tool_search_dirty_safely()
        logger.info(f"Deleted tool definition: {tool_id}")
        return True

    def shutdown(self) -> None:
        """Shutdown the loader and cleanup resources."""
        # The MCP connection manager is process-wide and may also be used by
        # managed MCP server tools, so this loader does not own its lifecycle.
        return None


def interpolate_env_vars(value: str) -> str:
    """Replace ${env:VAR_NAME} placeholders with environment variable values.

    Args:
        value: String containing environment variable placeholders.

    Returns:
        String with placeholders replaced by actual values.

    Raises:
        ValueError: If an environment variable is not set.
    """
    def replace_env(match: re.Match) -> str:
        var_name = match.group(1)
        var_value = os.environ.get(var_name)
        if var_value is None:
            raise ValueError(f"Environment variable not set: {var_name}")
        return var_value

    return ENV_VAR_PATTERN.sub(replace_env, value)


def interpolate_env_vars_with_names(value: str) -> tuple[str, set[str]]:
    """Replace env placeholders and return the variable names used."""
    used: set[str] = set()

    def replace_env(match: re.Match) -> str:
        var_name = match.group(1)
        var_value = os.environ.get(var_name)
        if var_value is None:
            raise ValueError(f"Environment variable not set: {var_name}")
        used.add(var_name)
        return var_value

    return ENV_VAR_PATTERN.sub(replace_env, value), used


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


async def execute_http_tool(
    config: HTTPToolConfig,
    params: Dict[str, Any],
    *,
    target_type: str = "custom_http_tool",
    target_id: Optional[str] = None,
    actor_user_id: Optional[str] = None,
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
        target_type=target_type,
        target_id=target_id,
        actor_user_id=actor_user_id,
    )


def _sync_http_request(
    config: HTTPToolConfig,
    params: Dict[str, Any],
    *,
    target_type: str = "custom_http_tool",
    target_id: Optional[str] = None,
    actor_user_id: Optional[str] = None,
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
            if "${credential:" not in value:
                return value
            from .credential_vault import get_credential_vault_repo

            return get_credential_vault_repo().resolve_references(
                value,
                actor_user_id=actor_user_id,
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
    target_type: str = "custom_http_tool",
    target_id: Optional[str] = None,
    actor_user_id: Optional[str] = None,
) -> str:
    """Synchronous entry point for HTTP tool execution (StructuredTool ``func``).

    Calls the synchronous core directly so it works on any thread, including a
    worker thread with no event loop (the previous ``get_event_loop().run_until_complete``
    raised there and inside an already-running loop).
    """
    return _sync_http_request(
        config,
        params,
        target_type=target_type,
        target_id=target_id,
        actor_user_id=actor_user_id,
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

        # Handle array access, e.g. "field[0]" or "field[*]". The field group
        # is `[^\[]+` (one or more), so it is always present when the pattern
        # matches; a bare "[0]" falls through to regular field access below.
        array_match = re.match(r"([^\[]+)\[(\d+|\*)\]", part)
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

    fields: dict[str, Any] = {}
    for name, param in parameters.items():
        # Map JSON Schema types to Python types
        type_map = {
            "string": str,
            "integer": int,
            "number": float,
            "boolean": bool,
            "array": list,
            "object": dict,
        }
        python_type = type_map.get(param.type, str)

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


def shutdown_custom_tools() -> None:
    """Shutdown custom tools and cleanup resources."""
    global _loader
    if _loader:
        _loader.shutdown()
        _loader = None
