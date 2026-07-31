"""Credential-vault resolution for LLM provider auth.

The settings layer still supports env vars for deployment-friendly defaults,
but runtime LLM calls should also be able to use Nymeria's encrypted vault.
This module keeps vault selection policy separate from provider metadata and
agent graph construction.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any, Iterable, Optional

from ..config.llm_providers import get_llm_provider_spec, normalize_llm_provider
from .credential_vault import (
    CREDENTIAL_REF_PATTERN,
    UNATTRIBUTED_ACTOR,
    CredentialAccessDenied,
    CredentialSecretUnavailable,
)

logger = logging.getLogger(__name__)

LLM_PROVIDER_TARGET_TYPE = "llm_provider"
LLM_CREDENTIAL_KINDS = {
    "api_key",
    "llm_provider",
    "llm_connection",
    "provider_connection",
    "openai_compatible",
    "connection",
}
API_KEY_FIELD_NAMES = (
    "api_key",
    "key",
    "token",
    "bearer_token",
    "access_token",
    "value",
)
BASE_URL_FIELD_NAMES = (
    "base_url",
    "api_base",
    "api_base_url",
    "endpoint",
)


@dataclass(frozen=True)
class LLMProviderCredential:
    """Resolved vault material for one provider call path."""

    api_key: Optional[str]
    base_url: Optional[str]
    credential_id: str
    api_key_field: Optional[str] = None
    base_url_field: Optional[str] = None


def _target_candidates(provider: str, thread_id: str | None) -> list[tuple[str, str]]:
    candidates: list[tuple[str, str]] = []
    if thread_id:
        candidates.append(("thread", thread_id))
    candidates.append((LLM_PROVIDER_TARGET_TYPE, provider))
    candidates.append((LLM_PROVIDER_TARGET_TYPE, "*"))
    return candidates


def _provider_matches(record_provider: str, provider: str) -> bool:
    return normalize_llm_provider(record_provider) == provider


def _target_score(record: Any, provider: str, thread_id: str | None, bound_ids: set[str]) -> int:
    allowed = set(record.allowed_targets or [])
    if record.id in bound_ids:
        return 0
    if thread_id and f"thread:{thread_id}" in allowed:
        return 1
    if f"{LLM_PROVIDER_TARGET_TYPE}:{provider}" in allowed:
        return 2
    if f"{LLM_PROVIDER_TARGET_TYPE}:*" in allowed or "thread:*" in allowed or "*" in allowed:
        return 3
    if not allowed:
        return 4
    return 100


def _list_visible_records(repo: Any, owner_user_id: str | None) -> list[Any]:
    if owner_user_id:
        return repo.list_credentials(owner_user_id=owner_user_id, include_system=True)
    return repo.list_credentials(owner_type="system")


def _bound_credential_ids(repo: Any, provider: str, thread_id: str | None) -> set[str]:
    targets = set(_target_candidates(provider, thread_id))
    try:
        bindings = repo.list_bindings()
    except Exception:
        logger.debug("Failed to list credential bindings for LLM provider lookup", exc_info=True)
        return set()
    return {
        str(row["credential_id"])
        for row in bindings
        if (str(row.get("target_type")), str(row.get("target_id"))) in targets
    }


def _read_first_secret(
    repo: Any,
    record: Any,
    field_names: Iterable[str],
    *,
    provider: str,
    owner_user_id: str | None,
    thread_id: str | None,
) -> tuple[str | None, str | None]:
    for field_name in field_names:
        if field_name not in record.secret_fields:
            continue
        for target_type, target_id in _target_candidates(provider, thread_id):
            try:
                value = repo.get_secret_field(
                    record.id,
                    field_name,
                    # UNATTRIBUTED, not SYSTEM. "We could not work out who is
                    # asking" is not a licence to read as the platform: an
                    # unclaimed thread with no acting user would otherwise
                    # decrypt any user's provider key. The vault denies this
                    # actor, and the caller treats a denial as "no credential
                    # here", so the degradation is a missing key rather than a
                    # crash.
                    actor=owner_user_id or UNATTRIBUTED_ACTOR,
                    target_type=target_type,
                    target_id=target_id,
                )
            except (CredentialAccessDenied, CredentialSecretUnavailable):
                continue
            if value:
                return value, field_name
    return None, None


def _metadata_base_url(record: Any) -> str | None:
    metadata = record.metadata or {}
    for key in BASE_URL_FIELD_NAMES:
        value = metadata.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip().rstrip("/")
    return None


def get_llm_provider_credential(
    provider: str | None,
    *,
    vault: Any | None,
    owner_user_id: str | None = None,
    thread_id: str | None = None,
) -> Optional[LLMProviderCredential]:
    """Return the best matching active vault credential for an LLM provider.

    Selection mirrors native tool credentials:
    explicit binding, exact allowed target, wildcard target, then unscoped
    credentials. User-owned records beat system records at the same score.
    """
    if vault is None:
        return None

    canonical_provider = normalize_llm_provider(provider)
    if not canonical_provider:
        return None

    try:
        records = [
            record
            for record in _list_visible_records(vault, owner_user_id)
            if record.status == "active"
            and record.kind in LLM_CREDENTIAL_KINDS
            and _provider_matches(record.provider, canonical_provider)
        ]
        if not records:
            return None

        bound_ids = _bound_credential_ids(vault, canonical_provider, thread_id)
        records.sort(
            key=lambda record: (
                _target_score(record, canonical_provider, thread_id, bound_ids),
                0 if record.owner_type == "user" else 1,
                record.name.lower(),
            )
        )

        for record in records:
            if _target_score(record, canonical_provider, thread_id, bound_ids) >= 100:
                continue
            api_key, api_key_field = _read_first_secret(
                vault,
                record,
                API_KEY_FIELD_NAMES,
                provider=canonical_provider,
                owner_user_id=owner_user_id,
                thread_id=thread_id,
            )
            if not api_key:
                continue
            base_url, base_url_field = _read_first_secret(
                vault,
                record,
                BASE_URL_FIELD_NAMES,
                provider=canonical_provider,
                owner_user_id=owner_user_id,
                thread_id=thread_id,
            )
            base_url = (base_url or _metadata_base_url(record) or "").strip().rstrip("/") or None
            return LLMProviderCredential(
                api_key=api_key,
                base_url=base_url,
                credential_id=record.id,
                api_key_field=api_key_field,
                base_url_field=base_url_field,
            )
    except Exception:
        logger.debug(
            "Credential-vault lookup failed for LLM provider %s",
            canonical_provider,
            exc_info=True,
        )
    return None


def resolve_credential_references(
    value: str | None,
    *,
    vault: Any | None,
    owner_user_id: str | None,
    provider: str | None,
    thread_id: str | None = None,
) -> str | None:
    """Resolve ${credential:id.field} placeholders in explicit LLM config."""
    if not value or vault is None or not CREDENTIAL_REF_PATTERN.search(value):
        return value

    canonical_provider = normalize_llm_provider(provider)
    for target_type, target_id in _target_candidates(canonical_provider, thread_id):
        try:
            return vault.resolve_references(
                value,
                # The sharper of the two sites. The reference is parsed out of
                # a caller-supplied string, so the credential id is arbitrary
                # rather than pre-filtered: reading as SYSTEM here meant any
                # ${credential:...} written into a per-thread base_url or key
                # resolved against the whole vault whenever the thread had no
                # identifiable owner.
                actor=owner_user_id or UNATTRIBUTED_ACTOR,
                target_type=target_type,
                target_id=target_id,
            )
        except (CredentialAccessDenied, CredentialSecretUnavailable):
            continue
    return value


def credential_setup_hint(provider: str | None) -> str:
    """Human-readable vault setup hint for provider auth errors."""
    canonical = normalize_llm_provider(provider)
    spec = get_llm_provider_spec(canonical)
    label = spec.label if spec else canonical
    return (
        f'Save an active credential in Settings > Connections with provider "{canonical}", '
        f'kind "api_key", secret field "api_key", and allowed target '
        f'"{LLM_PROVIDER_TARGET_TYPE}:{canonical}" or "{LLM_PROVIDER_TARGET_TYPE}:*". '
        f"Provider: {label}."
    )
