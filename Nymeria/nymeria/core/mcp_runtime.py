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
from typing import Any, Dict, List, Optional, Tuple
from urllib.parse import urlparse

from ..config import get_settings
from ..tools.definitions.schema import MCPServerDefinition
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
            pass


def save_preview(defn: MCPServerDefinition, plan: MCPInstallPlan, *, source: str = "") -> str:
    cleanup_expired_previews()
    token = uuid.uuid4().hex
    plan.preview_token = token
    payload = {
        "source": source,
        "definition": defn.model_dump(mode="json"),
        "plan": plan.to_dict(),
        "created_at": time.time(),
    }
    _preview_path(token).write_text(json.dumps(payload, indent=2), encoding="utf-8")
    return token


def load_preview(token: str) -> Tuple[str, MCPServerDefinition, MCPInstallPlan]:
    if not re.fullmatch(r"[a-f0-9]{32}", token or ""):
        raise MCPInstallError("invalid preview token")
    path = _preview_path(token)
    if not path.exists():
        raise MCPInstallError("preview expired or not found")
    payload = json.loads(path.read_text(encoding="utf-8"))
    if time.time() - float(payload.get("created_at", 0)) > PREVIEW_TTL_SECONDS:
        path.unlink(missing_ok=True)
        raise MCPInstallError("preview expired")
    defn = MCPServerDefinition(**payload["definition"])
    plan = MCPInstallPlan.from_dict(payload["plan"])
    plan.preview_token = token
    return payload.get("source", ""), defn, plan


def consume_preview(token: str) -> None:
    if token:
        _preview_path(token).unlink(missing_ok=True)


def _is_secret_name(name: str) -> bool:
    return bool(SECRET_NAME_RE.search(name or ""))


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
    plan = MCPInstallPlan(
        source_type=source_type,
        runtime_type=runtime_type,
        risk_level=risk_level,
        confirmation_required=confirmation_required,
        parsed_summary=describe_definition(defn),
        command_preview=_command_preview(defn),
        warnings=warnings or [],
        required_config=required_config or [],
        missing_config=[],
        source_url=source_url,
        bundle_path=bundle_path,
    )
    plan.missing_config = [f for f in plan.required_config if f.get("required", True)]
    defn.source_type = source_type
    defn.runtime_type = runtime_type
    defn.parsed_summary = plan.parsed_summary
    defn.install_plan = plan.to_dict()
    defn.missing_config = plan.missing_config
    defn.risk_level = risk_level
    defn.confirmation_required = confirmation_required
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
        return _base_plan(defn, source_type="http", runtime_type="http")

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
        source_type="json" if stripped.startswith("{") else "stdio",
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


def apply_config_values(
    defn: MCPServerDefinition,
    plan: MCPInstallPlan,
    config_values: Optional[Dict[str, str]] = None,
) -> Tuple[MCPServerDefinition, List[Dict[str, Any]]]:
    values = config_values or {}
    missing: List[Dict[str, Any]] = []
    env_vars = dict(defn.env_vars or {})
    encrypted = dict(defn.encrypted_env_vars or {})

    for field in plan.required_config:
        name = str(field.get("name") or "")
        env_name = str(field.get("env_name") or name)
        value = values.get(name)
        if (value is None or value == "") and field.get("default") is not None:
            value = str(field["default"])
        if (value is None or value == "") and field.get("required", True):
            missing.append(field)
            continue
        if value is None:
            continue
        if field.get("sensitive"):
            try:
                encrypted[env_name] = nymeria_secrets.encrypt(value)
                env_vars.pop(env_name, None)
            except Exception:
                missing.append({
                    **field,
                    "error": "NYMERIA_SECRETS_KEY is required to store this secret",
                })
        else:
            env_vars[env_name] = value
            encrypted.pop(env_name, None)

    defn.env_vars = env_vars
    defn.encrypted_env_vars = encrypted
    defn.missing_config = missing
    return defn, missing


