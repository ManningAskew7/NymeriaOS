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
from typing import Any, Dict, List, Literal, Optional, Union, cast

from langchain_core.tools import BaseTool, StructuredTool

from ..config import get_settings
from ..tools.definitions.mcp_schema import (
    MCPDiscoveredTool,
    MCPServerDefinition,
    MCPToolConfig,
)
from .mcp_execution_gate import mcp_execution_gate, stamp_mcp_approval
from .mcp_manager import get_mcp_manager
from .mcp_tool_names import format_mcp_tool_name, registered_mcp_tool_names
from .storage_paths import (
    FileFingerprint,
    capture_fingerprint,
    scan_fingerprint_map,
)
from .time_utils import utc_now

logger = logging.getLogger(__name__)

# Cap a third-party MCP tool description before it enters the model context.
# Long enough for a genuine multi-sentence description, short enough that a
# hostile or runaway server cannot flood the prompt.
MCP_TOOL_DESCRIPTION_MAX_CHARS = 1500


# Every server-controlled token interpolated into a description is capped so a
# hostile server can't flood the prompt through the name or the server label,
# not just the description body.
_MCP_DESC_TOKEN_MAX_CHARS = 120


def _cap_token(value: str, limit: int = _MCP_DESC_TOKEN_MAX_CHARS) -> str:
    text = str(value or "")
    return text if len(text) <= limit else text[:limit] + "..."


