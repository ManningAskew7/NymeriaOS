"""MCP Server Registry — manages MCP server definitions and tool discovery.

Stores server configs as JSON in data/mcp_servers/. On add/connect,
discovers tools via tools/list and wraps them as LangChain StructuredTool
objects that route execution through the existing MCPServerManager.
"""

import json
import logging
import threading
import time
from pathlib import Path
from typing import Any, Dict, List, Literal, Optional, Tuple, Union, cast

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

    # Minimum seconds between external-edit stat scans (hot-load debounce).
    # Class attribute so tests can zero it.
    EXTERNAL_REFRESH_INTERVAL_SECONDS: float = 2.0

    def __init__(self, servers_dir: Optional[Path] = None):
        settings = get_settings()
        self.servers_dir = servers_dir or settings.mcp_servers_dir
        self.servers_dir.mkdir(parents=True, exist_ok=True)

        # Guards the caches and the disk-fingerprint bookkeeping below.
        self._lock = threading.RLock()

        # Cache: server_id -> definition
        self._definitions: Dict[str, MCPServerDefinition] = {}
        # Cache: tool_name -> BaseTool
        self._tools: Dict[str, BaseTool] = {}

        # Hot-load bookkeeping (resource-filesystem-layout plan, slice 2).
        # filename -> (st_mtime_ns, st_size) recorded whenever a definition
        # file passes through this registry, so manager-driven writes never
        # register as external edits; filename -> server id for removals.
        self._disk_sigs: Dict[str, Tuple[int, int]] = {}
        self._file_ids: Dict[str, str] = {}
        self._registry_sync_needed = False
        self._last_freshness_check = 0.0

        # Load existing definitions from disk
        self._load_all_definitions()

    def _load_all_definitions(self) -> None:
        """Load all server definitions from disk."""
        with self._lock:
            self._definitions.clear()
            self._disk_sigs.clear()
            self._file_ids.clear()
            for json_file in self.servers_dir.glob("*.json"):
                self._load_definition_file(json_file)

    def _load_definition_file(self, json_file: Path) -> None:
        """Load one definition file, recording its disk fingerprint."""
        try:
            st = json_file.stat()
            self._disk_sigs[json_file.name] = (st.st_mtime_ns, st.st_size)
        except OSError:
            pass  # Unreadable stat: the refresh sweep will retry the file.
        try:
            data = json.loads(json_file.read_text(encoding="utf-8"))
            defn = MCPServerDefinition(**data)
            self._definitions[defn.id] = defn
            self._file_ids[json_file.name] = defn.id
        except Exception as e:  # noqa: BLE001 - unparseable file, quarantine it
            from .storage_paths import quarantine_corrupt_file

            quarantine = quarantine_corrupt_file(json_file)
            if quarantine is not None:
                self._disk_sigs.pop(json_file.name, None)
                self._file_ids.pop(json_file.name, None)
            logger.error(
                "Failed to load MCP server from %s: %s (%s)",
                json_file,
                e,
                f"corrupt file preserved at {quarantine}"
                if quarantine
                else "quarantine rename failed; file left in place",
            )
            from .activity_log import log_external_edit

            log_external_edit(
                "mcp_servers",
                f"corrupt file {json_file.name} "
                + (f"quarantined as quarantine/{quarantine.name}" if quarantine
                   else "could not be quarantined"),
            )

    def refresh_if_stale(self, force: bool = False) -> List[str]:
        """Pick up external (non-manager) edits to the definition files.

        Stat-scans the servers dir (debounced) and re-parses only files whose
        (mtime_ns, size) fingerprint changed, dropping servers whose files
        disappeared. Returns the affected server ids; the agent re-registers
        the wrapped tools via ``agent_tools.sync_external_resource_edits``.
        """
        now = time.monotonic()
        with self._lock:
            if not force and now - self._last_freshness_check < self.EXTERNAL_REFRESH_INTERVAL_SECONDS:
                return []
            self._last_freshness_check = now

        try:
            current: Dict[str, Tuple[int, int]] = {}
            for json_file in self.servers_dir.glob("*.json"):
                try:
                    st = json_file.stat()
                except OSError:
                    continue
                current[json_file.name] = (st.st_mtime_ns, st.st_size)
        except OSError:
            return []

        changed_ids: List[str] = []
        with self._lock:
            stale = [name for name, sig in current.items() if self._disk_sigs.get(name) != sig]
            removed = [name for name in self._disk_sigs if name not in current]
            if not stale and not removed:
                return []

            for name in removed:
                self._disk_sigs.pop(name, None)
                server_id = self._file_ids.pop(name, None)
                if server_id:
                    defn = self._definitions.pop(server_id, None)
                    if defn:
                        for dt in defn.discovered_tools:
                            self._tools.pop(format_mcp_tool_name(server_id, dt.name), None)
                    changed_ids.append(server_id)
            for name in stale:
                old_id = self._file_ids.pop(name, None)
                if old_id:
                    self._definitions.pop(old_id, None)
                    changed_ids.append(old_id)
                self._load_definition_file(self.servers_dir / name)
                new_id = self._file_ids.get(name)
                if new_id and new_id not in changed_ids:
                    changed_ids.append(new_id)

            self._registry_sync_needed = True
            logger.info(
                "MCP server store: external edit detected (%d changed, %d removed "
                "file(s)); refreshed ids %s",
                len(stale), len(removed), sorted(changed_ids),
            )
        from .activity_log import log_external_edit

        log_external_edit(
            "mcp_servers",
            f"{len(stale)} changed, {len(removed)} removed file(s); "
            f"server ids {sorted(changed_ids)}",
        )
        return changed_ids

    def drain_registry_sync(self) -> bool:
        """Return and clear the agent-registry re-sync flag."""
        with self._lock:
            needed = self._registry_sync_needed
            self._registry_sync_needed = False
            return needed

    # ---- CRUD ----

    def save_server(self, defn: MCPServerDefinition) -> Path:
        """Save a server definition to disk."""
        defn.updated_at = utc_now()
        file_path = self.servers_dir / f"{defn.id}.json"
        with self._lock:
            file_path.write_text(defn.model_dump_json(indent=2), encoding="utf-8")
            self._definitions[defn.id] = defn
            self._file_ids[file_path.name] = defn.id
            try:
                st = file_path.stat()
                self._disk_sigs[file_path.name] = (st.st_mtime_ns, st.st_size)
            except OSError:
                pass  # Fingerprint refresh is best-effort; the sweep retries.
        logger.info(f"Saved MCP server definition: {defn.id}")
        return file_path

    def delete_server(self, server_id: str) -> bool:
        """Delete a server definition and its tools."""
        file_path = self.servers_dir / f"{server_id}.json"
        if not file_path.exists():
            return False

        with self._lock:
            # Remove tools from cache
            defn = self._definitions.get(server_id)
            if defn:
                for dt in defn.discovered_tools:
                    tool_name = format_mcp_tool_name(server_id, dt.name)
                    self._tools.pop(tool_name, None)

            file_path.unlink()
            self._definitions.pop(server_id, None)
            self._disk_sigs.pop(file_path.name, None)
            self._file_ids.pop(file_path.name, None)
        logger.info(f"Deleted MCP server definition: {server_id}")
        return True

    def get_server(self, server_id: str) -> Optional[MCPServerDefinition]:
        """Get a server definition by ID (picking up external file edits)."""
        self.refresh_if_stale()
        with self._lock:
            return self._definitions.get(server_id)

    def get_all_servers(self) -> List[MCPServerDefinition]:
        """Get all server definitions (picking up external file edits)."""
        self.refresh_if_stale()
        with self._lock:
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

        # Connect and fetch full tool records (name/description/inputSchema) via the
        # manager's public discovery API rather than reaching into its connection internals.
        discovered = [
            MCPDiscoveredTool(
                name=tool_info.get("name", ""),
                description=tool_info.get("description", ""),
                input_schema=tool_info.get("inputSchema", {}),
            )
            for tool_info in manager.list_tools_detailed(config)
        ]

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
        with self._lock:
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