def prepare_runtime(
    defn: MCPServerDefinition,
    plan: MCPInstallPlan,
    *,
    config_values: Optional[Dict[str, str]] = None,
    log_sink: Optional[List[str]] = None,
) -> Tuple[MCPServerDefinition, List[str]]:
    logs: List[str] = log_sink if log_sink is not None else []
    defn, missing = apply_config_values(defn, plan, config_values)
    if missing:
        plan.missing_config = missing
        raise MCPInstallError("missing required MCP configuration")

    runtime_dir = _runtime_dir(defn.id)
    (runtime_dir / "cache").mkdir(exist_ok=True)

    if plan.runtime_type == "http":
        logs.append("HTTP MCP server; no local setup required.")
        return defn, logs

    if plan.source_type == "bundle_url":
        bundle_path = runtime_dir / "bundle.mcpb"
        _download(plan.source_url, bundle_path)
        plan.bundle_path = str(bundle_path)
        logs.append(f"Downloaded bundle from {plan.source_url}")
        defn, logs = _prepare_bundle(defn, plan, runtime_dir, config_values or {}, logs)
        return defn, logs

    if plan.source_type == "bundle_upload":
        defn, logs = _prepare_bundle(defn, plan, runtime_dir, config_values or {}, logs)
        return defn, logs

    if plan.source_type == "git":
        source_dir = runtime_dir / "source"
        if source_dir.exists():
            shutil.rmtree(source_dir)
        _run(["git", "clone", "--depth", "1", plan.source_url, str(source_dir)], logs, timeout=180)
        logs.append(f"Cloned {plan.source_url}")
        defn, logs = _prepare_source_tree(defn, source_dir, logs)
        return defn, logs

    if plan.runtime_type == "uvx":
        cache = runtime_dir / "cache" / "uv"
        cache.mkdir(parents=True, exist_ok=True)
        defn.env_vars = {**defn.env_vars, "UV_CACHE_DIR": str(cache)}
        logs.append(f"Prepared uvx cache at {cache}")
        return defn, logs

    if plan.runtime_type == "npx":
        cache = runtime_dir / "cache" / "npm"
        cache.mkdir(parents=True, exist_ok=True)
        defn.env_vars = {**defn.env_vars, "NPM_CONFIG_CACHE": str(cache)}
        logs.append(f"Prepared npx cache at {cache}")
        return defn, logs

    if plan.runtime_type in {"python", "node", "direct"}:
        logs.append("Using direct stdio command; no managed setup required.")
        return defn, logs

    logs.append(f"No setup handler for runtime type {plan.runtime_type}; using parsed command.")
    return defn, logs


def _download(url: str, dest: Path) -> None:
    import requests
    with requests.get(url, stream=True, timeout=60) as resp:
        if resp.status_code >= 400:
            raise MCPInstallError(f"download failed HTTP {resp.status_code}: {resp.text[:200]}")
        dest.parent.mkdir(parents=True, exist_ok=True)
        with dest.open("wb") as fh:
            for chunk in resp.iter_content(chunk_size=1024 * 1024):
                if chunk:
                    fh.write(chunk)


def _run(cmd: List[str], logs: List[str], *, cwd: Optional[Path] = None, timeout: int = 120) -> None:
    logs.append(f"$ {' '.join(cmd)}")
    proc = subprocess.run(
        cmd,
        cwd=str(cwd) if cwd else None,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        timeout=timeout,
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
            pass
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
    encrypted = dict(defn.encrypted_env_vars or {})
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
                encrypted[env_key] = nymeria_secrets.encrypt(str(config_values[field_name]))
                env_vars.pop(env_key, None)
            continue
        env_vars[env_key] = _replace_config_string(value, config_values, source_dir)
        encrypted.pop(env_key, None)

    defn.env_vars = env_vars
    defn.encrypted_env_vars = encrypted
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
    return defn
