"""Trigger source registry and auto-discovery.

Mirrors the pattern in ``nymeria/agents/__init__.py``: each ``.py`` file in
this directory that calls ``register_source()`` at module level is
auto-discovered on import.
"""

import importlib
import logging
import sys
from pathlib import Path
from typing import Any, Dict, List, Optional, Type

from ...core.secret_masking import mask_secret_value
from .base import BaseTriggerSource

logger = logging.getLogger(__name__)

# Registry: source name -> source instance
AVAILABLE_SOURCES: Dict[str, BaseTriggerSource] = {}


def _iter_source_module_names() -> List[str]:
    """Return the importable module name for each source plugin file.

    Shared by :func:`reload_sources` and :func:`_auto_load_sources`; skips
    private files (``_*``) and ``base.py``. Sorted for deterministic load
    order.
    """
    sources_dir = Path(__file__).parent
    return [
        f"nymeria.triggers.sources.{py_file.stem}"
        for py_file in sorted(sources_dir.glob("*.py"))
        if not py_file.name.startswith("_") and py_file.name != "base.py"
    ]


def register_source(name: str, source_class: Type[BaseTriggerSource]) -> None:
    """Register a trigger source plugin.

    Args:
        name: Unique source name (e.g. ``"webhook"``, ``"github_issues"``).
        source_class: A *class* (not instance) that extends BaseTriggerSource.
    """
    if name in AVAILABLE_SOURCES:
        logger.warning(f"Re-registering trigger source '{name}' (replacing existing)")

    instance = source_class()
    instance.on_register()
    AVAILABLE_SOURCES[name] = instance
    logger.info(f"Registered trigger source: {name}")


def get_source(name: str) -> Optional[BaseTriggerSource]:
    """Get a registered source instance by name."""
    return AVAILABLE_SOURCES.get(name)


def list_sources() -> Dict[str, Dict[str, Any]]:
    """Return enriched metadata for all registered sources.

    Returns:
        Dict mapping source name to full metadata including category,
        icon, setup guide, template variables, and example config.
    """
    return {
        name: {
            "name": name,
            "description": source.description,
            "config_schema": source.config_schema,
            "category": source.category,
            "icon": source.icon,
            "setup_guide": source.setup_guide,
            "template_variables": source.template_variables,
            "example_config": source.example_config,
            "requires_auth": source.requires_auth,
        }
        for name, source in AVAILABLE_SOURCES.items()
    }


def reload_sources() -> int:
    """Clear the registry and re-import all source modules.

    Returns:
        Number of sources registered after reload.
    """
    AVAILABLE_SOURCES.clear()

    count = 0
    for module_name in _iter_source_module_names():
        # Remove from cache so re-import triggers register_source() again
        if module_name in sys.modules:
            del sys.modules[module_name]

        try:
            importlib.import_module(module_name)
            count += 1
            logger.debug(f"Loaded trigger source module: {module_name}")
        except Exception as e:
            logger.error(f"Failed to load trigger source module {module_name}: {e}")

    logger.info(f"Reloaded {count} source module(s), {len(AVAILABLE_SOURCES)} source(s) registered")
    return len(AVAILABLE_SOURCES)


# ---------------------------------------------------------------------------
# Secret handling for source_config (#307)
# ---------------------------------------------------------------------------

# Fallback for a source type that is not registered (a plugin removed from the
# build, or one that failed to import). We cannot ask a class that is not there
# which of its fields are secret, so guess by NAME and fail safe: masking a
# harmless field is a cosmetic bug, printing a live credential is not. Only
# ever consulted when the schema is genuinely unavailable.
_SECRET_NAME_HINTS = (
    "secret",
    "token",
    "password",
    "api_key",
    "apikey",
    "credential",
    "auth",
)


def secret_field_names(source_type: str) -> Optional[set]:
    """Return the config keys a source declares secret, or None if unknown.

    The declaration is the per-field ``secret: True`` flag that has been part
    of the documented config-schema contract since the beginning (see
    ``base.py``), and is already set on ``webhook.secret`` and
    ``slack.bot_token``. Until #307 the only consumers were the two GUI setup
    wizards, which used it to pick a password input; no server-side renderer
    read it, so every backend surface printed the values in full.

    ``None`` (unknown source) is deliberately distinct from an empty set (a
    source that declares no secrets): the caller has to fail safe on the
    former and can print freely on the latter.
    """
    source = AVAILABLE_SOURCES.get(source_type)
    if source is None:
        return None
    schema = getattr(source, "config_schema", None) or {}
    return {
        key
        for key, spec in schema.items()
        if isinstance(spec, dict) and spec.get("secret") is True
    }


def _is_secret_key(key: str, declared: Optional[set]) -> bool:
    if declared is not None:
        return key in declared
    lowered = key.lower()
    return any(hint in lowered for hint in _SECRET_NAME_HINTS)


def redact_source_config(source_type: str, config: dict) -> dict:
    """Return a copy of ``config`` with declared-secret values fingerprinted.

    Rendering only. The stored config is never touched: a webhook trigger's
    secret is also its OWNER LOOKUP KEY (the anonymous ``POST
    /triggers/fire/{id}`` route derives the owner by finding whichever stored
    secret matches), so redacting at rest would break anonymous firing
    outright.

    Dict-valued secret fields are masked per VALUE rather than whole, because
    the case that needs it is ``http_poll.headers``: a free-form object whose
    values are arbitrary ``Authorization`` headers. Masking the dict as a unit
    would hide the header NAMES too, which are exactly the part a reader
    debugging a poll needs to see.
    """
    if not isinstance(config, dict):
        return config
    declared = secret_field_names(source_type)
    redacted = {}
    for key, value in config.items():
        if not _is_secret_key(key, declared):
            redacted[key] = value
        elif isinstance(value, dict):
            redacted[key] = {
                inner_key: mask_secret_value(str(inner_value))
                for inner_key, inner_value in value.items()
            }
        elif value in (None, ""):
            redacted[key] = value
        else:
            redacted[key] = mask_secret_value(str(value))
    return redacted


