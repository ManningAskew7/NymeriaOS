"""Agent-safe credential vault management tool."""

from __future__ import annotations

import json
from typing import Annotated, Any, Optional

from langchain_core.runnables import RunnableConfig
from langchain_core.tools import InjectedToolArg, tool

from ..core.credential_vault import get_credential_vault_repo
from .utils import get_user_id


def _json(data: dict[str, Any]) -> str:
    return json.dumps(data, indent=2, default=str)


def _public(record: Any) -> dict[str, Any]:
    data = record.public_dict()
    data.pop("owner_user_id", None)
    return data


def _can_manage(user_id: str, record: Any) -> bool:
    return record.owner_type == "user" and record.owner_user_id == user_id


@tool
def auth_manager(
    action: str,
    credential_id: str = "",
    provider: str = "",
    kind: str = "api_key",
    name: str = "",
    target_type: str = "",
    target_id: str = "",
    binding_name: str = "",
    binding_id: str = "",
    metadata: Optional[dict[str, Any]] = None,
    required_fields: Optional[list[str]] = None,
    config: Annotated[RunnableConfig, InjectedToolArg] = None,
) -> str:
    """Manage Nymeria credentials without exposing secret values.

    Actions:
    - list: list the current user's credentials plus system credential metadata.
    - status: show metadata and bindings for one credential.
    - request_setup: create a pending secure setup record for the UI/user.
    - bind: bind a credential to a target such as mcp_server:<id> or custom_tool:<id>.
    - unbind: remove a binding by binding_id.
    - test: perform a generic vault health check without revealing values.
    - disable: disable a credential.

    This tool never returns plaintext secrets, ciphertext, or partial keys.
    Users must enter secret material through the Settings/Connections UI or
    authenticated REST API, not through chat.
    """
    user_id = get_user_id(config)
    repo = get_credential_vault_repo()
    normalized = (action or "").strip().lower()

    if normalized == "list":
        records = repo.list_credentials(owner_user_id=user_id, include_system=True)
        return _json({"ok": True, "credentials": [_public(r) for r in records]})

    if normalized == "status":
        if not credential_id:
            return _json({"ok": False, "error": "credential_id is required"})
        record = repo.get_credential(credential_id)
        if not record or (record.owner_type == "user" and record.owner_user_id != user_id):
            return _json({"ok": False, "error": "credential not found"})
        return _json(
            {
                "ok": True,
                "credential": _public(record),
                "bindings": repo.list_bindings(credential_id),
            }
        )

    if normalized == "request_setup":
        if not provider or not name:
            return _json({"ok": False, "error": "provider and name are required"})
        safe_required = required_fields or ["value"]
        if metadata:
            forbidden = {"secret", "secrets", "token", "api_key", "password", "secret_fields"}
            if forbidden.intersection({str(k).lower() for k in metadata.keys()}):
                return _json(
                    {
                        "ok": False,
                        "error": "metadata must not contain secret values; use the secure UI prompt",
                    }
                )
        allowed_targets = []
        if target_type and target_id:
            allowed_targets.append(f"{target_type}:{target_id}")
        record = repo.create_credential(
            owner_type="user",
            owner_user_id=user_id,
            name=name,
            provider=provider,
            kind=kind or "api_key",
            status="pending_setup",
            metadata={
                **(metadata or {}),
                "setup_session": True,
                "required_fields": safe_required,
                "target_type": target_type or None,
                "target_id": target_id or None,
            },
            allowed_targets=allowed_targets,
            created_by_user_id=user_id,
        )
        if target_type and target_id:
            repo.bind_credential(
                record.id,
                target_type=target_type,
                target_id=target_id,
                binding_name=binding_name or "setup_request",
                actor_user_id=user_id,
            )
        return _json(
            {
                "ok": True,
                "credential": _public(record),
                "message": "Secure setup record created. Ask the user to finish it in Settings > Connections.",
            }
        )

    if normalized == "bind":
        if not credential_id or not target_type or not target_id:
            return _json({"ok": False, "error": "credential_id, target_type, and target_id are required"})
        record = repo.get_credential(credential_id)
        if not record or not _can_manage(user_id, record):
            return _json({"ok": False, "error": "credential not found"})
        next_binding_id = repo.bind_credential(
            credential_id,
            target_type=target_type,
            target_id=target_id,
            binding_name=binding_name or None,
            actor_user_id=user_id,
        )
        return _json({"ok": True, "binding_id": next_binding_id})

    if normalized == "unbind":
        if not binding_id:
            return _json({"ok": False, "error": "binding_id is required"})
        row = next((r for r in repo.list_bindings() if r["id"] == binding_id), None)
        if not row:
            return _json({"ok": False, "error": "binding not found"})
        record = repo.get_credential(row["credential_id"])
        if not record or not _can_manage(user_id, record):
            return _json({"ok": False, "error": "binding not found"})
        return _json({"ok": True, "deleted": repo.delete_binding(binding_id, actor_user_id=user_id)})

    if normalized == "test":
        if not credential_id:
            return _json({"ok": False, "error": "credential_id is required"})
        record = repo.get_credential(credential_id)
        if not record or not _can_manage(user_id, record):
            return _json({"ok": False, "error": "credential not found"})
        status = "active" if record.secret_fields else "pending_setup"
        repo.mark_tested(
            credential_id,
            status=status,
            actor_user_id=user_id,
            details={"generic_test": True, "has_secret": bool(record.secret_fields)},
        )
        return _json({"ok": True, "status": status})

    if normalized == "disable":
        if not credential_id:
            return _json({"ok": False, "error": "credential_id is required"})
        record = repo.get_credential(credential_id)
        if not record or not _can_manage(user_id, record):
            return _json({"ok": False, "error": "credential not found"})
        return _json({"ok": True, "disabled": repo.disable_credential(credential_id, actor_user_id=user_id)})

    return _json(
        {
            "ok": False,
            "error": "unknown action",
            "actions": ["list", "status", "request_setup", "bind", "unbind", "test", "disable"],
        }
    )


AUTH_MANAGER_TOOLS = [auth_manager]
