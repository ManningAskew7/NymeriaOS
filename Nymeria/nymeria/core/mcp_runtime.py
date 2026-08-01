"""Managed install planning and runtime preparation for pasted MCP servers.

This layer owns MCP install previews, planning, and local runtime preparation.
`mcp_sources` classifies the source string, and `mcp_installer` parses ready
sources into concrete server definitions. The managed runtime path adds:

- source previews and smart-confirm metadata
- per-server cache/source directories
- npm/PyPI/Git/MCPB/DXT source normalization
- encrypted config values for env vars and bundle user_config fields
- disabled drafts when setup/discovery fails
"""

from __future__ import annotations

import json
import logging
import os
import re
import shutil
import subprocess
import time
import uuid
import zipfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Dict, Iterable, List, Optional, Tuple
from urllib.parse import urlparse

from ..config import get_settings
from ..oom import oom_score_preexec
from ..tools.definitions.mcp_schema import MCPServerDefinition
from . import secrets as nymeria_secrets
from .mcp_installer import parse_mcp_source
from .mcp_sources import (
    MCPInstallError,
    SAFE_STDIO_COMMANDS,
    classify_mcp_source,
    describe_definition,
    extract_install_source,
    new_mcp_server_id,
    package_command_source,
)

logger = logging.getLogger(__name__)

PREVIEW_TTL_SECONDS = 60 * 30
SECRET_NAME_RE = re.compile(r"(api[_-]?key|token|secret|password|credential|auth)", re.I)
SECRET_VALUE_RE = re.compile(
    r"(?i)(bearer\s+[a-z0-9._~+/=-]{16,}|"
    r"sk-[a-z0-9_-]{16,}|"
    r"xox[baprs]-[a-z0-9-]{16,}|"
    r"gh[pousr]_[a-z0-9_]{16,}|"
    r"[a-z0-9_=-]{32,})"
)
SHELL_META_RE = re.compile(r"[;&|`$<>]")


@dataclass
class MCPInstallPlan:
    source_type: str
    runtime_type: str
    risk_level: str
    confirmation_required: bool
    parsed_summary: str
    command_preview: str = ""
    warnings: List[str] = field(default_factory=list)
    required_config: List[Dict[str, Any]] = field(default_factory=list)
    missing_config: List[Dict[str, Any]] = field(default_factory=list)
    setup_hint: str = ""
    source_url: str = ""
    bundle_path: str = ""
    preview_token: str = ""
    can_install: bool = True
    candidate_id: str = ""
    credential_requirements: List[Dict[str, Any]] = field(default_factory=list)
    risk_signals: List[Dict[str, Any]] = field(default_factory=list)
    install_steps: List[Dict[str, Any]] = field(default_factory=list)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "source_type": self.source_type,
            "runtime_type": self.runtime_type,
            "risk_level": self.risk_level,
            "confirmation_required": self.confirmation_required,
            "parsed_summary": self.parsed_summary,
            "command_preview": self.command_preview,
            "warnings": self.warnings,
            "required_config": self.required_config,
            "missing_config": self.missing_config,
            "setup_hint": self.setup_hint,
            "source_url": self.source_url,
            "bundle_path": self.bundle_path,
            "preview_token": self.preview_token,
            "can_install": self.can_install,
            "candidate_id": self.candidate_id,
            "credential_requirements": self.credential_requirements,
            "risk_signals": self.risk_signals,
            "install_steps": self.install_steps,
        }

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "MCPInstallPlan":
        return cls(
            source_type=data.get("source_type", "stdio"),
            runtime_type=data.get("runtime_type", "direct"),
            risk_level=data.get("risk_level", "low"),
            confirmation_required=bool(data.get("confirmation_required", False)),
            parsed_summary=data.get("parsed_summary", ""),
            command_preview=data.get("command_preview", ""),
            warnings=list(data.get("warnings") or []),
            required_config=list(data.get("required_config") or []),
            missing_config=list(data.get("missing_config") or []),
            setup_hint=data.get("setup_hint", ""),
            source_url=data.get("source_url", ""),
            bundle_path=data.get("bundle_path", ""),
            preview_token=data.get("preview_token", ""),
            can_install=bool(data.get("can_install", True)),
            candidate_id=data.get("candidate_id", ""),
            credential_requirements=list(data.get("credential_requirements") or []),
            risk_signals=list(data.get("risk_signals") or []),
            install_steps=list(data.get("install_steps") or []),
        )


@dataclass
class MCPInstallCandidate:
    """One non-executing MCP install candidate produced by preview analysis."""

    id: str
    title: str
    source: str
    definition: MCPServerDefinition
    plan: MCPInstallPlan

    def to_dict(self) -> Dict[str, Any]:
        return {
            "id": self.id,
            "title": self.title,
            "source": self.source,
            "server": self.definition.model_dump(mode="json"),
            "plan": self.plan.to_dict(),
            "credential_requirements": self.plan.credential_requirements,
            "risk_signals": self.plan.risk_signals,
            "install_steps": self.plan.install_steps,
            "can_install": self.plan.can_install,
        }

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "MCPInstallCandidate":
        plan = MCPInstallPlan.from_dict(data.get("plan") or {})
        candidate_id = str(data.get("id") or plan.candidate_id or "")
        plan.candidate_id = candidate_id
        return cls(
            id=candidate_id,
            title=str(data.get("title") or candidate_id),
            source=str(data.get("source") or ""),
            definition=MCPServerDefinition(**(data.get("server") or data.get("definition") or {})),
            plan=plan,
        )


def _runtime_base() -> Path:
    path = get_settings().data_dir / "mcp_runtimes"
    path.mkdir(parents=True, exist_ok=True)
    return path


def _preview_base() -> Path:
    path = get_settings().data_dir / "mcp_install_previews"
    path.mkdir(parents=True, exist_ok=True)
    return path


def _runtime_dir(server_id: str) -> Path:
    path = _runtime_base() / server_id
    path.mkdir(parents=True, exist_ok=True)
    return path


def _preview_path(token: str) -> Path:
    return _preview_base() / f"{token}.json"


def cleanup_expired_previews() -> None:
    cutoff = time.time() - PREVIEW_TTL_SECONDS
    for path in _preview_base().glob("*.json"):
        try:
            if path.stat().st_mtime < cutoff:
                path.unlink()
        except OSError:
            pass  # file may already be removed


def save_preview(
    defn: MCPServerDefinition,
    plan: MCPInstallPlan,
    *,
    source: str = "",
    candidates: Optional[List[MCPInstallCandidate]] = None,
) -> str:
    cleanup_expired_previews()
    token = uuid.uuid4().hex
    plan.preview_token = token
    if not plan.candidate_id:
        plan.candidate_id = "candidate-1"
    stored_source = _preview_source_for_definition(defn, source)
    if candidates is None:
        candidates = [
            MCPInstallCandidate(
                id=plan.candidate_id,
                title=defn.name,
                source=stored_source,
                definition=defn,
                plan=plan,
            )
        ]
    else:
        for candidate in candidates:
            candidate.source = _preview_source_for_definition(candidate.definition, candidate.source)
        stored_source = candidates[0].source if len(candidates) == 1 else ""
    for candidate in candidates:
        candidate.plan.preview_token = token
        if not candidate.plan.candidate_id:
            candidate.plan.candidate_id = candidate.id
    payload = {
        "source": stored_source,
        "definition": defn.model_dump(mode="json"),
        "plan": plan.to_dict(),
        "candidates": [candidate.to_dict() for candidate in candidates],
        "created_at": time.time(),
    }
    _preview_path(token).write_text(json.dumps(payload, indent=2), encoding="utf-8")
    return token


