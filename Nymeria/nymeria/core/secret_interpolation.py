"""Shared `${env:}` / `${credential:}` secret-reference interpolation.

This is the single home for the placeholder grammar used by MCP server
configuration (`mcp_manager.py`) and custom HTTP tools (`custom_tools.py`).
Before this module existed the resolution was copied across three sites and had
drifted: `mcp_manager` used a brittle whole-value ``value[6:-1]`` slice that
could not resolve an embedded reference (e.g. ``Authorization: Bearer
${env:TOKEN}``), while `custom_tools` already used the regex below.

Grammar:
    ``${env:VAR_NAME}``            -> os.environ value (resolved anywhere in the
                                      string, not only as the whole value)
    ``${credential:ID.FIELD}``     -> credential vault secret (see
                                      ``credential_vault.resolve_references``)

The vault import is deliberately function-local so this module stays a leaf
(stdlib only at import time) and cannot form an import cycle with the callers
that import it at module level.
"""

from __future__ import annotations

import os
import re
from typing import Literal, Optional

# Canonical env-reference grammar. Identifier var names (upper, lower, digits,
# underscore), resolved anywhere in the value so embedded references work.
ENV_VAR_PATTERN = re.compile(r"\$\{env:([A-Za-z_][A-Za-z0-9_]*)\}")

# Cheap pre-check so callers can skip the vault lookup when there is nothing to
# resolve. Mirrors the guard the call sites used before this module existed.
CREDENTIAL_REF_MARKER = "${credential:"

MissingEnvPolicy = Literal["raise", "empty"]


def interpolate_env_vars_with_names(
    value: str, *, missing: MissingEnvPolicy = "raise"
) -> tuple[str, set[str]]:
    """Replace ``${env:VAR}`` placeholders, returning (resolved, used_var_names).

    Args:
        value: String possibly containing one or more ``${env:VAR}`` references.
        missing: Policy for an unset environment variable. ``"raise"`` raises
            ``ValueError`` (the custom-tool behavior); ``"empty"`` substitutes
            an empty string (the MCP-server behavior).
    """
    used: set[str] = set()

    def replace_env(match: re.Match[str]) -> str:
        var_name = match.group(1)
        var_value = os.environ.get(var_name)
        if var_value is None:
            if missing == "raise":
                raise ValueError(f"Environment variable not set: {var_name}")
            return ""
        used.add(var_name)
        return var_value

    return ENV_VAR_PATTERN.sub(replace_env, value), used


def resolve_credential_refs(
    value: str,
    *,
    target_type: Optional[str] = None,
    target_id: Optional[str] = None,
    used_credentials: Optional[set[str]] = None,
    actor_user_id: Optional[str] = None,
    redact_values: Optional[set[str]] = None,
) -> str:
    """Resolve ``${credential:ID.FIELD}`` references via the credential vault.

    A guarded no-op when the value contains no credential reference, so callers
    do not pay for a vault lookup on plain values.
    """
    if CREDENTIAL_REF_MARKER not in value:
        return value
    from .credential_vault import get_credential_vault_repo

    return get_credential_vault_repo().resolve_references(
        value,
        actor_user_id=actor_user_id,
        target_type=target_type,
        target_id=target_id,
        used_credentials=used_credentials,
        redact_values=redact_values,
    )


def resolve_env_and_credential_refs(
    value: str,
    *,
    target_type: Optional[str] = None,
    target_id: Optional[str] = None,
    used_credentials: Optional[set[str]] = None,
    used_env_names: Optional[set[str]] = None,
    actor_user_id: Optional[str] = None,
    redact_values: Optional[set[str]] = None,
    missing_env: MissingEnvPolicy = "raise",
) -> str:
    """Resolve ``${env:}`` then ``${credential:}`` references in one value.

    The two-step order matches the existing MCP env/header resolution: env
    placeholders are substituted first, then any remaining credential references
    are resolved against the (already env-resolved) string.
    """
    resolved, env_used = interpolate_env_vars_with_names(value, missing=missing_env)
    if used_env_names is not None:
        used_env_names.update(env_used)
    return resolve_credential_refs(
        resolved,
        target_type=target_type,
        target_id=target_id,
        used_credentials=used_credentials,
        actor_user_id=actor_user_id,
        redact_values=redact_values,
    )
