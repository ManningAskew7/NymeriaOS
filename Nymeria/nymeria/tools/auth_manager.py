"""Agent-safe credential vault management tool."""

from __future__ import annotations

import json
import time
from datetime import datetime
from typing import Annotated, Any, Literal, NamedTuple

from langchain_core.runnables import RunnableConfig
from langchain_core.tools import InjectedToolArg, tool

from ..core.credential_vault import get_credential_vault_repo
from .utils import get_user_id, is_admin


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


def _max_rows(limit: int) -> int:
    return max(1, min(500, int(limit or 100)))


class _NormalizedFilters(NamedTuple):
    provider: str
    kind: str
    status: str
    account_id: str
    prompt_id: str
    max_rows: int


def _normalize_filters(
    provider: str,
    kind: str,
    status: str,
    account_id: str,
    prompt_id: str,
    limit: int,
) -> _NormalizedFilters:
    """Normalize the credential filter args shared by auth_inspect/auth_cleanup.

    provider/kind/status are lowercased; account_id/prompt_id are only stripped
    (they are opaque identifiers, not case-insensitive enums). max_rows applies
    the shared clamp.
    """
    return _NormalizedFilters(
        provider=(provider or "").strip().lower(),
        kind=(kind or "").strip().lower(),
        status=(status or "").strip().lower(),
        account_id=(account_id or "").strip(),
        prompt_id=(prompt_id or "").strip(),
        max_rows=_max_rows(limit),
    )


@tool
def auth_inspect(
    view: Literal["list", "status", "oauth_accounts"] = "list",
    credential_id: str = "",
    provider: str = "",
    kind: str = "",
    status: str = "",
    account_id: str = "",
    prompt_id: str = "",
    include_disabled: bool = False,
    limit: int = 100,
    config: Annotated[RunnableConfig, InjectedToolArg] = None,
) -> str:
    """Inspect Nymeria credential metadata without exposing secret values.

    Views:
    - list: list credentials visible to the current user, with optional filters.
    - status: show one credential's metadata and bindings; requires credential_id.
    - oauth_accounts: group OAuth and legacy token-cache rows by provider account.

    Secret values, ciphertext, and partial keys are never returned.
    """
    user_id = get_user_id(config)
    repo = get_credential_vault_repo()
    normalized = (view or "list").strip().lower()
    provider_norm, kind_norm, status_norm, account_id_norm, prompt_id_norm, max_rows = (
        _normalize_filters(provider, kind, status, account_id, prompt_id, limit)
    )

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

    return _json({"ok": False, "error": "unknown view", "views": ["list", "status", "oauth_accounts"]})


@tool
def auth_cleanup(
    operation: Literal["stale_oauth", "disable_matching", "disable"] = "stale_oauth",
    credential_id: str = "",
    provider: str = "",
    kind: str = "",
    status: str = "",
    account_id: str = "",
    prompt_id: str = "",
    dry_run: bool = True,
    limit: int = 100,
    config: Annotated[RunnableConfig, InjectedToolArg] = None,
) -> str:
    """Disable stale or unwanted user-owned credentials without exposing secrets.

    Operations:
    - stale_oauth: find stale pending/duplicate OAuth and replaced legacy cache rows.
      Dry-run by default; pass dry_run=false to disable candidates.
    - disable_matching: disable credentials matching filters. Requires at least one
      filter and is dry-run by default.
    - disable: disable exactly one credential_id immediately.
    """
    user_id = get_user_id(config)
    repo = get_credential_vault_repo()
    normalized = (operation or "stale_oauth").strip().lower()
    provider_norm, kind_norm, status_norm, account_id_norm, prompt_id_norm, max_rows = (
        _normalize_filters(provider, kind, status, account_id, prompt_id, limit)
    )

    if normalized == "stale_oauth":
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

    if normalized == "disable":
        if not credential_id:
            return _json({"ok": False, "error": "credential_id is required"})
        record = repo.get_credential(credential_id)
        if not record or not _can_manage(user_id, record):
            return _json({"ok": False, "error": "credential not found"})
        return _json({"ok": True, "disabled": repo.disable_credential(credential_id, actor_user_id=user_id)})

    return _json({"ok": False, "error": "unknown operation", "operations": ["stale_oauth", "disable_matching", "disable"]})