def load_preview(
    token: str,
    *,
    candidate_id: Optional[str] = None,
) -> Tuple[str, MCPServerDefinition, MCPInstallPlan]:
    if not re.fullmatch(r"[a-f0-9]{32}", token or ""):
        raise MCPInstallError("invalid preview token")
    path = _preview_path(token)
    if not path.exists():
        raise MCPInstallError("preview expired or not found")
    payload = json.loads(path.read_text(encoding="utf-8"))
    if time.time() - float(payload.get("created_at", 0)) > PREVIEW_TTL_SECONDS:
        path.unlink(missing_ok=True)
        raise MCPInstallError("preview expired")
    raw_candidates = payload.get("candidates") or []
    if raw_candidates:
        candidates = [MCPInstallCandidate.from_dict(item) for item in raw_candidates]
        if candidate_id:
            selected = next((item for item in candidates if item.id == candidate_id), None)
            if selected is None:
                raise MCPInstallError(f"preview candidate not found: {candidate_id}")
        elif len(candidates) == 1:
            selected = candidates[0]
        else:
            raise MCPInstallError("candidate_id is required when preview has multiple candidates")
        defn = selected.definition
        plan = selected.plan
    else:
        defn = MCPServerDefinition(**payload["definition"])
        plan = MCPInstallPlan.from_dict(payload["plan"])
    plan.preview_token = token
    if candidate_id:
        plan.candidate_id = candidate_id
    return payload.get("source", ""), defn, plan


def load_preview_candidates(token: str) -> List[MCPInstallCandidate]:
    if not re.fullmatch(r"[a-f0-9]{32}", token or ""):
        raise MCPInstallError("invalid preview token")
    path = _preview_path(token)
    if not path.exists():
        raise MCPInstallError("preview expired or not found")
    payload = json.loads(path.read_text(encoding="utf-8"))
    if time.time() - float(payload.get("created_at", 0)) > PREVIEW_TTL_SECONDS:
        path.unlink(missing_ok=True)
        raise MCPInstallError("preview expired")
    raw_candidates = payload.get("candidates") or []
    if raw_candidates:
        return [MCPInstallCandidate.from_dict(item) for item in raw_candidates]
    defn = MCPServerDefinition(**payload["definition"])
    plan = MCPInstallPlan.from_dict(payload["plan"])
    plan.preview_token = token
    if not plan.candidate_id:
        plan.candidate_id = "candidate-1"
    return [
        MCPInstallCandidate(
            id=plan.candidate_id,
            title=defn.name,
            source=payload.get("source", ""),
            definition=defn,
            plan=plan,
        )
    ]


def consume_preview(token: str) -> None:
    if token:
        _preview_path(token).unlink(missing_ok=True)


def _is_secret_name(name: str) -> bool:
    return bool(SECRET_NAME_RE.search(name or ""))


def _is_secret_value(value: str) -> bool:
    if not value or value.startswith("${credential:"):
        return False
    if _credential_ref(value):
        return False
    if value.startswith("${env:") and value.endswith("}"):
        return _is_secret_name(value[6:-1])
    return bool(SECRET_VALUE_RE.search(value.strip()))


def _credential_ref(value: str) -> bool:
    return isinstance(value, str) and re.search(r"\$\{credential:[^}]+\}", value) is not None


def _redact_secret_like_text(text: str) -> str:
    if not text:
        return ""
    redacted = SECRET_VALUE_RE.sub("${credential:redacted}", text)
    env_assignment = re.compile(
        r"(?i)\b([A-Z_][A-Z0-9_]*(?:API[_-]?KEY|TOKEN|SECRET|PASSWORD|CREDENTIAL|AUTH)[A-Z0-9_]*)="
        r"(\"[^\"]*\"|'[^']*'|[^\s,}]+)"
    )
    return env_assignment.sub(r"\1=${credential:redacted}", redacted)


def _preview_source_for_definition(defn: MCPServerDefinition, fallback: str = "") -> str:
    return _redact_secret_like_text(defn.original_source or fallback)


def _credential_requirements_from_definition(
    defn: MCPServerDefinition,
    required_config: Iterable[Dict[str, Any]],
) -> List[Dict[str, Any]]:
    requirements: Dict[str, Dict[str, Any]] = {}

    def add(source: str, key: str, value: str, *, required: bool, sensitive: bool) -> None:
        if not sensitive and not required:
            return
        rid = f"{source}:{key}"
        requirements[rid] = {
            "id": rid,
            "name": key,
            "field": key,
            "source": source,
            "label": key,
            "required": required,
            "sensitive": sensitive,
            "provided": bool(value) and not value.startswith("${env:"),
            "uses_credential_ref": _credential_ref(value),
        }

    for key, value in (defn.env_vars or {}).items():
        value_str = str(value or "")
        add(
            "env",
            str(key),
            value_str,
            required=value_str == "" or (value_str.startswith("${env:") and _is_secret_name(str(key))),
            sensitive=_is_secret_name(str(key)) or _is_secret_value(value_str),
        )
    for key, value in (defn.headers or {}).items():
        value_str = str(value or "")
        add(
            "header",
            str(key),
            value_str,
            required=value_str == "" or (value_str.startswith("${env:") and _is_secret_name(str(key))),
            sensitive=_is_secret_name(str(key)) or _is_secret_value(value_str),
        )
    for required_field in required_config:
        source = str(required_field.get("source") or "env")
        key = str(
            required_field.get("header_name")
            or required_field.get("env_name")
            or required_field.get("name")
            or ""
        )
        if not key:
            continue
        rid = f"{source}:{key}"
        requirements[rid] = {
            **requirements.get(rid, {}),
            "id": rid,
            "name": str(required_field.get("name") or key),
            "field": key,
            "source": source,
            "label": required_field.get("label") or key,
            "description": required_field.get("description") or "",
            "required": bool(required_field.get("required", True)),
            "sensitive": bool(required_field.get("sensitive", False)) or _is_secret_name(key),
            "provided": False,
            "uses_credential_ref": False,
        }
    return list(requirements.values())


def _risk_signal(
    signal_id: str,
    label: str,
    severity: str,
    description: str,
    *,
    requires_confirmation: bool = False,
) -> Dict[str, Any]:
    return {
        "id": signal_id,
        "label": label,
        "severity": severity,
        "description": description,
        "requires_confirmation": requires_confirmation,
    }


def _risk_signals_for_plan(
    defn: MCPServerDefinition,
    *,
    source_type: str,
    runtime_type: str,
    warnings: Iterable[str],
) -> List[Dict[str, Any]]:
    signals: List[Dict[str, Any]] = []
    parsed = urlparse(getattr(defn, "url", "") or getattr(defn, "original_source", "") or "")
    host = parsed.hostname or ""
    if source_type in {"npm", "pypi"} or runtime_type in {"npx", "uvx"}:
        signals.append(_risk_signal(
            "package_manager",
            "Package manager runtime",
            "medium",
            "Install will run package code through npx or uvx.",
            requires_confirmation=True,
        ))
    if source_type in {"git", "bundle_url", "bundle_upload"}:
        signals.append(_risk_signal(
            "downloaded_code",
            "Downloaded code",
            "medium",
            "Install downloads or unpacks code before running an MCP server.",
            requires_confirmation=True,
        ))
    if host:
        signals.append(_risk_signal(
            "source_host",
            "Source host",
            "low",
            f"Source host: {host}",
        ))
    if defn.transport == "stdio":
        basename = Path(defn.server_command).name.lower()
        if defn.server_command:
            known = basename in SAFE_STDIO_COMMANDS
            signals.append(_risk_signal(
                "known_launcher" if known else "unknown_launcher",
                "Known launcher" if known else "Unknown launcher",
                "low" if known else "high",
                f"Command launcher: {defn.server_command}",
                requires_confirmation=not known,
            ))
        if any(str(a).startswith(("/", "~", ".")) for a in defn.server_args):
            signals.append(_risk_signal(
                "local_path",
                "Local path access",
                "medium",
                "Command references a local path on the Nymeria host.",
                requires_confirmation=True,
            ))
    if any("shell metacharacters" in warning.lower() for warning in warnings):
        signals.append(_risk_signal(
            "shell_metacharacters",
            "Shell metacharacters",
            "high",
            "Command contains shell metacharacters and needs manual review.",
            requires_confirmation=True,
        ))
    return signals


