"""Credential-vault helpers for native Nymeria tools."""

from __future__ import annotations

import logging
import os
from dataclasses import dataclass
from typing import Any, Iterable, Optional

from langchain_core.runnables import RunnableConfig

from .utils import get_user_id

logger = logging.getLogger(__name__)

NATIVE_TOOL_TARGET_TYPE = "native_tool"


@dataclass(frozen=True)
class NativeCredentialValue:
    value: str
    credential_id: str
    field_name: str


def provider_candidates(provider: str, aliases: Iterable[str]) -> set[str]:
    """Names a vault record's ``provider`` may carry and still match ``provider``.

    Public because ``credential_registry`` reuses the same matching semantics;
    the private name below is kept for existing callers/tests.
    """
    return {provider, *aliases, provider.replace("-", "_"), provider.replace("_", "-")}


_provider_candidates = provider_candidates


def _target_score(record: Any, tool_name: str, bound_ids: set[str]) -> int:
    allowed = set(record.allowed_targets or [])
    if record.id in bound_ids:
        return 0
    if f"{NATIVE_TOOL_TARGET_TYPE}:{tool_name}" in allowed:
        return 1
    if f"{NATIVE_TOOL_TARGET_TYPE}:*" in allowed or "*" in allowed:
        return 2
    # An empty allowed_targets used to score 3 here, as a usable-but-unscoped
    # candidate, because the vault then read it as "any target may read". It
    # denies now, so ranking such a record above 100 would only pick a
    # credential the vault is about to refuse.
    return 100


def get_native_credential_value(
    *,
    provider: str,
    field_names: Iterable[str],
    tool_name: str,
    config: Optional[RunnableConfig] = None,
    provider_aliases: Iterable[str] = (),
) -> Optional[NativeCredentialValue]:
    """Return the best matching credential-vault field for a native tool.

    Selection order favors explicit bindings, then explicit allowed target,
    then wildcard target. User-owned credentials are preferred over system
    credentials at the same target score.

    A credential with no allowed targets is NOT a candidate. It used to rank
    last but still win when nothing else matched, back when an empty list meant
    "any consumer may decrypt this". Empty now denies, so ranking it would only
    pick a credential the vault is about to refuse.
    """
    user_id = get_user_id(config)
    provider_names = _provider_candidates(provider, provider_aliases)
    field_list = list(dict.fromkeys(field_names))

    try:
        from ..core.credential_vault import (
            CredentialAccessDenied,
            CredentialSecretUnavailable,
            get_credential_vault_repo,
        )

        repo = get_credential_vault_repo()
        records = [
            record
            for record in repo.list_credentials(owner_user_id=user_id, include_system=True)
            if record.provider in provider_names and record.status == "active"
        ]
        bindings = [
            row
            for row in repo.list_bindings()
            if row.get("target_type") == NATIVE_TOOL_TARGET_TYPE
            and row.get("target_id") in {tool_name, "*"}
        ]
        bound_ids = {str(row["credential_id"]) for row in bindings}
        records.sort(
            key=lambda record: (
                _target_score(record, tool_name, bound_ids),
                0 if record.owner_type == "user" else 1,
                record.updated_at,
            )
        )
        for record in records:
            if _target_score(record, tool_name, bound_ids) >= 100:
                continue
            for field_name in field_list:
                if field_name not in record.secret_fields:
                    continue
                try:
                    value = repo.get_secret_field(
                        record.id,
                        field_name,
                        actor=user_id,
                        target_type=NATIVE_TOOL_TARGET_TYPE,
                        target_id=tool_name,
                    )
                except (CredentialAccessDenied, CredentialSecretUnavailable):
                    logger.debug(
                        "Credential %s was not usable for native tool %s",
                        record.id,
                        tool_name,
                        exc_info=True,
                    )
                    continue
                if value:
                    return NativeCredentialValue(
                        value=value,
                        credential_id=record.id,
                        field_name=field_name,
                    )
    except Exception:
        logger.debug("Native credential lookup failed for provider %s", provider, exc_info=True)
    return None


def resolve_native_credential(
    *,
    provider: str,
    tool_name: str,
    settings_attr: str,
    config: Optional[RunnableConfig] = None,
    aliases: tuple[str, ...] = (),
    env_vars: tuple[str, ...] = (),
    field_names: Iterable[str] = ("api_key", "token", "value"),
) -> Optional[str]:
    """Resolve a native-tool credential string: vault, then settings, then env.

    Tries the credential vault (``get_native_credential_value``), then the named
    ``Settings`` attribute, then each env var in ``env_vars`` order, returning the
    first truthy value (or None). ``field_names`` defaults to the standard API-key
    trio; pass a different tuple for non-key credentials (e.g. the SearXNG base
    URL). Replaces the per-provider ``_get_<provider>_api_key`` resolvers that each
    hand-rolled this vault -> settings -> env fallback.
    """
    cred = get_native_credential_value(
        provider=provider,
        provider_aliases=aliases,
        field_names=field_names,
        tool_name=tool_name,
        config=config,
    )
    if cred and cred.value:
        return cred.value

    from ..config import get_settings

    # Left-fold of ``or`` reproduces ``settings.X or env(A) or env(B)`` exactly,
    # including the empty-string edge (the original returns the last ``.get()``
    # verbatim when nothing is truthy).
    value = getattr(get_settings(), settings_attr)
    for env_var in env_vars:
        value = value or os.environ.get(env_var)
    return value


def native_credential_setup_hint(
    *,
    provider: str,
    field_names: Iterable[str],
    tool_name: str,
    env_var: str = "",
    display_name: str = "",
) -> str:
    label = display_name or provider
    fields = ", ".join(f'"{field}"' for field in field_names)
    target = f"{NATIVE_TOOL_TARGET_TYPE}:{tool_name}"
    text = (
        f"[Error]: No {label} credential found. Save one in Settings > Connections "
        f'with provider "{provider}", required field(s) {fields}, and allowed target "{target}" '
        f'or "native_tool:*".'
    )
    if env_var:
        text += f" Env fallback: {env_var}."
    return text