def _format_mcp_tool_description(server_name: str, raw_description: str, raw_name: str) -> str:
    """Frame a discovered MCP tool description as untrusted third-party text.

    The server-authored text is capped and quoted behind an explicit provenance
    lead-in ("MCP server '<name>' describes this tool as: ..."), so the model
    reads it as an external description rather than a system instruction. That
    blunts prompt-injection via tool descriptions without hiding the content.
    Every interpolated server-controlled token (description, name, label) is
    length-capped so no branch can flood the prompt.
    """
    text = (raw_description or "").strip()
    label = _cap_token((server_name or "").strip()) or "an MCP server"
    if not text:
        return f"Tool '{_cap_token(raw_name)}' provided by {label} (no description supplied)."
    if len(text) > MCP_TOOL_DESCRIPTION_MAX_CHARS:
        text = text[:MCP_TOOL_DESCRIPTION_MAX_CHARS].rstrip() + " ... [truncated]"
    return f"MCP server '{label}' describes this tool as: {text}"


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
        # filename -> FileFingerprint recorded whenever a definition file
        # passes through this registry, so manager-driven writes never
        # register as external edits; filename -> server id for removals.
        self._disk_sigs: Dict[str, FileFingerprint] = {}
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
        fingerprint = capture_fingerprint(json_file)
        if fingerprint is not None:
            self._disk_sigs[json_file.name] = fingerprint
        # Unreadable stat: the refresh sweep will retry the file.
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
        fingerprint changed, dropping servers whose files disappeared. Returns
        the affected server ids; the agent re-registers the wrapped tools via
        ``agent_tools.sync_external_resource_edits``.
        """
        now = time.monotonic()
        with self._lock:
            if not force and now - self._last_freshness_check < self.EXTERNAL_REFRESH_INTERVAL_SECONDS:
                return []
            self._last_freshness_check = now

        changed_ids: List[str] = []
        with self._lock:
            try:
                entries = [(f.name, f) for f in self.servers_dir.glob("*.json")]
            except OSError:
                return []
            current, stale, removed = scan_fingerprint_map(entries, self._disk_sigs)
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
        """Save a server definition to disk.

        Stamps the execution-trust approval hash (core/mcp_execution_gate.py):
        any definition persisted through Nymeria's code is approved, while a
        raw-disk / hot-loaded file that never reaches this method stays inert.
        """
        defn.updated_at = utc_now()
        stamp_mcp_approval(defn)
        # Redact secret-shaped text (stderr tails, exception strings) captured
        # into last_error before it is persisted where the model and admins can
        # read it. One chokepoint here covers every last_error assignment site
        # (runtime drafts, REST error paths, /mcp discover). mcp_runtime is
        # imported function-locally: no import cycle.
        if defn.last_error:
            from .mcp_runtime import _redact_secret_like_text

            defn.last_error = _redact_secret_like_text(defn.last_error)
        from .storage_paths import write_text_atomic

        file_path = self.servers_dir / f"{defn.id}.json"
        with self._lock:
            # Atomic write: a crash mid-save must not leave a torn JSON file
            # that the loader then quarantines, losing the prior definition.
            write_text_atomic(file_path, defn.model_dump_json(indent=2))
            self._definitions[defn.id] = defn
            self._file_ids[file_path.name] = defn.id
            fingerprint = capture_fingerprint(file_path)
            if fingerprint is not None:
                self._disk_sigs[file_path.name] = fingerprint
            # Fingerprint refresh is best-effort; the sweep retries.
        logger.info(f"Saved MCP server definition: {defn.id}")
        return file_path

    def delete_server(self, server_id: str) -> bool:
        """Delete a server definition and its tools.

        Resolves the on-disk file(s) by cache VALUE, not by an assumed
        ``<id>.json`` name. A hot-loaded / raw-planted file whose filename
        differs from the id it declares (or two files declaring the same id)
        would otherwise survive an ``<id>.json``-only unlink and resurrect the
        definition on the next reload. We therefore unlink every file this
        registry maps to ``server_id`` (plus the canonical ``<id>.json`` for
        safety) and drop all matching cache entries. Returns True when a cached
        definition or an on-disk file was removed, False when nothing matched.
        """
        with self._lock:
            defn = self._definitions.get(server_id)
            # Every filename this registry has associated with the id, plus the
            # canonical name in case the file was never loaded into _file_ids.
            filenames = {
                name for name, sid in self._file_ids.items() if sid == server_id
            }
            filenames.add(f"{server_id}.json")

            removed = server_id in self._definitions
            for name in filenames:
                fp = self.servers_dir / name
                try:
                    if fp.exists():
                        fp.unlink()
                        removed = True
                except OSError:
                    logger.warning("Failed to unlink MCP server file %s", name)
                self._disk_sigs.pop(name, None)
                self._file_ids.pop(name, None)

            if defn:
                for dt in defn.discovered_tools:
                    tool_name = format_mcp_tool_name(server_id, dt.name)
                    self._tools.pop(tool_name, None)
            self._definitions.pop(server_id, None)

        if removed:
            logger.info(f"Deleted MCP server definition: {server_id}")
        return removed

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
        # Snapshot the launch surface under the lock; the network/subprocess
        # round trip below must NOT hold it (it would serialize every reader of
        # the cache behind a slow server). Build a temporary MCPToolConfig to
        # trigger connection.
        with self._lock:
            defn = self._definitions.get(server_id)
            if not defn:
                raise ValueError(f"MCP server not found: {server_id}")
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
                call_timeout_seconds=defn.call_timeout_seconds,
            )

        manager = get_mcp_manager()

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

        # Re-acquire the lock to mutate + persist. Re-fetch the definition: a
        # concurrent reload may have swapped the cached object while we did I/O,
        # so mutate whatever is current (falling back to our snapshot). save_server
        # re-enters the RLock and stamps updated_at.
        with self._lock:
            current = self._definitions.get(server_id) or defn
            current.discovered_tools = discovered
            current.registered_tool_names = registered_mcp_tool_names(current, discovered)
            self.save_server(current)

        logger.info(f"Discovered {len(discovered)} tools from MCP server '{server_id}'")
        return discovered

    def test_connection(self, server_id: str) -> Dict[str, Any]:
        """Test connectivity to a server.

        Returns a dict with status info.
        """
        with self._lock:
            if server_id not in self._definitions:
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
                gate_reason = mcp_execution_gate(defn)
                if gate_reason:
                    logger.warning(
                        "Skipping unapproved MCP server '%s': %s", defn.id, gate_reason
                    )
                    continue
                for dt in defn.discovered_tools:
                    raw_name = str(getattr(dt, "name", "") or "")
                    if not raw_name.strip():
                        logger.error(
                            "Skipping MCP tool with empty name on server '%s'",
                            defn.id,
                        )
                        continue
                    tool_name = format_mcp_tool_name(defn.id, raw_name)
                    # Two raw names can sanitize to the same internal name (e.g.
                    # "a.b" and "a b" -> "a_b"). The wrapper is keyed by internal
                    # name but dispatches the RAW name, so silently overwriting
                    # would send the wrong tools/call. Keep the first, skip the
                    # rest with a clear log.
                    if tool_name in self._tools:
                        logger.error(
                            "Skipping MCP tool '%s' on server '%s': its sanitized "
                            "name '%s' collides with an already-wrapped tool",
                            raw_name,
                            defn.id,
                            tool_name,
                        )
                        continue
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
            call_timeout_seconds=defn.call_timeout_seconds,
        )

        manager = get_mcp_manager()
        server_id = defn.id

        def _runtime_gate() -> Optional[str]:
            """Re-check the live definition's approval right before dispatch.

            The closure above captured a config snapshot; re-reading the live
            definition here makes an edit-after-wrap (or a hot-loaded raw edit)
            inert. Fails open only on an infra error (the wrap-time filter
            already vetted approval), closed on an actual gate verdict.
            """
            try:
                live = get_mcp_server_registry().get_server(server_id)
            except Exception:
                logger.warning(
                    "MCP execution gate: registry lookup failed for %s",
                    server_id,
                    exc_info=True,
                )
                return None
            if live is None:
                return f"[Error]: MCP server '{server_id}' is no longer available."
            reason = mcp_execution_gate(live)
            return f"[Error]: {reason}" if reason else None

        async def execute_mcp(**kwargs: Any) -> str:
            blocked = _runtime_gate()
            if blocked:
                return blocked
            return await manager.call_tool(config, kwargs)

        def execute_mcp_sync(**kwargs: Any) -> str:
            blocked = _runtime_gate()
            if blocked:
                return blocked
            return manager.call_tool_sync(config, kwargs)

        # Build args schema from discovered input_schema
        args_schema = self._create_args_schema(tool_name, discovered.input_schema)

        description = _format_mcp_tool_description(
            defn.name, discovered.description, discovered.name
        )

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
        """Create a Pydantic model from a third-party MCP JSON Schema.

        Hardened against untrusted server schemas (T5): intra-document
        ``$ref``/``$defs`` are resolved (with a visited-set for cycles) so
        nested/reused shapes keep structure instead of degrading to
        ``dict[str, Any]``; numeric/string constraints are applied only when the
        resolved type matches and the value is well-formed (Draft-4 boolean
        ``exclusiveMinimum``/``Maximum`` are ignored, not fed to pydantic); and a
        build failure drops constraints rather than the whole tool.
        """
        from pydantic import Field, create_model
        from typing import Any, Optional

        root = input_schema if isinstance(input_schema, dict) else {}
        defs: Dict[str, Any] = {}
        for defs_key in ("$defs", "definitions"):
            candidate = root.get(defs_key)
            if isinstance(candidate, dict):
                defs.update(candidate)

        def lookup_ref(ref: Any) -> Optional[Dict[str, Any]]:
            """Resolve an intra-document ``#/$defs/x`` or ``#/definitions/x`` ref."""
            if not isinstance(ref, str):
                return None
            for prefix in ("#/$defs/", "#/definitions/"):
                if ref.startswith(prefix):
                    target = defs.get(ref[len(prefix):])
                    return target if isinstance(target, dict) else None
            return None  # external / unsupported ref -> caller degrades

        def clean_model_name(raw: str) -> str:
            cleaned = "".join(ch for ch in raw.title() if ch.isalnum())
            return cleaned or "MCPArgs"

        def schema_type(schema: Any, path: str, visited: frozenset) -> Any:
            if not isinstance(schema, dict):
                return Any
            if "$ref" in schema:
                ref = schema["$ref"]
                # A non-string (or unhashable) ref is malformed; degrade just
                # this field rather than letting `ref in visited` raise and
                # escalate the whole tool to the permissive fallback.
                if not isinstance(ref, str):
                    return dict[str, Any]
                if ref in visited:  # cyclic definition -> stop unrolling
                    return dict[str, Any]
                target = lookup_ref(ref)
                if target is None:
                    return dict[str, Any]
                return schema_type(target, path, visited | {ref})
            if "enum" in schema and isinstance(schema["enum"], list) and schema["enum"]:
                try:
                    return cast(Any, Literal)[tuple(schema["enum"])]
                except Exception:
                    return Any
            if "anyOf" in schema or "oneOf" in schema:
                variants = schema.get("anyOf") or schema.get("oneOf") or []
                mapped = tuple(
                    schema_type(v, f"{path}Variant{i}", visited)
                    for i, v in enumerate(variants)
                    if isinstance(v, dict)
                )
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
                item_type = schema_type(schema.get("items") or {}, f"{path}Item", visited)
                return list[item_type]  # type: ignore[valid-type]
            if raw_type == "object" or "properties" in schema:
                props = schema.get("properties")
                if not isinstance(props, dict):
                    return dict[str, Any]
                required = set(schema.get("required") or [])
                nested_fields: dict[str, Any] = {}
                for prop_name, prop_schema in props.items():
                    nested_type = schema_type(
                        prop_schema, f"{path}{clean_model_name(str(prop_name))}", visited
                    )
                    default = ... if prop_name in required else (
                        prop_schema.get("default", None) if isinstance(prop_schema, dict) else None
                    )
                    nested_fields[str(prop_name)] = (
                        nested_type if prop_name in required else Optional[nested_type],
                        Field(
                            default,
                            description=prop_schema.get("description", "") if isinstance(prop_schema, dict) else "",
                        ),
                    )
                try:
                    return create_model(clean_model_name(path), **nested_fields)
                except Exception:
                    # A malformed nested object should not kill the whole tool;
                    # degrade this one branch to an untyped mapping.
                    return dict[str, Any]
            return dict[str, Any]

        def field_kwargs(prop: Dict[str, Any], resolved_type: Any) -> Dict[str, Any]:
            """Map JSON-Schema constraints to pydantic Field kwargs, defensively.

            Only constraints that match the resolved type and carry a well-formed
            value are forwarded; the rest are dropped (never handed to pydantic,
            where a Draft-4 boolean bound or a type-mismatched constraint would
            raise and take the tool down with it).
            """
            kwargs: Dict[str, Any] = {
                "description": prop.get("description", "") if isinstance(prop, dict) else ""
            }
            if not isinstance(prop, dict):
                return kwargs
            is_numeric = resolved_type in (int, float)
            is_str = resolved_type is str
            for schema_key, field_key in (
                ("minimum", "ge"),
                ("maximum", "le"),
                ("exclusiveMinimum", "gt"),
                ("exclusiveMaximum", "lt"),
            ):
                if not (is_numeric and schema_key in prop):
                    continue
                value = prop[schema_key]
                # Draft-4 uses a boolean exclusiveMinimum/Maximum; bool is an int
                # subclass, so a bare isinstance(int) check would let True through.
                if isinstance(value, bool) or not isinstance(value, (int, float)):
                    continue
                kwargs[field_key] = value
            for schema_key, field_key in (("minLength", "min_length"), ("maxLength", "max_length")):
                if is_str and schema_key in prop:
                    value = prop[schema_key]
                    if isinstance(value, int) and not isinstance(value, bool):
                        kwargs[field_key] = value
            if is_str and isinstance(prop.get("pattern"), str):
                kwargs["pattern"] = prop["pattern"]
            return kwargs

        properties = root.get("properties", {}) if isinstance(root.get("properties"), dict) else {}
        required_fields = set(root.get("required", [])) if isinstance(root.get("required"), list) else set()

        model_name = (
            tool_name.replace("__", "_")
            .title()
            .replace("_", "")
            .replace("-", "")
            + "Args"
        )

        def build(*, with_constraints: bool) -> type:
            fields: dict[str, Any] = {}
            for name, prop in properties.items():
                prop = prop if isinstance(prop, dict) else {}
                python_type = schema_type(prop, f"{tool_name}_{name}", frozenset())
                kwargs = field_kwargs(prop, python_type) if with_constraints else {
                    "description": prop.get("description", "")
                }
                if name in required_fields:
                    fields[name] = (python_type, Field(..., **kwargs))
                else:
                    fields[name] = (Optional[python_type], Field(prop.get("default", None), **kwargs))
            return create_model(model_name, **fields)

        try:
            return build(with_constraints=True)
        except Exception:
            logger.warning(
                "MCP schema build for '%s' failed with constraints; retrying without them",
                tool_name,
                exc_info=True,
            )
            try:
                return build(with_constraints=False)
            except Exception:
                logger.warning(
                    "MCP schema build for '%s' failed entirely; using a permissive schema",
                    tool_name,
                    exc_info=True,
                )
                from pydantic import ConfigDict

                return create_model(model_name, __config__=ConfigDict(extra="allow"))


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