def _install_steps_for_plan(source_type: str, runtime_type: str) -> List[Dict[str, Any]]:
    steps = [{"id": "approve", "label": "Approval recorded", "runs_code": False}]
    if source_type in {"git", "bundle_url", "bundle_upload"}:
        steps.append({"id": "prepare_source", "label": "Download or unpack source", "runs_code": True})
    elif runtime_type in {"npx", "uvx"}:
        steps.append({"id": "prepare_cache", "label": "Prepare isolated package cache", "runs_code": False})
    else:
        steps.append({"id": "prepare_runtime", "label": "Prepare runtime config", "runs_code": False})
    steps.append({"id": "discover", "label": "Start server and discover tools", "runs_code": True})
    return steps


def _strip_plaintext_secrets_from_definition(
    defn: MCPServerDefinition,
) -> Tuple[MCPServerDefinition, List[Dict[str, Any]]]:
    required: List[Dict[str, Any]] = []
    redactions: List[str] = []

    env_vars = dict(defn.env_vars or {})
    for key, raw_value in list(env_vars.items()):
        value = str(raw_value or "")
        if _credential_ref(value):
            continue
        if _is_secret_name(key) or _is_secret_value(value):
            if value and not (value.startswith("${env:") and value.endswith("}")):
                redactions.append(value)
            env_vars[key] = ""
            required.append({
                "name": key,
                "env_name": key,
                "label": key,
                "description": f"Value for {key}",
                "required": True,
                "sensitive": True,
                "source": "env",
            })

    headers = dict(defn.headers or {})
    for key, raw_value in list(headers.items()):
        value = str(raw_value or "")
        if _credential_ref(value):
            continue
        if _is_secret_name(key) or _is_secret_value(value):
            prefix, secret_value = _strip_auth_prefix(value)
            if secret_value and not (secret_value.startswith("${env:") and secret_value.endswith("}")):
                redactions.append(secret_value)
            headers[key] = ""
            required.append({
                "name": key,
                "header_name": key,
                "label": key,
                "description": f"HTTP header value for {key}",
                "required": True,
                "sensitive": True,
                "source": "header",
                "auth_prefix": prefix,
            })

    if redactions and defn.original_source:
        redacted_source = defn.original_source
        for literal in sorted(set(redactions), key=len, reverse=True):
            redacted_source = redacted_source.replace(literal, "${credential:redacted}")
        defn.original_source = redacted_source

    defn.env_vars = env_vars
    defn.headers = headers
    return defn, required


def _required_field_key(field: Dict[str, Any]) -> Tuple[str, str]:
    source = str(field.get("source") or "env")
    name = str(
        field.get("header_name")
        or field.get("env_name")
        or field.get("name")
        or ""
    )
    return source, name


def _merge_required_config(
    first: Iterable[Dict[str, Any]],
    second: Iterable[Dict[str, Any]],
) -> List[Dict[str, Any]]:
    merged: Dict[Tuple[str, str], Dict[str, Any]] = {}
    order: List[Tuple[str, str]] = []
    for required_field in [*first, *second]:
        key = _required_field_key(required_field)
        if not key[1]:
            continue
        if key not in merged:
            order.append(key)
            merged[key] = dict(required_field)
            continue
        merged[key] = {**merged[key], **required_field}
    return [merged[key] for key in order]


def _command_preview(defn: MCPServerDefinition) -> str:
    if defn.transport == "http":
        return defn.url
    return " ".join([defn.server_command, *defn.server_args]).strip()


def _base_plan(
    defn: MCPServerDefinition,
    *,
    source_type: str,
    runtime_type: str,
    risk_level: str = "low",
    confirmation_required: bool = False,
    warnings: Optional[List[str]] = None,
    required_config: Optional[List[Dict[str, Any]]] = None,
    source_url: str = "",
    bundle_path: str = "",
) -> Tuple[MCPServerDefinition, MCPInstallPlan]:
    existing_required = list(required_config or [])
    defn, secret_required = _strip_plaintext_secrets_from_definition(defn)
    required_fields = _merge_required_config(existing_required, secret_required)
    plan = MCPInstallPlan(
        source_type=source_type,
        runtime_type=runtime_type,
        risk_level=risk_level,
        confirmation_required=confirmation_required,
        parsed_summary=describe_definition(defn),
        command_preview=_command_preview(defn),
        warnings=warnings or [],
        required_config=required_fields,
        missing_config=[],
        source_url=source_url,
        bundle_path=bundle_path,
    )
    plan.missing_config = [f for f in plan.required_config if f.get("required", True)]
    plan.credential_requirements = _credential_requirements_from_definition(defn, plan.required_config)
    plan.risk_signals = _risk_signals_for_plan(
        defn,
        source_type=source_type,
        runtime_type=runtime_type,
        warnings=plan.warnings,
    )
    if any(signal.get("requires_confirmation") for signal in plan.risk_signals):
        plan.confirmation_required = True
        severity_order = {"low": 0, "medium": 1, "high": 2}
        highest = max(
            (str(signal.get("severity") or "low") for signal in plan.risk_signals),
            key=lambda level: severity_order.get(level, 0),
            default=risk_level,
        )
        if severity_order.get(highest, 0) > severity_order.get(plan.risk_level, 0):
            plan.risk_level = highest
    plan.install_steps = _install_steps_for_plan(source_type, runtime_type)
    defn.source_type = source_type
    defn.runtime_type = runtime_type
    defn.parsed_summary = plan.parsed_summary
    defn.install_plan = plan.to_dict()
    defn.missing_config = plan.missing_config
    defn.credential_requirements = plan.credential_requirements
    defn.risk_signals = plan.risk_signals
    defn.risk_level = plan.risk_level
    defn.confirmation_required = plan.confirmation_required
    return defn, plan


def _required_config_from_env(env_vars: Dict[str, str]) -> List[Dict[str, Any]]:
    fields: List[Dict[str, Any]] = []
    for key, value in env_vars.items():
        missing = value == ""
        env_ref = None
        if isinstance(value, str) and value.startswith("${env:") and value.endswith("}"):
            env_ref = value[6:-1]
            missing = not bool(os.environ.get(env_ref))
        if missing:
            field_name = env_ref or key
            fields.append({
                "name": field_name,
                "env_name": key,
                "label": field_name,
                "description": f"Value for {key}",
                "required": True,
                "sensitive": _is_secret_name(field_name) or _is_secret_name(key),
                "source": "env",
            })
    return fields


def _required_config_from_headers(headers: Dict[str, str]) -> List[Dict[str, Any]]:
    fields: List[Dict[str, Any]] = []
    for key, value in headers.items():
        missing = value == ""
        header_ref = None
        if isinstance(value, str) and value.startswith("${env:") and value.endswith("}"):
            header_ref = value[6:-1]
            missing = not bool(os.environ.get(header_ref))
        if missing or (_is_secret_name(key) and isinstance(value, str) and value.startswith("${env:")):
            field_name = header_ref or key
            fields.append({
                "name": field_name,
                "header_name": key,
                "label": field_name,
                "description": f"HTTP header value for {key}",
                "required": True,
                "sensitive": _is_secret_name(field_name) or _is_secret_name(key),
                "source": "header",
            })
    return fields


