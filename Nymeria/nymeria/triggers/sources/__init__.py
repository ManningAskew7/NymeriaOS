"""Trigger source registry and auto-discovery.

Mirrors the pattern in ``nymeria/agents/__init__.py``: each ``.py`` file in
this directory that calls ``register_source()`` at module level is
auto-discovered on import.
"""

import importlib
import logging
import sys
from pathlib import Path
from typing import Any, Dict, Optional, Type

from .base import BaseTriggerSource

logger = logging.getLogger(__name__)

# Registry: source name -> source instance
AVAILABLE_SOURCES: Dict[str, BaseTriggerSource] = {}


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
    """Return metadata for all registered sources.

    Returns:
        Dict mapping source name to ``{description, config_schema}``.
    """
    return {
        name: {
            "name": name,
            "description": source.description,
            "config_schema": source.config_schema,
        }
        for name, source in AVAILABLE_SOURCES.items()
    }


def reload_sources() -> int:
    """Clear the registry and re-import all source modules.

    Returns:
        Number of sources registered after reload.
    """
    AVAILABLE_SOURCES.clear()

    sources_dir = Path(__file__).parent

    count = 0
    for py_file in sorted(sources_dir.glob("*.py")):
        if py_file.name.startswith("_") or py_file.name == "base.py":
            continue

        module_name = f"nymeria.triggers.sources.{py_file.stem}"

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
# Auto-load sources on import
# ---------------------------------------------------------------------------

def _auto_load_sources():
    """Auto-load all source plugins in this directory."""
    sources_dir = Path(__file__).parent

    for py_file in sorted(sources_dir.glob("*.py")):
        if py_file.name.startswith("_") or py_file.name == "base.py":
            continue

        module_name = f"nymeria.triggers.sources.{py_file.stem}"
        try:
            importlib.import_module(module_name)
            logger.info(f"Auto-loaded trigger source: {module_name}")
        except Exception as e:
            logger.warning(f"Failed to auto-load trigger source {module_name}: {e}", exc_info=True)


_auto_load_sources()