def reslug_legacy_mcp_server_ids() -> List[str]:
    """One-time re-slug of servers to clean, name-derived ids.

    Replaces the old ``<slug>-<uuid6>`` ids with the readable slug (``notion``
    rather than ``notion-a1b2c3``), so the shared model/user tool name reads
    ``mcp__notion__search``. Renames the file, updates the id, and regenerates
    ``registered_tool_names``; collisions disambiguate deterministically
    (``notion-2``). Guarded by a marker so it runs once. It intentionally does
    NOT rewrite per-thread ``enabled_tools`` / ``default_thread_tools``
    references, so a thread that had a re-slugged MCP tool enabled must re-enable
    it, a one-time transitional cost (existing installs are dev/test at this
    stage). Historical checkpoint tool-call names keep their old ids.
    """
    from .mcp_sources import slugify_mcp_name

    registry = get_mcp_server_registry()
    marker = registry.servers_dir / ".reslug.done"
    if marker.exists():
        return []
    logs: List[str] = []
    servers = registry.get_all_servers()
    taken = {s.id for s in servers}
    for defn in servers:
        desired = slugify_mcp_name(defn.name)
        if not desired or desired == defn.id:
            continue
        others = taken - {defn.id}
        new_id = desired
        if new_id in others:
            suffix = 2
            while f"{desired}-{suffix}" in others:
                suffix += 1
            new_id = f"{desired}-{suffix}"
        old_id = defn.id
        if new_id == old_id:
            # Already at its clean/disambiguated id (e.g. a second run after a
            # lost marker): renaming to itself would save-then-delete the file.
            continue
        defn.id = new_id
        defn.registered_tool_names = registered_mcp_tool_names(defn)
        registry.save_server(defn)  # writes new_id.json + stamps approval
        registry.delete_server(old_id)  # removes old_id.json + old tool cache
        taken.discard(old_id)
        taken.add(new_id)
        logs.append(f"Re-slugged MCP server '{old_id}' -> '{new_id}'")
    try:
        marker.write_text("", encoding="utf-8")
    except OSError as exc:
        logs.append(f"Failed to write MCP re-slug marker: {exc}")
    return logs