@tool
def auth_bindings(
    operation: Literal["bind", "unbind"] = "bind",
    credential_id: str = "",
    target_type: str = "",
    target_id: str = "",
    binding_name: str = "",
    binding_id: str = "",
    config: Annotated[RunnableConfig, InjectedToolArg] = None,
) -> str:
    """Manage credential-to-target bindings without exposing secret values.

    Operations:
    - bind: bind a user-owned credential to target_type:target_id and add that
      allowed target for runtime secret resolution.
    - unbind: remove a binding by binding_id and revoke that allowed target
      when no same-target binding remains.
    """
    user_id = get_user_id(config)
    repo = get_credential_vault_repo()
    normalized = (operation or "bind").strip().lower()

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
        row = repo.get_binding(binding_id)
        if not row:
            return _json({"ok": False, "error": "binding not found"})
        record = repo.get_credential(row["credential_id"])
        if not record or not _can_manage(user_id, record):
            return _json({"ok": False, "error": "binding not found"})
        target = f"{row['target_type']}:{row['target_id']}"
        deleted = repo.delete_binding(binding_id, actor_user_id=user_id)
        allowed_target_removed = False
        if deleted:
            remaining_same_target = [
                binding
                for binding in repo.list_bindings(record.id)
                if binding["target_type"] == row["target_type"]
                and binding["target_id"] == row["target_id"]
            ]
            if not remaining_same_target:
                allowed_target_removed = repo.remove_allowed_target(
                    record.id,
                    target=target,
                    actor_user_id=user_id,
                )
        return _json(
            {
                "ok": True,
                "deleted": deleted,
                "allowed_target": target,
                "allowed_target_removed": allowed_target_removed,
            }
        )

    return _json({"ok": False, "error": "unknown operation", "operations": ["bind", "unbind"]})