def _risk_for_command(command: str, args: List[str]) -> Tuple[str, bool, List[str]]:
    warnings: List[str] = []
    basename = Path(command).name.lower()
    joined = " ".join([command, *args])
    confirmation = False
    risk = "low"
    if basename not in SAFE_STDIO_COMMANDS:
        confirmation = True
        risk = "high"
        warnings.append(f"Command '{command}' is not a known MCP launcher.")
    if SHELL_META_RE.search(joined):
        confirmation = True
        risk = "high"
        warnings.append("Command contains shell metacharacters and requires review.")
    if args and any(str(a).startswith(("/", "~", ".")) for a in args):
        confirmation = True
        risk = "medium" if risk == "low" else risk
        warnings.append("Command references a local path; it must exist where Nymeria runs.")
    return risk, confirmation, warnings


def _json_server_sources(blob: str, *, name: Optional[str] = None) -> List[Tuple[str, str]]:
    try:
        data = json.loads(blob)
    except Exception:
        return []
    if not isinstance(data, dict):
        return []

    server_maps: List[Tuple[str, Any]] = []
    for key in ("mcpServers", "mcp_servers", "servers"):
        value = data.get(key)
        if isinstance(value, dict):
            server_maps.extend((str(server_name), entry) for server_name, entry in value.items())
            break
    if not server_maps and isinstance(data.get("server"), dict):
        server_maps.append((name or data.get("name") or "pasted", data["server"]))
    if not server_maps and ("command" in data or "url" in data):
        server_maps.append((name or data.get("name") or "pasted", data))

    out: List[Tuple[str, str]] = []
    for server_name, entry in server_maps:
        if not isinstance(entry, dict):
            continue
        display_name = name if name and len(server_maps) == 1 else server_name
        out.append((
            str(display_name),
            json.dumps({"mcpServers": {str(display_name): entry}}, separators=(",", ":")),
        ))
    return out


def _json_blobs_from_source(source: str) -> List[str]:
    stripped = source.strip()
    blobs: List[str] = []
    if stripped.startswith("{"):
        blobs.append(stripped)
    for fence in re.finditer(r"```(?:json|javascript|js|toml|bash|sh)?\s*\n(.*?)```", source, re.S | re.I):
        body = fence.group(1).strip()
        if body.startswith("{"):
            blobs.append(body)
    if "{" in source and "}" in source:
        candidate = source[source.find("{") : source.rfind("}") + 1].strip()
        if candidate not in blobs:
            blobs.append(candidate)
    return blobs


def _line_install_sources(source: str) -> List[str]:
    candidates: List[str] = []
    for raw_line in source.splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or line.startswith("{"):
            continue
        if line.startswith("$"):
            line = line[1:].strip()
        try:
            classification = classify_mcp_source(line)
        except MCPInstallError:
            continue
        if classification.kind in {"stdio", "http", "npm", "pypi", "git", "bundle_url", "registry"}:
            if classification.kind != "stdio" or any(
                token in line.split() for token in SAFE_STDIO_COMMANDS
            ):
                candidates.append(line)
    return candidates


def analyze_text_source(source: str, *, name: Optional[str] = None) -> List[MCPInstallCandidate]:
    """Return one or more non-executing install candidates for pasted source text."""
    raw_candidates: List[Tuple[str, str]] = []

    for blob in _json_blobs_from_source(source):
        raw_candidates.extend(_json_server_sources(blob, name=name))

    for line in _line_install_sources(source):
        raw_candidates.append((name or "", line))

    if not raw_candidates:
        extracted = extract_install_source(source)
        raw_candidates.append((name or "", extracted))

    seen: set[str] = set()
    candidates: List[MCPInstallCandidate] = []
    for title, candidate_source in raw_candidates:
        if candidate_source in seen:
            continue
        seen.add(candidate_source)
        defn, plan = plan_text_source(candidate_source, name=title or name)
        candidate_id = f"candidate-{len(candidates) + 1}"
        plan.candidate_id = candidate_id
        defn.install_plan = plan.to_dict()
        candidates.append(
            MCPInstallCandidate(
                id=candidate_id,
                title=title or defn.name,
                source=candidate_source,
                definition=defn,
                plan=plan,
            )
        )

    if not candidates:
        raise MCPInstallError("no install candidates found")
    return candidates


def plan_text_source(source: str, *, name: Optional[str] = None) -> Tuple[MCPServerDefinition, MCPInstallPlan]:
    stripped = extract_install_source(source)
    classification = classify_mcp_source(stripped)

    if classification.kind == "npm":
        defn = parse_mcp_source(
            package_command_source(classification),
            name=name or classification.value,
        )
        return _base_plan(defn, source_type="npm", runtime_type="npx")
    if classification.kind == "pypi":
        defn = parse_mcp_source(
            package_command_source(classification),
            name=name or classification.value,
        )
        return _base_plan(defn, source_type="pypi", runtime_type="uvx")
    if classification.kind == "git":
        value = classification.value
        display = name or Path(urlparse(value).path).stem.removesuffix(".git") or "mcp-git"
        defn = MCPServerDefinition(
            id=new_mcp_server_id(display),
            name=display,
            transport="stdio",
            server_command="",
            server_args=[],
            install_status="draft",
            original_source=stripped,
        )
        return _base_plan(
            defn,
            source_type="git",
            runtime_type="git",
            risk_level="medium",
            confirmation_required=True,
            warnings=["Nymeria will clone this repository and run its MCP server entrypoint."],
            source_url=value,
        )
    if classification.kind == "bundle_url":
        value = classification.value
        display = name or Path(urlparse(value).path).stem or "mcp-bundle"
        defn = MCPServerDefinition(
            id=new_mcp_server_id(display),
            name=display,
            transport="stdio",
            server_command="",
            server_args=[],
            install_status="draft",
            original_source=stripped,
        )
        return _base_plan(
            defn,
            source_type="bundle_url",
            runtime_type="bundle",
            risk_level="medium",
            confirmation_required=True,
            warnings=["Nymeria will download and unpack this MCP bundle before running it."],
            source_url=value,
        )

    defn = parse_mcp_source(stripped, name=name)
    defn.original_source = stripped
    if defn.transport == "http":
        return _base_plan(
            defn,
            source_type="http",
            runtime_type="http",
            required_config=_required_config_from_headers(defn.headers),
        )

    required = _required_config_from_env(defn.env_vars)
    basename = Path(defn.server_command).name.lower()
    runtime = "direct"
    if basename == "uvx":
        runtime = "uvx"
    elif basename == "npx":
        runtime = "npx"
    elif basename in {"python", "python3"}:
        runtime = "python"
    elif basename in {"node", "npm"}:
        runtime = "node"
    risk, confirmation, warnings = _risk_for_command(defn.server_command, defn.server_args)
    return _base_plan(
        defn,
        source_type="json" if stripped.startswith("{") else ("registry" if classification.kind == "registry" else "stdio"),
        runtime_type=runtime,
        risk_level=risk,
        confirmation_required=confirmation,
        warnings=warnings,
        required_config=required,
    )


def plan_bundle_file(bundle_path: Path, *, original_name: str = "", name: Optional[str] = None) -> Tuple[MCPServerDefinition, MCPInstallPlan]:
    manifest = _read_manifest_from_zip(bundle_path)
    display = name or manifest.get("display_name") or manifest.get("name") or Path(original_name or bundle_path.name).stem
    defn = MCPServerDefinition(
        id=new_mcp_server_id(str(display)),
        name=str(display),
        description=manifest.get("description", "") or "",
        transport="stdio",
        server_command="",
        server_args=[],
        install_status="draft",
        original_source=original_name or str(bundle_path),
    )
    required_config = _required_config_from_manifest(manifest)
    return _base_plan(
        defn,
        source_type="bundle_upload",
        runtime_type="bundle",
        risk_level="medium",
        confirmation_required=True,
        warnings=["Nymeria will unpack this MCP bundle and run its declared entrypoint."],
        required_config=required_config,
        bundle_path=str(bundle_path),
    )


