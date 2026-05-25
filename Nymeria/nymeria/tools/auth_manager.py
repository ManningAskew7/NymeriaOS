"""Agent-safe credential vault management tool."""

from __future__ import annotations

import json
import time
from datetime import datetime
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


def _iso_epoch(value: Any) -> float:
    if not value:
        return 0.0
    if isinstance(value, (int, float)):
        return float(value)
    try:
        return datetime.fromisoformat(str(value).replace("Z", "+00:00")).timestamp()
    except Exception:
        return 0.0


def _record_account_ids(record: Any) -> set[str]:
    metadata = record.metadata or {}
    account_ids: set[str] = set()
    account_id = metadata.get("account_id")
    if account_id:
        account_ids.add(str(account_id))
    for account in metadata.get("accounts") or []:
        if isinstance(account, dict):
            account_id = account.get("account_id")
            if account_id:
                account_ids.add(str(account_id))
    return account_ids


def _record_emails(record: Any) -> list[str]:
    metadata = record.metadata or {}
    emails: list[str] = []
    email = metadata.get("email")
    if email:
        emails.append(str(email))
    for account in metadata.get("accounts") or []:
        if isinstance(account, dict) and account.get("email"):
            emails.append(str(account["email"]))
    return sorted(set(emails))


def _matches_filters(
    record: Any,
    *,
    provider: str = "",
    kind: str = "",
    status: str = "",
    account_id: str = "",
    prompt_id: str = "",
) -> bool:
    if provider and record.provider != provider:
        # Outlook's legacy cache is stored under provider="microsoft".
        if not (provider == "outlook" and record.provider == "microsoft"):
            return False
    if kind and record.kind != kind:
        return False
    if status and record.status != status:
        return False
    metadata = record.metadata or {}
    if account_id and account_id not in _record_account_ids(record):
        return False
    if prompt_id and metadata.get("prompt_id") != prompt_id:
        return False
    return True


def _filtered_records(
    repo: Any,
    user_id: str,
    *,
    provider: str = "",
    kind: str = "",
    status: str = "",
    account_id: str = "",
    prompt_id: str = "",
    include_disabled: bool = False,
) -> list[Any]:
    records = repo.list_credentials(
        owner_user_id=user_id,
        include_system=True,
        include_disabled=include_disabled,
    )
    return [
        record
        for record in records
        if _matches_filters(
            record,
            provider=provider,
            kind=kind,
            status=status,
            account_id=account_id,
            prompt_id=prompt_id,
        )
    ]


def _oauth_account_summary(records: list[Any]) -> list[dict[str, Any]]:
    rows: dict[tuple[str, str], dict[str, Any]] = {}
    for record in records:
        account_ids = _record_account_ids(record) or {record.account_label or record.id}
        for account_id in account_ids:
            key = (record.provider, account_id)
            row = rows.setdefault(
                key,
                {
                    "provider": record.provider,
                    "account_id": account_id,
                    "emails": [],
                    "credentials": [],
                    "active_count": 0,
                    "pending_count": 0,
                    "legacy_count": 0,
                },
            )
            row["emails"] = sorted(set(row["emails"] + _record_emails(record)))
            row["credentials"].append(
                {
                    "id": record.id,
                    "name": record.name,
                    "kind": record.kind,
                    "status": record.status,
                    "account_label": record.account_label,
                    "expires_at": record.expires_at,
                    "created_at": record.created_at,
                    "updated_at": record.updated_at,
                    "has_secret": bool(record.secret_fields),
                }
            )
            if record.status == "active":
                row["active_count"] += 1
            if record.status == "pending_setup":
                row["pending_count"] += 1
            if record.kind == "legacy_token_cache":
                row["legacy_count"] += 1
    return sorted(rows.values(), key=lambda row: (row["provider"], row["account_id"]))


def _cleanup_stale_oauth_candidates(records: list[Any]) -> list[dict[str, Any]]:
    now = time.time()
    candidates: dict[str, dict[str, Any]] = {}
    active_accounts: set[tuple[str, str]] = set()
    active_by_account: dict[tuple[str, str], list[Any]] = {}

    for record in records:
        if record.kind != "oauth_token" or record.status != "active":
            continue
        for account_id in _record_account_ids(record):
            active_accounts.add((record.provider, account_id))
            active_by_account.setdefault((record.provider, account_id), []).append(record)

    for account_key, account_records in active_by_account.items():
        if len(account_records) <= 1:
            continue
        keep = max(account_records, key=lambda record: _iso_epoch(record.updated_at) or _iso_epoch(record.created_at))
        for record in account_records:
            if record.id == keep.id:
                continue
            candidates[record.id] = {
                "credential_id": record.id,
                "reason": "duplicate_active_oauth_token",
                "keep_credential_id": keep.id,
                "provider": record.provider,
                "account_ids": sorted(_record_account_ids(record)),
                "status": record.status,
                "kind": record.kind,
            }

    for record in records:
        metadata = record.metadata or {}
        account_ids = _record_account_ids(record)
        if record.kind == "oauth_token" and record.status == "pending_setup" and metadata.get("oauth_pending") is True:
            candidates.setdefault(
                record.id,
                {
                    "credential_id": record.id,
                    "reason": "pending_oauth_placeholder",
                    "provider": record.provider,
                    "account_ids": sorted(account_ids),
                    "status": record.status,
                    "kind": record.kind,
                },
            )
            continue
        if record.kind == "oauth_token" and record.status == "active" and record.expires_at:
            expired = _iso_epoch(record.expires_at) and _iso_epoch(record.expires_at) < now
            if expired:
                for account_id in account_ids:
                    siblings = active_by_account.get((record.provider, account_id), [])
                    if any(sibling.id != record.id for sibling in siblings):
                        candidates.setdefault(
                            record.id,
                            {
                                "credential_id": record.id,
                                "reason": "expired_oauth_token_with_newer_active_sibling",
                                "provider": record.provider,
                                "account_ids": sorted(account_ids),
                                "status": record.status,
                                "kind": record.kind,
                            },
                        )
                        break
        if record.kind == "legacy_token_cache" and record.status == "active":
            provider_key = "outlook" if record.provider == "microsoft" else record.provider
            if any((provider_key, account_id) in active_accounts for account_id in account_ids):
                candidates.setdefault(
                    record.id,
                    {
                        "credential_id": record.id,
                        "reason": "legacy_cache_replaced_by_active_oauth_token",
                        "provider": record.provider,
                        "account_ids": sorted(account_ids),
                        "status": record.status,
                        "kind": record.kind,
                    },
                )

    return sorted(candidates.values(), key=lambda row: (row["provider"], row["reason"], row["credential_id"]))