@tool
async def auth_test(
    credential_id: str = "",
    provider: str = "",
    tool_name: str = "",
    config: Annotated[RunnableConfig, InjectedToolArg] = None,
) -> str:
    """Test whether a saved credential is usable, without exposing secret values.

    Resolution precedence: credential_id > provider > tool_name (the tool's
    provider is looked up in the credential-spec registry; tools that need no
    credential return auth "not_required").

    Two checks run on the resolved credential:
    - a presence check against the provider spec's required fields, and
    - a live verification probe where the provider has one registered
      (no_tester providers report verified=false, presence-only).

    A successful probe marks the credential active; a failed probe marks it
    invalid (matching the Settings > Connections test button). System-owned
    credentials are probed by admins only (non-admins get a presence check,
    matching the REST test route's gate) and never have their status changed
    by this tool. Secret values are never returned.
    """
    from .credential_registry import get_provider_spec, spec_for_tool
    from .native_credentials import provider_candidates

    user_id = get_user_id(config)
    repo = get_credential_vault_repo()
    provider_norm = (provider or "").strip().lower()
    tool_name_norm = (tool_name or "").strip()
    credential_id = (credential_id or "").strip()

    spec = None
    record = None
    other_matches = 0

    if credential_id:
        record = repo.get_credential(credential_id)
        if not record or (record.owner_type == "user" and record.owner_user_id != user_id):
            return _json({"ok": False, "error": "credential not found"})
        spec = get_provider_spec(record.provider)
    elif provider_norm or tool_name_norm:
        if not provider_norm:
            spec = spec_for_tool(tool_name_norm)
            if spec is None:
                return _json(
                    {
                        "ok": True,
                        "tool": tool_name_norm,
                        "auth": "not_required",
                        "note": "No provider credential is associated with this tool.",
                    }
                )
            provider_norm = spec.provider
        else:
            spec = get_provider_spec(provider_norm)
        candidates = (
            provider_candidates(spec.provider, spec.aliases)
            if spec
            else provider_candidates(provider_norm, ())
        )
        matches = [
            rec
            for rec in repo.list_credentials(owner_user_id=user_id, include_system=True)
            if rec.provider in candidates
        ]
        actives = sorted(
            (rec for rec in matches if rec.status == "active"),
            key=lambda rec: (
                0 if rec.owner_type == "user" else 1,
                -_iso_epoch(rec.updated_at),
            ),
        )
        if not actives:
            pending = [rec for rec in matches if rec.status == "pending_setup"]
            payload: dict[str, Any] = {
                "ok": False,
                "provider": provider_norm,
                "error": "no active credential found",
                "pending_setup": len(pending),
            }
            if spec is not None:
                from .credential_registry import tools_hint_for_spec

                payload["setup_hint"] = tools_hint_for_spec(spec)
            return _json(payload)
        record = actives[0]
        other_matches = len(actives) - 1
    else:
        return _json(
            {"ok": False, "error": "provide credential_id, provider, or tool_name"}
        )

    saved_fields = set(record.secret_fields or ())
    missing_fields: list[dict[str, Any]] = []
    if spec is not None:
        for group in spec.required_groups:
            if not any(name in saved_fields for name in group.names):
                missing_fields.append({"role": group.role, "accepted_names": list(group.names)})

    probe: dict[str, Any]
    status_updated = False
    if record.owner_type != "user" and not is_admin(user_id):
        # Parity with the REST test route: non-admins cannot live-probe
        # system-owned credentials (no key-validity disclosure, no probe
        # traffic on shared keys). Presence check only.
        probe = {
            "ok": None,
            "verified": False,
            "code": "admin_only",
            "message": "Live probes of system-owned credentials are admin-only; "
            "presence check only. Tools that use this credential still "
            "resolve it automatically.",
        }
    else:
        try:
            secret_fields = repo.get_secret_fields_for_test(record.id, actor_user_id=user_id)
        except Exception:
            probe = {
                "ok": None,
                "verified": False,
                "code": "secrets_unavailable",
                "message": "Secret fields are not accessible for this credential "
                "(the secrets key may be unavailable); presence check only.",
            }
        else:
            from ..config import get_settings
            from ..core.credential_tests import test_credential_fields

            result = await test_credential_fields(
                provider=record.provider,
                kind=record.kind,
                metadata=record.metadata,
                secret_fields=secret_fields,
                settings=get_settings(),
            )
            probe = {
                "ok": result.ok,
                "verified": result.verified,
                "code": result.code,
                "message": result.message,
            }
            if _can_manage(user_id, record):
                status = "active" if result.ok else "invalid"
                repo.mark_tested(
                    record.id,
                    status=status,
                    actor_user_id=user_id,
                    details={
                        "status": status,
                        "ok": result.ok,
                        "verified": result.verified,
                        "code": result.code,
                        "message": result.message,
                        **(result.metadata or {}),
                    },
                )
                status_updated = True
                record = repo.get_credential(record.id) or record

    payload = {
        "ok": bool(probe.get("ok")) and not missing_fields,
        "credential": _public(record),
        "provider": record.provider,
        "fields_ok": not missing_fields,
        "missing_fields": missing_fields,
        "probe": probe,
        "status_updated": status_updated,
        "other_matches": other_matches,
    }
    if tool_name_norm:
        payload["tool"] = tool_name_norm
    if spec is not None:
        payload["spec_provider"] = spec.provider
    return _json(payload)


AUTH_MANAGER_TOOLS = [auth_inspect, auth_cleanup, auth_bindings, auth_test]