def _read_manifest_from_zip(bundle_path: Path) -> Dict[str, Any]:
    try:
        with zipfile.ZipFile(bundle_path) as zf:
            with zf.open("manifest.json") as fh:
                return json.loads(fh.read().decode("utf-8"))
    except KeyError as e:
        raise MCPInstallError("bundle does not contain manifest.json") from e
    except Exception as e:
        raise MCPInstallError(f"could not read bundle manifest: {e}") from e


def _required_config_from_manifest(manifest: Dict[str, Any]) -> List[Dict[str, Any]]:
    raw = manifest.get("user_config") or {}
    fields: List[Dict[str, Any]] = []
    if not isinstance(raw, dict):
        return fields
    for name, cfg in raw.items():
        if not isinstance(cfg, dict):
            continue
        required = bool(cfg.get("required", True))
        fields.append({
            "name": name,
            "env_name": name,
            "label": cfg.get("title") or cfg.get("label") or name,
            "description": cfg.get("description", ""),
            "required": required,
            "sensitive": bool(cfg.get("sensitive", False)) or _is_secret_name(name),
            "type": cfg.get("type", "string"),
            "default": cfg.get("default"),
            "source": "user_config",
        })
    return fields


def _safe_credential_segment(value: str) -> str:
    safe = re.sub(r"[^A-Za-z0-9_-]+", "_", value).strip("_")
    return safe[:48] or "field"


def _mcp_credential_id(server_id: str, source: str, field: str) -> str:
    base = _safe_credential_segment(f"{server_id}_{source}_{field}")
    return f"cred_mcp_{base}"[:120]


def _credential_ref_for_secret(
    *,
    defn: MCPServerDefinition,
    source: str,
    field: str,
    value: Optional[str],
    user_id: Optional[str],
    actor_user_id: Optional[str],
    missing: List[Dict[str, Any]],
    field_def: Optional[Dict[str, Any]] = None,
) -> str:
    from .credential_vault import get_credential_vault_repo

    field_def = field_def or {}
    credential_id = _mcp_credential_id(defn.id, source, field)
    target = f"mcp_server:{defn.id}"
    status = "active" if value else "pending_setup"
    secret_fields = {"value": value} if value else {}
    if value and not nymeria_secrets.has_secrets_key():
        status = "pending_setup"
        secret_fields = {}
        missing.append({
            **field_def,
            "name": field_def.get("name") or field,
            "source": source,
            "credential_id": credential_id,
            "error": "NYMERIA_SECRETS_KEY is required before Nymeria can store this secret",
        })
    elif not value:
        missing.append({
            **field_def,
            "name": field_def.get("name") or field,
            "source": source,
            "credential_id": credential_id,
        })

    repo = get_credential_vault_repo()
    repo.upsert_credential(
        credential_id=credential_id,
        owner_type="user" if user_id else "system",
        owner_user_id=user_id,
        name=f"{defn.name} {field}",
        provider="mcp",
        kind="secret",
        secret_fields=secret_fields,
        metadata={
            "server_id": defn.id,
            "server_name": defn.name,
            "source": source,
            "field": field,
        },
        allowed_targets=[target],
        status=status,
        actor_user_id=actor_user_id or user_id,
    )
    repo.bind_credential(
        credential_id,
        target_type="mcp_server",
        target_id=defn.id,
        binding_name=field,
        actor_user_id=actor_user_id or user_id,
    )
    return f"${{credential:{credential_id}.value}}"


def _binding_ref(
    binding: Any,
    *,
    defn: MCPServerDefinition,
    source: str,
    field: str,
    user_id: Optional[str],
) -> Optional[str]:
    if not binding:
        return None
    credential_id = binding.get("credential_id") if isinstance(binding, dict) else str(binding)
    field_name = binding.get("field", "value") if isinstance(binding, dict) else "value"
    if not credential_id:
        return None
    from .credential_vault import (
        CREDENTIAL_FIELD_RE,
        CREDENTIAL_ID_RE,
        get_credential_vault_repo,
    )

    # Both parts are caller-supplied and get interpolated into the ref below,
    # so each has to be legal on its own. Without this, a field of
    # ``value} ${credential:other-id.cache_json`` closes the ref early and
    # appends a SECOND one. The sweep then skips the value (it already matches
    # ``_credential_ref``), so it is stored verbatim, and MCP resolves both at
    # spawn as SYSTEM_ACTOR. That turns a binding for a credential you may name
    # into a read of one you may not.
    if not CREDENTIAL_ID_RE.fullmatch(str(credential_id or "")):
        raise ValueError(
            f"Invalid credential id {credential_id!r} in binding for {field}"
        )
    if not CREDENTIAL_FIELD_RE.fullmatch(str(field_name or "")):
        raise ValueError(
            f"Invalid credential field {field_name!r} in binding for {field}"
        )

    # NB deliberately NOT owner-checked here. Every surface that reaches this
    # (the REST install/create/update/retry routes and the agent's
    # ``install_mcp_server``) is admin-only, and an admin already reads every
    # credential legitimately via ``actor_is_admin``, so a check here would
    # enforce nothing while breaking an admin configuring a server on a user's
    # behalf. It becomes load-bearing the moment MCP management stops being
    # admin-only; see the security backlog entry rather than re-deriving it.
    repo = get_credential_vault_repo()
    repo.bind_credential(
        credential_id,
        target_type="mcp_server",
        target_id=defn.id,
        binding_name=field,
        actor_user_id=user_id,
    )
    # The bindings row records the CHOICE; the grant has to be written where the
    # gate actually reads, or this whole surface is dead: MCP resolves as
    # SYSTEM_ACTOR, so `allowed_targets` is the only check on the spawn path,
    # and a bound-but-ungranted credential raises `CredentialAccessDenied` out
    # of `_start_server` with nothing pointing back to the bind.
    #
    # Widening on bind is right HERE and wrong in the agent-facing bind tools,
    # which deliberately do not: this surface is an admin saying "use this
    # credential for this server", which is the entire point of the action,
    # whereas there scope belongs to whoever created the credential rather than
    # to whoever references it later. `add_allowed_target` is owner-checked,
    # and `actor_is_admin` is what carries the admin-only fact above into it.
    repo.add_allowed_target(
        credential_id,
        target=f"mcp_server:{defn.id}",
        actor_user_id=user_id,
        actor_is_admin=True,
    )
    return f"${{credential:{credential_id}.{field_name}}}"


def _strip_auth_prefix(value: str) -> Tuple[str, str]:
    match = re.match(r"(?i)^(bearer\s+)(.+)$", value.strip())
    if match:
        return match.group(1), match.group(2)
    return "", value


@dataclass(frozen=True)
class _ApplyConfigContext:
    """Shared inputs/accumulators for the ``apply_config_values`` loop helpers.

    The fields are references to the mutable maps/lists owned by
    ``apply_config_values``; the helpers mutate them in place, so the behavior is
    identical to the previous single-function form. ``supplied_value`` and
    ``supplied_binding`` are the closures over the normalized input dicts.
    """

    defn: MCPServerDefinition
    supplied_value: Callable[..., Optional[str]]
    supplied_binding: Callable[..., Any]
    env_vars: Dict[str, str]
    headers: Dict[str, str]
    values: Dict[str, str]
    missing: List[Dict[str, Any]]
    redacted_literals: List[str]
    user_id: Optional[str]


