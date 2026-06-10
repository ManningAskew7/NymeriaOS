"""CLIProxy subscription-OAuth integration.

`catalog` is the data-driven table of CLI providers CLIProxy can log into and
how each routes into Nymeria's LLM settings. `management_client` is the async
HTTP client for the proxy's `/v0/management` API (OAuth logins, auth files,
config knobs). Everything here is opt-in: nothing in the runtime requires a
CLIProxy to exist.
"""

from .catalog import (
    CLIProxyProviderSpec,
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

__all__ = [
    "CLIProxyProviderSpec",
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