@tool
def auth_manager(
    action: str,
    credential_id: str = "",
    provider: str = "",
    kind: str = "",
    name: str = "",
    target_type: str = "",
    target_id: str = "",
    binding_name: str = "",
    binding_id: str = "",
    status: str = "",
    account_id: str = "",
    prompt_id: str = "",
    include_disabled: bool = False,
    dry_run: bool = True,
    limit: int = 100,
    metadata: Optional[dict[str, Any]] = None,
    required_fields: Optional[list[str]] = None,
    config: Annotated[RunnableConfig, InjectedToolArg] = None,
) -> str:
    """Manage Nymeria credentials without exposing secret values.

    Actions:
    - list: list the current user's credentials plus system credential metadata.
    - status: show metadata and bindings for one credential.
    - oauth_accounts: summarize OAuth accounts and duplicate/stale state.
    - cleanup_stale_oauth: disable stale pending/duplicate OAuth rows (dry_run by default).
    - disable_matching: disable credentials matching metadata filters (dry_run by default).
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
    provider_norm = (provider or "").strip().lower()
    kind_norm = (kind or "").strip().lower()
    status_norm = (status or "").strip().lower()
    account_id_norm = (account_id or "").strip()
    prompt_id_norm = (prompt_id or "").strip()
    max_rows = max(1, min(500, int(limit or 100)))

    if normalized == "list":
        records = _filtered_records(
            repo,
            user_id,
            provider=provider_norm,
            kind=kind_norm,
            status=status_norm,
            account_id=account_id_norm,
            prompt_id=prompt_id_norm,
            include_disabled=include_disabled,
        )
        return _json({"ok": True, "credentials": [_public(r) for r in records[:max_rows]], "total": len(records)})

    if normalized == "oauth_accounts":
        records = _filtered_records(
            repo,
            user_id,
            provider=provider_norm,
            kind="",
            status=status_norm,
            account_id=account_id_norm,
            prompt_id=prompt_id_norm,
            include_disabled=include_disabled,
        )
        oauthish = [
            record
            for record in records
            if record.kind in {"oauth_token", "legacy_token_cache"}
        ]
        return _json({"ok": True, "accounts": _oauth_account_summary(oauthish), "total_credentials": len(oauthish)})

    if normalized == "cleanup_stale_oauth":
        records = _filtered_records(
            repo,
            user_id,
            provider=provider_norm,
            kind="",
            status="",
            account_id=account_id_norm,
            prompt_id=prompt_id_norm,
            include_disabled=False,
        )
        oauthish = [
            record
            for record in records
            if record.kind in {"oauth_token", "legacy_token_cache"} and _can_manage(user_id, record)
        ]
        candidates = _cleanup_stale_oauth_candidates(oauthish)
        disabled: list[str] = []
        if not dry_run:
            candidate_ids = {row["credential_id"] for row in candidates}
            for record in oauthish:
                if record.id not in candidate_ids:
                    continue
                if repo.disable_credential(record.id, actor_user_id=user_id):
                    disabled.append(record.id)
        return _json(
            {
                "ok": True,
                "dry_run": dry_run,
                "candidates": candidates[:max_rows],
                "candidate_count": len(candidates),
                "disabled": disabled,
            }
        )

    if normalized == "disable_matching":
        if not any([provider_norm, kind_norm, status_norm, account_id_norm, prompt_id_norm]):
            return _json(
                {
                    "ok": False,
                    "error": "At least one filter is required: provider, kind, status, account_id, or prompt_id.",
                }
            )
        records = _filtered_records(
            repo,
            user_id,
            provider=provider_norm,
            kind=kind_norm,
            status=status_norm,
            account_id=account_id_norm,
            prompt_id=prompt_id_norm,
            include_disabled=False,
        )
        manageable = [record for record in records if _can_manage(user_id, record)]
        disabled: list[str] = []
        if not dry_run:
            for record in manageable:
                if repo.disable_credential(record.id, actor_user_id=user_id):
                    disabled.append(record.id)
        return _json(
            {
                "ok": True,
                "dry_run": dry_run,
                "matched": [_public(record) for record in manageable[:max_rows]],
                "matched_count": len(manageable),
                "disabled": disabled,
            }
        )

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
        repo.add_allowed_target(
            credential_id,
            target=f"{target_type}:{target_id}",
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
            "actions": [
                "list",
                "status",
                "oauth_accounts",
                "cleanup_stale_oauth",
                "disable_matching",
                "request_setup",
                "bind",
                "unbind",
                "test",
                "disable",
            ],
        }
    )


AUTH_MANAGER_TOOLS = [auth_manager]