def _apply_required_field(required_field: Dict[str, Any], ctx: _ApplyConfigContext) -> None:
    """Resolve one ``plan.required_config`` entry into env/header config.

    Writes a plaintext value, a credential reference, or (for a non-sensitive
    required field with no value) appends to ``ctx.missing``.
    """
    name = str(required_field.get("name") or "")
    source = str(required_field.get("source") or "env")
    env_name = str(required_field.get("env_name") or name)
    header_name = str(required_field.get("header_name") or name)
    target_name = header_name if source == "header" else env_name
    auth_prefix = str(required_field.get("auth_prefix") or required_field.get("prefix") or "")
    value = ctx.supplied_value(name, target_name, f"{source}:{target_name}")
    if (value is None or value == "") and required_field.get("default") is not None:
        value = str(required_field["default"])
    if source == "header" and auth_prefix and isinstance(value, str):
        if value.lower().startswith(auth_prefix.lower()):
            value = value[len(auth_prefix):].strip()
    if value == "":
        value = None
    if value is None and required_field.get("required", True):
        if required_field.get("sensitive"):
            ref = _credential_ref_for_secret(
                defn=ctx.defn,
                source=source,
                field=target_name,
                value=None,
                user_id=ctx.user_id,
                actor_user_id=ctx.user_id,
                missing=ctx.missing,
                field_def=required_field,
            )
            if source == "header":
                ctx.headers[target_name] = f"{auth_prefix}{ref}"
            else:
                ctx.env_vars[target_name] = ref
        else:
            ctx.missing.append(required_field)
        return
    if value is None:
        return
    if required_field.get("sensitive"):
        ref = _binding_ref(
            ctx.supplied_binding(name, target_name, f"{source}:{target_name}"),
            defn=ctx.defn,
            source=source,
            field=target_name,
            user_id=ctx.user_id,
        )
        if ref is None:
            ref = _credential_ref_for_secret(
                defn=ctx.defn,
                source=source,
                field=target_name,
                value=value,
                user_id=ctx.user_id,
                actor_user_id=ctx.user_id,
                missing=ctx.missing,
                field_def=required_field,
            )
            if value:
                ctx.redacted_literals.append(value)
        ctx.values[name] = ref
        if source == "header":
            ctx.headers[target_name] = f"{auth_prefix}{ref}"
        else:
            ctx.env_vars[target_name] = ref
    else:
        if source == "header":
            ctx.headers[target_name] = value
        else:
            ctx.env_vars[target_name] = value


def _sweep_sensitive_mapping(source: str, mapping: Dict[str, str], ctx: _ApplyConfigContext) -> None:
    """Convert any remaining secret-looking plaintext in ``mapping`` to refs.

    Runs after the ``required_config`` pass, so it also catches the non-sensitive
    plaintext values that pass wrote which turn out to be secret-looking. Existing
    credential references are left untouched.
    """
    for key, raw_value in list(mapping.items()):
        value = str(raw_value or "")
        if _credential_ref(value):
            continue
        sensitive = _is_secret_name(key) or _is_secret_value(value)
        if not sensitive:
            continue
        ref = _binding_ref(
            ctx.supplied_binding(key, f"{source}:{key}"),
            defn=ctx.defn,
            source=source,
            field=key,
            user_id=ctx.user_id,
        )
        if ref is None:
            secret_value: Optional[str] = value
            prefix = ""
            if value.startswith("${env:") and value.endswith("}"):
                secret_value = ctx.supplied_value(key, f"{source}:{key}")
                if not secret_value:
                    secret_value = None
            elif source == "header":
                prefix, secret_value = _strip_auth_prefix(value)
            if secret_value:
                ctx.redacted_literals.append(secret_value)
            ref = _credential_ref_for_secret(
                defn=ctx.defn,
                source=source,
                field=key,
                value=secret_value,
                user_id=ctx.user_id,
                actor_user_id=ctx.user_id,
                missing=ctx.missing,
                field_def={
                    "name": key,
                    "label": key,
                    "source": source,
                    "required": True,
                    "sensitive": True,
                },
            )
            if source == "header" and prefix:
                ref = f"{prefix}{ref}"
        mapping[key] = ref


def apply_config_values(
    defn: MCPServerDefinition,
    plan: MCPInstallPlan,
    config_values: Optional[Dict[str, str]] = None,
    *,
    credential_values: Optional[Dict[str, str]] = None,
    credential_bindings: Optional[Dict[str, Any]] = None,
    user_id: Optional[str] = None,
) -> Tuple[MCPServerDefinition, List[Dict[str, Any]]]:
    values = config_values if config_values is not None else {}
    secret_values = credential_values or {}
    bindings = credential_bindings or {}
    missing: List[Dict[str, Any]] = []
    env_vars = dict(defn.env_vars or {})
    headers = dict(defn.headers or {})
    redacted_literals: List[str] = []

    def supplied_value(*keys: str) -> Optional[str]:
        for key in keys:
            if key and key in secret_values:
                return secret_values[key]
            if key and key in values:
                return values[key]
        return None

    def supplied_binding(*keys: str) -> Any:
        for key in keys:
            if key and key in bindings:
                return bindings[key]
        return None

    ctx = _ApplyConfigContext(
        defn=defn,
        supplied_value=supplied_value,
        supplied_binding=supplied_binding,
        env_vars=env_vars,
        headers=headers,
        values=values,
        missing=missing,
        redacted_literals=redacted_literals,
        user_id=user_id,
    )

    # The required-config pass must run to completion before the sweep: it can
    # write plaintext non-sensitive values that the sweep then re-scans and
    # converts to credential references.
    for required_field in plan.required_config:
        _apply_required_field(required_field, ctx)

    for source, mapping in (("env", env_vars), ("header", headers)):
        _sweep_sensitive_mapping(source, mapping, ctx)

    defn.env_vars = env_vars
    defn.headers = headers
    # New managed installs store credential references, not legacy encrypted env vars.
    defn.encrypted_env_vars = {}
    defn.missing_config = missing
    defn.credential_requirements = plan.credential_requirements
    if defn.original_source and redacted_literals:
        redacted_source = defn.original_source
        for literal in sorted(set(redacted_literals), key=len, reverse=True):
            if literal:
                redacted_source = redacted_source.replace(literal, "${credential:redacted}")
        defn.original_source = redacted_source
    return defn, missing


def prepare_runtime(
    defn: MCPServerDefinition,
    plan: MCPInstallPlan,
    *,
    config_values: Optional[Dict[str, str]] = None,
    credential_values: Optional[Dict[str, str]] = None,
    credential_bindings: Optional[Dict[str, Any]] = None,
    user_id: Optional[str] = None,
    log_sink: Optional[List[str]] = None,
) -> Tuple[MCPServerDefinition, List[str]]:
    logs: List[str] = log_sink if log_sink is not None else []
    settings = get_settings()
    if (
        (
            plan.source_type in {"git", "bundle_url", "bundle_upload"}
            or plan.runtime_type in {"npx", "uvx"}
        )
        and not getattr(settings, "nymeria_allow_unsandboxed_mcp_install", False)
    ):
        raise MCPInstallError(
            "Managed MCP installs that execute downloaded package code are disabled. "
            "Run them in an external sandbox or set "
            "NYMERIA_ALLOW_UNSANDBOXED_MCP_INSTALL=true only for a trusted "
            "admin maintenance window."
        )
    defn, missing = apply_config_values(
        defn,
        plan,
        config_values,
        credential_values=credential_values,
        credential_bindings=credential_bindings,
        user_id=user_id,
    )
    if missing:
        plan.missing_config = missing
        raise MCPInstallError("missing required MCP configuration")

    runtime_dir = _runtime_dir(defn.id)
    (runtime_dir / "cache").mkdir(exist_ok=True)

    # First matching rule in the ordered `_RUNTIME_PREPARERS` table runs,
    # reproducing the former if-ladder exactly. The order is load-bearing (see
    # the table's comment for why): a dict keyed by (source_type, runtime_type)
    # would mis-route a plan whose two keys each match a different rule.
    prepared_config = config_values or {}
    for matches, handler in _RUNTIME_PREPARERS:
        if matches(plan):
            return handler(defn, plan, runtime_dir, prepared_config, logs)

    logs.append(f"No setup handler for runtime type {plan.runtime_type}; using parsed command.")
    return defn, logs