def redacted_secret_keys(source_type: str, config: dict) -> List[str]:
    """Which keys ``redact_source_config`` ACTUALLY masked, not which it would
    consider.

    Shipped on the REST read model so a client never has to guess by key name.
    Derived by comparing the two dicts rather than re-running the predicate:
    an empty or unset secret is left alone by the redactor, and reporting it
    as masked would put a "masked" chip beside a visibly empty value, on a
    field whose whole selling point is that it is authoritative.
    """
    if not isinstance(config, dict):
        return []
    redacted = redact_source_config(source_type, config)
    return sorted(key for key, value in config.items() if redacted.get(key) != value)


class MaskedSecretRejected(ValueError):
    """A caller tried to STORE a fingerprint as if it were the real secret.

    The failure mode masking creates. An agent reads a trigger's detail view,
    sees `secret: "QA-C...1d0"`, is asked for "another one like that", and
    passes the fingerprint into a CREATE. Nothing compares it against a stored
    value (there is none yet), so an 11-character string that is already
    sitting in the transcript, the checkpoints and the RAG index becomes a
    live credential on the anonymous fire route. The user meanwhile points
    their external service at the real secret and gets 403.

    Update repairs this silently (`restore_unchanged_secrets`); create can
    only refuse, so it refuses loudly.
    """

    def __init__(self, key: str) -> None:
        self.key = key
        super().__init__(
            f"'{key}' was given the masked fingerprint of an existing "
            f"trigger's secret, not a real value. Secrets are never shown in "
            f"full, so a value read from a trigger's details cannot be reused "
            f"to create another one: supply the actual secret."
        )


def masked_secret_collision(
    source_type: str, config: dict, existing_configs: list
) -> Optional[str]:
    """Return the key whose value is a fingerprint of an existing secret.

    Deterministic, not a heuristic: the mask is a pure function, so this asks
    the exact question "is this string the mask of something we already
    store", never "does this look mask-shaped".
    """
    if not isinstance(config, dict):
        return None
    declared = secret_field_names(source_type)
    known = {
        mask_secret_value(str(value))
        for existing in existing_configs
        if isinstance(existing, dict)
        for value in existing.values()
        if isinstance(value, str) and value
    }
    for key, value in config.items():
        if _is_secret_key(key, declared) and isinstance(value, str) and value in known:
            return key
    return None


def unchanged_secret_keys(source_type: str, incoming: dict, stored: dict) -> List[str]:
    """Which secret keys ``restore_unchanged_secrets`` would put back.

    So a write surface can SAY that it discarded a fingerprint. Create refuses
    an echoed mask loudly; without this, update accepted one and answered a
    bare "updated", which is the same input and the same discard reported two
    different ways. A caller then cannot tell "my new secret was saved" from
    "my new secret was ignored" without firing the webhook to find out (a live
    verification run did exactly that).
    """
    if not isinstance(incoming, dict) or not isinstance(stored, dict):
        return []
    restored = restore_unchanged_secrets(source_type, incoming, stored)
    return sorted(
        key for key, value in incoming.items() if restored.get(key) != value
    )


def restore_unchanged_secrets(
    source_type: str, incoming: dict, stored: dict
) -> dict:
    """Put back any secret the caller echoed to us as its own fingerprint.

    Both GUI setup wizards prefill their edit form from the trigger read model
    and POST the WHOLE ``source_config`` back on save, so once the read model
    masks a secret, an ordinary "rename this trigger" save would otherwise
    write the fingerprint over the real credential. A value that exactly
    equals the mask of what is stored therefore means "unchanged".

    A caller who genuinely wants to SET a secret to the literal fingerprint of
    the current one gets a no-op. That collision requires knowing the mask and
    choosing it as the new value, and the outcome is that the working secret
    keeps working.
    """
    if not isinstance(incoming, dict) or not isinstance(stored, dict):
        return incoming
    declared = secret_field_names(source_type)
    merged = dict(incoming)
    for key, value in incoming.items():
        if not _is_secret_key(key, declared) or key not in stored:
            continue
        stored_value = stored[key]
        if isinstance(value, dict) and isinstance(stored_value, dict):
            merged[key] = {
                inner_key: (
                    stored_value[inner_key]
                    if inner_key in stored_value
                    and inner_value == mask_secret_value(str(stored_value[inner_key]))
                    else inner_value
                )
                for inner_key, inner_value in value.items()
            }
        elif (
            isinstance(value, str)
            and stored_value not in (None, "")
            and value == mask_secret_value(str(stored_value))
        ):
            merged[key] = stored_value
    return merged


# ---------------------------------------------------------------------------
# Auto-load sources on import
# ---------------------------------------------------------------------------

def _auto_load_sources():
    """Auto-load all source plugins in this directory."""
    for module_name in _iter_source_module_names():
        try:
            importlib.import_module(module_name)
            logger.info(f"Auto-loaded trigger source: {module_name}")
        except Exception as e:
            logger.warning(f"Failed to auto-load trigger source {module_name}: {e}", exc_info=True)


_auto_load_sources()
