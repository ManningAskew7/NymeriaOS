"""MCP Server Registry — manages MCP server definitions and tool discovery.

Stores server configs as JSON in data/mcp_servers/. On add/connect,
discovers tools via tools/list and wraps them as LangChain StructuredTool
objects that route execution through the existing MCPServerManager.
"""

import json
import logging
from pathlib import Path
from typing import Any, Dict, List, Literal, Optional, Union, cast

from langchain_core.tools import BaseTool, StructuredTool

from ..config import get_settings
from ..tools.definitions.mcp_schema import (
    MCPDiscoveredTool,
    MCPServerDefinition,
    MCPToolConfig,
)
from .mcp_manager import get_mcp_manager
from .mcp_tool_names import format_mcp_tool_name, registered_mcp_tool_names
from .time_utils import utc_now

logger = logging.getLogger(__name__)


class MCPServerRegistry:
    """Central manager for MCP server definitions."""

    def __init__(self, servers_dir: Optional[Path] = None):
        settings = get_settings()
        self.servers_dir = servers_dir or settings.mcp_servers_dir
        self.servers_dir.mkdir(parents=True, exist_ok=True)

        # Cache: server_id -> definition
        self._definitions: Dict[str, MCPServerDefinition] = {}
        # Cache: tool_name -> BaseTool
        self._tools: Dict[str, BaseTool] = {}

        # Load existing definitions from disk
        self._load_all_definitions()

    def _load_all_definitions(self) -> None:
        """Load all server definitions from disk."""
        self._definitions.clear()
        for json_file in self.servers_dir.glob("*.json"):
            try:
                data = json.loads(json_file.read_text(encoding="utf-8"))
                defn = MCPServerDefinition(**data)
                self._definitions[defn.id] = defn
            except Exception as e:
                logger.error(f"Failed to load MCP server from {json_file}: {e}")

    # ---- CRUD ----

    def save_server(self, defn: MCPServerDefinition) -> Path:
        """Save a server definition to disk."""
        defn.updated_at = utc_now()
        file_path = self.servers_dir / f"{defn.id}.json"
        file_path.write_text(defn.model_dump_json(indent=2), encoding="utf-8")
        self._definitions[defn.id] = defn
        logger.info(f"Saved MCP server definition: {defn.id}")
        return file_path

    def delete_server(self, server_id: str) -> bool:
        """Delete a server definition and its tools."""
        file_path = self.servers_dir / f"{server_id}.json"
        if not file_path.exists():
            return False

        # Remove tools from cache
        defn = self._definitions.get(server_id)
        if defn:
            for dt in defn.discovered_tools:
                tool_name = format_mcp_tool_name(server_id, dt.name)
                self._tools.pop(tool_name, None)

        file_path.unlink()
        self._definitions.pop(server_id, None)
        logger.info(f"Deleted MCP server definition: {server_id}")
        return True

    def get_server(self, server_id: str) -> Optional[MCPServerDefinition]:
        """Get a server definition by ID."""
        return self._definitions.get(server_id)

    def get_all_servers(self) -> List[MCPServerDefinition]:
        """Get all server definitions."""
        return list(self._definitions.values())

    # ---- Discovery ----

    def discover_tools(self, server_id: str) -> List[MCPDiscoveredTool]:
        """Connect to a server, call tools/list, update discovered_tools.

        Returns the list of discovered tools.
        Raises RuntimeError if connection or discovery fails.
        """
        defn = self._definitions.get(server_id)
        if not defn:
            raise ValueError(f"MCP server not found: {server_id}")

        manager = get_mcp_manager()

        # Build a temporary MCPToolConfig to trigger connection
        config = MCPToolConfig(
            transport=defn.transport,
            server_command=defn.server_command,
            server_args=defn.server_args,
            url=defn.url,
            headers=defn.headers,
            tool_name="__discovery__",  # placeholder
            server_id=defn.id,
            env_vars=defn.env_vars,
            encrypted_env_vars=defn.encrypted_env_vars,
            working_directory=defn.working_directory,
            idle_timeout_seconds=defn.idle_timeout_seconds,
            startup_timeout_seconds=defn.startup_timeout_seconds,
        )

        # Get or create connection (this initializes + calls tools/list internally)
        conn = manager._get_or_create_connection(config)

        # The connection already has available_tools from initialization,
        # but we need full tool info. Re-send tools/list for schemas.
        tools_request = {
            "jsonrpc": "2.0",
            "id": conn.next_request_id(),
            "method": "tools/list",
        }
        response = manager._send_request(conn, tools_request, timeout=defn.startup_timeout_seconds)

        discovered = []
        if "result" in response:
            for tool_info in response["result"].get("tools", []):
                discovered.append(MCPDiscoveredTool(
                    name=tool_info.get("name", ""),
                    description=tool_info.get("description", ""),
                    input_schema=tool_info.get("inputSchema", {}),
                ))

        # Update definition and save
        defn.discovered_tools = discovered
        defn.registered_tool_names = registered_mcp_tool_names(defn, discovered)
        defn.updated_at = utc_now()
        self.save_server(defn)

        logger.info(f"Discovered {len(discovered)} tools from MCP server '{server_id}'")
        return discovered

    def test_connection(self, server_id: str) -> Dict[str, Any]:
        """Test connectivity to a server.

        Returns a dict with status info.
        """
        defn = self._definitions.get(server_id)
        if not defn:
            raise ValueError(f"MCP server not found: {server_id}")

        try:
            tools = self.discover_tools(server_id)
            return {
                "status": "ok",
                "server_id": server_id,
                "tools_count": len(tools),
                "tool_names": [t.name for t in tools],
            }
        except Exception as e:
            return {
                "status": "error",
                "server_id": server_id,
                "error": str(e),
            }

    # ---- Tool wrapping ----

    def get_all_tools(self) -> List[BaseTool]:
        """Create StructuredTool wrappers for all enabled servers' discovered tools."""
        self._tools.clear()
        tools = []

        for defn in self._definitions.values():
            if not defn.enabled or defn.install_status not in {"ready", "discovering"}:
                continue
            for dt in defn.discovered_tools:
                tool_name = format_mcp_tool_name(defn.id, dt.name)
                try:
                    tool = self._wrap_tool(defn, dt, tool_name)
                    self._tools[tool_name] = tool
                    tools.append(tool)
                except Exception as e:
                    logger.error(f"Failed to wrap MCP tool {tool_name}: {e}")

        logger.info(f"Wrapped {len(tools)} MCP server tool(s)")
        return tools

    def _wrap_tool(
        self,
        defn: MCPServerDefinition,
        discovered: MCPDiscoveredTool,
        tool_name: str,
    ) -> BaseTool:
        """Wrap a single discovered MCP tool as a LangChain StructuredTool."""
        # Build MCPToolConfig for execution
        config = MCPToolConfig(
            transport=defn.transport,
            server_command=defn.server_command,
            server_args=defn.server_args,
            url=defn.url,
            headers=defn.headers,
            tool_name=discovered.name,
            server_id=defn.id,
            env_vars=defn.env_vars,
            encrypted_env_vars=defn.encrypted_env_vars,
            working_directory=defn.working_directory,
            idle_timeout_seconds=defn.idle_timeout_seconds,
            startup_timeout_seconds=defn.startup_timeout_seconds,
        )

        manager = get_mcp_manager()

        async def execute_mcp(**kwargs: Any) -> str:
            return await manager.call_tool(config, kwargs)

        def execute_mcp_sync(**kwargs: Any) -> str:
            return manager.call_tool_sync(config, kwargs)

        # Build args schema from discovered input_schema
        args_schema = self._create_args_schema(tool_name, discovered.input_schema)

        description = discovered.description or f"MCP tool: {discovered.name}"

        return StructuredTool.from_function(
            func=execute_mcp_sync,
            coroutine=execute_mcp,
            name=tool_name,
            description=description,
            args_schema=args_schema,
        )

    def _create_args_schema(
        self, tool_name: str, input_schema: Dict[str, Any]
    ) -> type:
        """Create a Pydantic model from MCP JSON Schema input_schema."""
        from pydantic import Field, create_model
        from typing import Any, Optional

        def clean_model_name(raw: str) -> str:
            cleaned = "".join(ch for ch in raw.title() if ch.isalnum())
            return cleaned or "MCPArgs"

        def schema_type(schema: Dict[str, Any], path: str) -> Any:
            if not isinstance(schema, dict):
                return Any
            if "enum" in schema and isinstance(schema["enum"], list) and schema["enum"]:
                try:
                    return cast(Any, Literal)[tuple(schema["enum"])]
                except Exception:
                    return Any
            if "anyOf" in schema or "oneOf" in schema:
                variants = schema.get("anyOf") or schema.get("oneOf") or []
                mapped = tuple(schema_type(v, f"{path}Variant{i}") for i, v in enumerate(variants) if isinstance(v, dict))
                if not mapped:
                    return Any
                if len(mapped) == 1:
                    return mapped[0]
                try:
                    return cast(Any, Union)[mapped]
                except Exception:
                    return Any
            raw_type = schema.get("type")
            if isinstance(raw_type, list):
                non_null = [item for item in raw_type if item != "null"]
                if len(non_null) == 1:
                    raw_type = non_null[0]
                else:
                    return Any
            if raw_type == "string":
                return str
            if raw_type == "integer":
                return int
            if raw_type == "number":
                return float
            if raw_type == "boolean":
                return bool
            if raw_type == "array":
                item_type = schema_type(schema.get("items") or {}, f"{path}Item")
                return list[item_type]  # type: ignore[valid-type]
            if raw_type == "object" or "properties" in schema:
                props = schema.get("properties")
                if not isinstance(props, dict):
                    return dict[str, Any]
                required = set(schema.get("required") or [])
                nested_fields: dict[str, Any] = {}
                for prop_name, prop_schema in props.items():
                    nested_type = schema_type(prop_schema, f"{path}{clean_model_name(str(prop_name))}")
                    default = ... if prop_name in required else prop_schema.get("default", None)
                    nested_fields[str(prop_name)] = (
                        nested_type if prop_name in required else Optional[nested_type],
                        Field(
                            default,
                            description=prop_schema.get("description", "") if isinstance(prop_schema, dict) else "",
                        ),
                    )
                return create_model(clean_model_name(path), **nested_fields)
            return dict[str, Any]

        def field_kwargs(prop: Dict[str, Any]) -> Dict[str, Any]:
            kwargs: Dict[str, Any] = {"description": prop.get("description", "")}
            for schema_key, field_key in (
                ("minimum", "ge"),
                ("maximum", "le"),
                ("exclusiveMinimum", "gt"),
                ("exclusiveMaximum", "lt"),
                ("minLength", "min_length"),
                ("maxLength", "max_length"),
                ("pattern", "pattern"),
            ):
                if schema_key in prop:
                    kwargs[field_key] = prop[schema_key]
            return kwargs

        properties = input_schema.get("properties", {}) if isinstance(input_schema, dict) else {}
        required_fields = set(input_schema.get("required", [])) if isinstance(input_schema, dict) else set()

        fields: dict[str, Any] = {}
        for name, prop in properties.items():
            prop = prop if isinstance(prop, dict) else {}
            python_type = schema_type(prop, f"{tool_name}_{name}")
            if name in required_fields:
                fields[name] = (python_type, Field(..., **field_kwargs(prop)))
            else:
                default = prop.get("default", None)
                fields[name] = (
                    Optional[python_type],
                    Field(default, **field_kwargs(prop)),
                )

        model_name = (
            tool_name.replace("__", "_")
            .title()
            .replace("_", "")
            .replace("-", "")
            + "Args"
        )
        return create_model(model_name, **fields)


# Global registry instance
_registry: Optional[MCPServerRegistry] = None


def get_mcp_server_registry() -> MCPServerRegistry:
    """Get or create the global MCP server registry."""
    global _registry
    if _registry is None:
        _registry = MCPServerRegistry()
    return _registry


def reload_mcp_server_registry() -> MCPServerRegistry:
    """Reload the global MCP server registry."""
    global _registry
    _registry = MCPServerRegistry()
    return _registry