def _prepare_http(
    defn: MCPServerDefinition,
    plan: MCPInstallPlan,
    runtime_dir: Path,
    config_values: Dict[str, str],
    logs: List[str],
) -> Tuple[MCPServerDefinition, List[str]]:
    logs.append("HTTP MCP server; no local setup required.")
    return defn, logs


def _prepare_bundle_url(
    defn: MCPServerDefinition,
    plan: MCPInstallPlan,
    runtime_dir: Path,
    config_values: Dict[str, str],
    logs: List[str],
) -> Tuple[MCPServerDefinition, List[str]]:
    bundle_path = runtime_dir / "bundle.mcpb"
    _download(plan.source_url, bundle_path)
    plan.bundle_path = str(bundle_path)
    logs.append(f"Downloaded bundle from {plan.source_url}")
    return _prepare_bundle(defn, plan, runtime_dir, config_values, logs)


def _prepare_bundle_upload(
    defn: MCPServerDefinition,
    plan: MCPInstallPlan,
    runtime_dir: Path,
    config_values: Dict[str, str],
    logs: List[str],
) -> Tuple[MCPServerDefinition, List[str]]:
    return _prepare_bundle(defn, plan, runtime_dir, config_values, logs)


def _prepare_git(
    defn: MCPServerDefinition,
    plan: MCPInstallPlan,
    runtime_dir: Path,
    config_values: Dict[str, str],
    logs: List[str],
) -> Tuple[MCPServerDefinition, List[str]]:
    source_dir = runtime_dir / "source"
    if source_dir.exists():
        shutil.rmtree(source_dir)
    _run(["git", "clone", "--depth", "1", plan.source_url, str(source_dir)], logs, timeout=180)
    logs.append(f"Cloned {plan.source_url}")
    return _prepare_source_tree(defn, source_dir, logs)


def _prepare_uvx_cache(
    defn: MCPServerDefinition,
    plan: MCPInstallPlan,
    runtime_dir: Path,
    config_values: Dict[str, str],
    logs: List[str],
) -> Tuple[MCPServerDefinition, List[str]]:
    cache = runtime_dir / "cache" / "uv"
    cache.mkdir(parents=True, exist_ok=True)
    defn.env_vars = {**defn.env_vars, "UV_CACHE_DIR": str(cache)}
    logs.append(f"Prepared uvx cache at {cache}")
    return defn, logs


def _prepare_npx_cache(
    defn: MCPServerDefinition,
    plan: MCPInstallPlan,
    runtime_dir: Path,
    config_values: Dict[str, str],
    logs: List[str],
) -> Tuple[MCPServerDefinition, List[str]]:
    cache = runtime_dir / "cache" / "npm"
    cache.mkdir(parents=True, exist_ok=True)
    defn.env_vars = {**defn.env_vars, "NPM_CONFIG_CACHE": str(cache)}
    logs.append(f"Prepared npx cache at {cache}")
    return defn, logs


def _prepare_direct(
    defn: MCPServerDefinition,
    plan: MCPInstallPlan,
    runtime_dir: Path,
    config_values: Dict[str, str],
    logs: List[str],
) -> Tuple[MCPServerDefinition, List[str]]:
    logs.append("Using direct stdio command; no managed setup required.")
    return defn, logs


# A runtime preparer takes the (defn, plan, runtime_dir, config_values, logs)
# bundle and returns the (possibly mutated) defn plus the threaded logs list.
_RuntimePreparer = Callable[
    [MCPServerDefinition, MCPInstallPlan, Path, Dict[str, str], List[str]],
    Tuple[MCPServerDefinition, List[str]],
]

# Ordered (predicate, handler) rule table consumed by `prepare_runtime`. Order is
# load-bearing: source_type bundle/git arms intentionally precede the uvx/npx
# runtime arms, and `http` precedes everything. Keep this aligned with the
# `_install_steps_for_plan` precedence.
_RUNTIME_PREPARERS: Tuple[Tuple[Callable[[MCPInstallPlan], bool], _RuntimePreparer], ...] = (
    (lambda plan: plan.runtime_type == "http", _prepare_http),
    (lambda plan: plan.source_type == "bundle_url", _prepare_bundle_url),
    (lambda plan: plan.source_type == "bundle_upload", _prepare_bundle_upload),
    (lambda plan: plan.source_type == "git", _prepare_git),
    (lambda plan: plan.runtime_type == "uvx", _prepare_uvx_cache),
    (lambda plan: plan.runtime_type == "npx", _prepare_npx_cache),
    (lambda plan: plan.runtime_type in {"python", "node", "direct"}, _prepare_direct),
)


def _download(url: str, dest: Path) -> None:
    from .http_policy import (
        HTTPPolicyRedirectLimit,
        HTTPPolicyViolation,
        requests_get_with_policy,
    )

    try:
        resp, _redirect_chain, _policy = requests_get_with_policy(
            url,
            stream=True,
            timeout=60,
        )
    except (HTTPPolicyViolation, HTTPPolicyRedirectLimit) as e:
        raise MCPInstallError(f"download blocked by HTTP egress policy: {e}") from e

    with resp:
        if resp.status_code >= 400:
            raise MCPInstallError(f"download failed HTTP {resp.status_code}: {resp.text[:200]}")
        dest.parent.mkdir(parents=True, exist_ok=True)
        with dest.open("wb") as fh:
            for chunk in resp.iter_content(chunk_size=1024 * 1024):
                if chunk:
                    fh.write(chunk)


def _run(cmd: List[str], logs: List[str], *, cwd: Optional[Path] = None, timeout: int = 120) -> None:
    # Install commands (npm/pip/git) run untrusted package build/postinstall
    # scripts, so they get the same deny-by-default env as the runtime exec
    # surfaces (never the API secrets), plus the non-secret network/CA vars a
    # package fetch legitimately needs.
    from ..subprocess_env import NETWORK_RUNTIME_PASSTHROUGH, scrubbed_subprocess_env

    logs.append(f"$ {' '.join(cmd)}")
    # sandbox-gate: unsandboxed - npm/pip/git install, which writes into the
    # runtime dir it is installing to and then reads it back, so the policy
    # would have to grant that dir READ_FILE for files created after the
    # launch, which Landlock cannot express. Needs the runtime dir treated as a
    # creation root. C1-02 follow-up.
    proc = subprocess.run(
        cmd,
        cwd=str(cwd) if cwd else None,
        env=scrubbed_subprocess_env(NETWORK_RUNTIME_PASSTHROUGH),
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        timeout=timeout,
        # An install command (npm/pip, ...) can spike memory; make it the OOM
        # victim rather than the API server (see nymeria/oom.py).
        preexec_fn=oom_score_preexec(),
    )
    output = (proc.stdout or "").strip()
    if output:
        logs.extend(output.splitlines()[-40:])
    if proc.returncode != 0:
        raise MCPInstallError(f"setup command failed ({proc.returncode}): {' '.join(cmd)}")


def _safe_extract_zip(bundle_path: Path, target: Path) -> None:
    if target.exists():
        shutil.rmtree(target)
    target.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(bundle_path) as zf:
        root = target.resolve()
        for member in zf.infolist():
            dest = (target / member.filename).resolve()
            if root not in dest.parents and dest != root:
                raise MCPInstallError("bundle contains unsafe paths")
        zf.extractall(target)


