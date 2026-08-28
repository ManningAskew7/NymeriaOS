"""Nymeria configuration module.

GOTCHA-DENSE area (tier order, curated-over-catalog, per-source token-cap
semantics): read the backend guide's ``config/`` row (``Nymeria/CLAUDE.md``)
before changing behavior here. Env vars are documented in
``docs/configuration.md``.
"""

from .settings import Settings, get_settings

__all__ = ["Settings", "get_settings"]
