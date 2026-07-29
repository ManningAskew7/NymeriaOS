"""Nymeria triggers module."""

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    # Import eagerly for type checkers and IDEs only. At runtime these resolve
    # through __getattr__ below, so nothing here costs import time.
    from .cli import CLITrigger
    from .api import create_api_app

# Re-exports are resolved lazily (PEP 562), mirroring `nymeria/__init__.py`.
# `.api` builds the whole FastAPI app, the agent and all 1,259 catalog tools
# (~13.5s). Eagerly importing it here meant reaching ANY sibling paid for the
# server: `from nymeria.triggers.cli import run_cli` cost ~14.9s, which is why
# the thin CLI (`--transport api`, the default) took ~16s to launch while
# never using a single line of it.
#
# Trigger sources are NOT imported here. `sources/__init__.py` auto-loads its
# plugins at module scope, and every consumer (`core/trigger_manager.py`,
# `triggers/trigger_api.py`, `tools/triggers.py`) imports `.sources`
# function-locally at the point of use, so the registry is populated on first
# access. The old eager import only forced that work earlier.
_LAZY_EXPORTS = {
    "CLITrigger": ".cli",
    "create_api_app": ".api",
}


def __getattr__(name: str):
    """Resolve a re-exported symbol by importing only its defining submodule.

    Falls back to importing ``name`` as a submodule: the eager imports this
    replaced also bound every submodule they touched as a package attribute,
    so `nymeria.triggers.sources` kept working without a direct import.
    """
    from importlib import import_module

    module = _LAZY_EXPORTS.get(name)
    if module is not None:
        return getattr(import_module(module, __name__), name)

    try:
        return import_module(f".{name}", __name__)
    except ModuleNotFoundError as exc:
        # Only translate "this submodule does not exist". A submodule that
        # exists but fails on a missing dependency must surface its own error,
        # not be masked as a missing attribute.
        if exc.name != f"{__name__}.{name}":
            raise
        raise AttributeError(
            f"module {__name__!r} has no attribute {name!r}"
        ) from None


def __dir__() -> list[str]:
    # Augment the default (`vars()`), never replace it: __getattr__ makes
    # submodules reachable, so hiding them here (along with __name__ and
    # already-imported submodules) would be a strict regression.
    return sorted(set(__all__) | set(globals()))


__all__ = ["CLITrigger", "create_api_app"]