def _prepare_bundle(
    defn: MCPServerDefinition,
    plan: MCPInstallPlan,
    runtime_dir: Path,
    config_values: Dict[str, str],
    logs: Optional[List[str]] = None,
) -> Tuple[MCPServerDefinition, List[str]]:
    logs = logs if logs is not None else []
    bundle_path = Path(plan.bundle_path)
    if not bundle_path.exists():
        raise MCPInstallError("bundle file is missing")
    source_dir = runtime_dir / "source"
    _safe_extract_zip(bundle_path, source_dir)
    manifest = json.loads((source_dir / "manifest.json").read_text(encoding="utf-8"))
    logs.append(f"Unpacked bundle manifest: {manifest.get('name') or defn.name}")

    server = manifest.get("server") or {}
    mcp_config = server.get("mcp_config") or {}
    defn = _apply_bundle_env(defn, plan, mcp_config.get("env") or {}, config_values, source_dir)

    if mcp_config.get("command"):
        command = _replace_config_string(str(mcp_config["command"]), config_values, source_dir)
        args = [
            _replace_config_string(str(a), config_values, source_dir)
            for a in (mcp_config.get("args") or [])
        ]
        defn.server_command = command
        defn.server_args = args
        defn.working_directory = str(source_dir)
        logs.append("Using bundle mcp_config command.")
        return defn, logs

    entry = server.get("entry_point") or server.get("entrypoint") or server.get("entry")
    server_type = (server.get("type") or "").lower()
    if not entry:
        raise MCPInstallError("bundle manifest server.entry_point is missing")
    entry_path = source_dir / str(entry)

    if server_type == "node":
        if not (source_dir / "node_modules").exists() and (source_dir / "package.json").exists():
            _run(["npm", "install", "--omit=dev"], logs, cwd=source_dir, timeout=180)
        defn.server_command = "node"
        defn.server_args = [str(entry_path)]
    elif server_type in {"python", "uv"}:
        venv_python = _ensure_python_env(source_dir, logs)
        defn.server_command = str(venv_python)
        defn.server_args = [str(entry_path)]
    elif server_type == "binary":
        try:
            entry_path.chmod(entry_path.stat().st_mode | 0o111)
        except OSError:
            pass  # chmod may fail on read-only filesystem
        defn.server_command = str(entry_path)
        defn.server_args = []
    else:
        raise MCPInstallError(f"unsupported bundle server type: {server_type or '(missing)'}")

    defn.working_directory = str(source_dir)
    return defn, logs


def _replace_config_string(value: str, config_values: Dict[str, str], source_dir: Path) -> str:
    out = value.replace("${__dirname}", str(source_dir))
    for key, val in config_values.items():
        out = out.replace("${user_config." + key + "}", str(val))
    return out


def _replace_config_tokens(data: Dict[str, Any], config_values: Dict[str, str], source_dir: Path) -> Dict[str, str]:
    out: Dict[str, str] = {}
    for key, value in data.items():
        out[str(key)] = _replace_config_string(str(value), config_values, source_dir)
    return out


def _apply_bundle_env(
    defn: MCPServerDefinition,
    plan: MCPInstallPlan,
    data: Dict[str, Any],
    config_values: Dict[str, str],
    source_dir: Path,
) -> MCPServerDefinition:
    env_vars = dict(defn.env_vars or {})
    sensitive_fields = {
        str(field.get("name")) for field in plan.required_config if field.get("sensitive")
    }
    token_re = re.compile(r"\$\{user_config\.([A-Za-z0-9_-]+)\}")

    for key, raw_value in data.items():
        env_key = str(key)
        value = str(raw_value)
        match = token_re.fullmatch(value.strip())
        if match and match.group(1) in sensitive_fields:
            field_name = match.group(1)
            if field_name in config_values:
                env_vars[env_key] = str(config_values[field_name])
            continue
        env_vars[env_key] = _replace_config_string(value, config_values, source_dir)

    defn.env_vars = env_vars
    return defn


def _prepare_source_tree(
    defn: MCPServerDefinition,
    source_dir: Path,
    logs: Optional[List[str]] = None,
) -> Tuple[MCPServerDefinition, List[str]]:
    logs = logs if logs is not None else []
    if (source_dir / "package.json").exists():
        _run(["npm", "install", "--omit=dev"], logs, cwd=source_dir, timeout=180)
        entry = _node_entrypoint(source_dir)
        if not entry:
            raise MCPInstallError("could not determine Node MCP entrypoint")
        defn.server_command = "node"
        defn.server_args = [str(entry)]
        defn.working_directory = str(source_dir)
        logs.append(f"Detected Node MCP entrypoint {entry}")
        return defn, logs

    if (source_dir / "pyproject.toml").exists() or (source_dir / "requirements.txt").exists():
        venv_python = _ensure_python_env(source_dir, logs)
        script = _python_script_entrypoint(source_dir, venv_python)
        if script and script.exists():
            defn.server_command = str(script)
            defn.server_args = []
            logs.append(f"Detected Python console script {script}")
        else:
            entry = _python_entrypoint(source_dir)
            if not entry:
                raise MCPInstallError("could not determine Python MCP entrypoint")
            defn.server_command = str(venv_python)
            defn.server_args = [str(entry)]
            logs.append(f"Detected Python MCP entrypoint {entry}")
        defn.working_directory = str(source_dir)
        return defn, logs

    raise MCPInstallError("repository does not look like a Node or Python MCP server")


def _node_entrypoint(source_dir: Path) -> Optional[Path]:
    pkg = json.loads((source_dir / "package.json").read_text(encoding="utf-8"))
    bin_field = pkg.get("bin")
    if isinstance(bin_field, str):
        return source_dir / bin_field
    if isinstance(bin_field, dict) and bin_field:
        return source_dir / next(iter(bin_field.values()))
    main = pkg.get("main")
    if main:
        return source_dir / main
    for candidate in ("dist/index.js", "build/index.js", "server/index.js", "index.js"):
        path = source_dir / candidate
        if path.exists():
            return path
    return None


def _ensure_python_env(source_dir: Path, logs: List[str]) -> Path:
    venv_dir = source_dir / ".nymeria-venv"
    python = shutil.which("python3") or shutil.which("python")
    if not python:
        raise MCPInstallError("python is not available")
    if not venv_dir.exists():
        _run([python, "-m", "venv", str(venv_dir)], logs, timeout=180)
    venv_python = venv_dir / ("Scripts/python.exe" if os.name == "nt" else "bin/python")
    if (source_dir / "requirements.txt").exists():
        _run([str(venv_python), "-m", "pip", "install", "-r", "requirements.txt"], logs, cwd=source_dir, timeout=300)
    if (source_dir / "pyproject.toml").exists():
        _run([str(venv_python), "-m", "pip", "install", "-e", "."], logs, cwd=source_dir, timeout=300)
    return venv_python


def _python_script_entrypoint(source_dir: Path, venv_python: Path) -> Optional[Path]:
    try:
        import tomllib
        data = tomllib.loads((source_dir / "pyproject.toml").read_text(encoding="utf-8"))
    except Exception:
        return None
    scripts = ((data.get("project") or {}).get("scripts") or {})
    if not scripts:
        return None
    name = next(iter(scripts.keys()))
    script_dir = venv_python.parent
    return script_dir / (f"{name}.exe" if os.name == "nt" else name)


def _python_entrypoint(source_dir: Path) -> Optional[Path]:
    for candidate in ("server.py", "main.py", "mcp_server.py", "src/server.py", "src/main.py"):
        path = source_dir / candidate
        if path.exists():
            return path
    py_files = sorted(source_dir.glob("*.py"))
    return py_files[0] if py_files else None


def make_failed_draft(
    defn: MCPServerDefinition,
    plan: MCPInstallPlan,
    error: str,
    logs: Optional[List[str]] = None,
) -> MCPServerDefinition:
    defn.enabled = False
    defn.install_status = "failed"
    defn.last_error = error
    defn.install_logs = list(logs or [])
    defn.install_plan = plan.to_dict()
    defn.source_type = plan.source_type
    defn.runtime_type = plan.runtime_type
    defn.risk_level = plan.risk_level
    defn.confirmation_required = plan.confirmation_required
    defn.missing_config = plan.missing_config
    defn.credential_requirements = plan.credential_requirements
    defn.risk_signals = plan.risk_signals
    return defn
