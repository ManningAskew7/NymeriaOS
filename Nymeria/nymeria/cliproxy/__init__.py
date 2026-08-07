"""CLIProxy subscription-OAuth integration.

`catalog` is the data-driven table of CLI providers CLIProxy can log into and
how each routes into Nymeria's LLM settings. `management_client` is the async
HTTP client for the proxy's `/v0/management` API (OAuth logins, auth files,
config knobs). Everything here is opt-in: nothing in the runtime requires a
CLIProxy to exist.
"""

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    # Import eagerly for type checkers and IDEs only. At runtime these resolve
    # through __getattr__ below, so nothing here costs import time.
    from .catalog import (
        CLIProxyProviderSpec,
        cliproxy_channel_label,
        cliproxy_data_plane_url,
        get_cliproxy_provider,
        list_cliproxy_providers,
    )
    from .management_client import (
        CLIProxyAuthError,
        CLIProxyConflict,
        CLIProxyManagementClient,
        CLIProxyManagementError,
        CLIProxyNotFound,
        CLIProxyUnreachable,
        CLIProxyUnsupported,
    )

# Re-exports resolve lazily (PEP 562), mirroring the sibling package inits.
# An eager init here pulled management_client -> httpx (~120ms, measured
# 2026-08-07) into every consumer of the pure catalog table, notably the thin
# CLI header on its launch path; tests/test_cli_startup_imports.py now
# forbids httpx at CLI import time to keep it that way.
_LAZY_EXPORTS = {
    "CLIProxyProviderSpec": ".catalog",
    "cliproxy_channel_label": ".catalog",
    "cliproxy_data_plane_url": ".catalog",
    "get_cliproxy_provider": ".catalog",
    "list_cliproxy_providers": ".catalog",
    "CLIProxyAuthError": ".management_client",
    "CLIProxyConflict": ".management_client",
    "CLIProxyManagementClient": ".management_client",
    "CLIProxyManagementError": ".management_client",
    "CLIProxyNotFound": ".management_client",
    "CLIProxyUnreachable": ".management_client",
    "CLIProxyUnsupported": ".management_client",
}


def __getattr__(name: str):
    """Resolve a re-exported symbol by importing only its defining submodule.

    Falls back to importing ``name`` as a submodule so attribute access like
    ``nymeria.cliproxy.management_client`` keeps working after a bare
    ``import nymeria.cliproxy`` (the eager imports this replaced bound the
    touched submodules as package attributes).
    """
    from importlib import import_module

    module = _LAZY_EXPORTS.get(name)
    if module is not None:
        return getattr(import_module(module, __name__), name)

    try:
        return import_module(f".{name}", __name__)
    except ModuleNotFoundError as exc:
        # Only translate "this submodule does not exist". A submodule that
        # exists but fails on a missing dependency must surface its own
        # error, not be masked as a missing attribute.
        if exc.name != f"{__name__}.{name}":
            raise
        raise AttributeError(
            f"module {__name__!r} has no attribute {name!r}"
        ) from None


def __dir__() -> list[str]:
    return sorted(set(__all__) | set(globals()))


__all__ = [
    "CLIProxyProviderSpec",
    "cliproxy_channel_label",
    "cliproxy_data_plane_url",
    "get_cliproxy_provider",
    "list_cliproxy_providers",
    "CLIProxyAuthError",
    "CLIProxyConflict",
    "CLIProxyManagementClient",
    "CLIProxyManagementError",
    "CLIProxyNotFound",
    "CLIProxyUnreachable",
    "CLIProxyUnsupported",
]
