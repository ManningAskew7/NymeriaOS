"""Shared allowlist environment for spawned child processes.

Every subprocess Nymeria launches for tool/agent work (the ``bash_execute``
shell, ``nym`` workflow author code, the ``run_command`` hook action, Python
custom tools, and MCP stdio servers) must run with a deny-by-default
environment: the API process carries the master encryption key
(``NYMERIA_SECRETS_KEY``), the database/Redis credentials, provider API keys,
and the service token, none of which a child should inherit (they would leak
into command output, LLM context, or an untrusted server's reach).

This is the single source of truth for that allowlist. It exists because the
same allowlist was previously copy-pasted per surface and drifted: two
surfaces (Python custom tools and MCP stdio servers) started from
``os.environ.copy()`` and handed children the full secret-bearing environment.
Callers layer their own non-secret extras (run identifiers, declared server
env vars) on top of the base returned here.

Note this is defense-in-depth, not an isolation boundary: a child can still
read the parent's environment from ``/proc/<pid>/environ`` on the same host.
Closing that requires an execution sandbox (tracked separately); scrubbing the
inherited environment is the cheap, shape-independent layer.
"""

from __future__ import annotations

import os
from collections.abc import Iterable

# Windows child-process essentials. Without SystemRoot/ComSpec a ``cmd.exe``
# or DLL-loading child fails to start at all, and PATHEXT/TEMP govern basic
# executable resolution and temp placement, so these belong in the base for
# EVERY exec surface, not just the networked ones that used to opt them in.
# On POSIX none of these are set, so including them here is a no-op there
# (the scrubber only copies variables that actually exist): no platform
# branch needed. None are secrets. The ProgramFiles trio is what the Windows
# Docker CLI walks to find its plugins (%ProgramFiles%\Docker\cli-plugins);
# without it the CLI has no `compose` subcommand (`docker compose up -d` dies
# with "unknown shorthand flag: 'd' in -d") and every compose surface fails,
# the wizard's CLIProxy deploy first.
WINDOWS_RUNTIME_PASSTHROUGH: tuple[str, ...] = (
    "SystemRoot", "windir", "ComSpec", "PATHEXT", "SYSTEMDRIVE",
    "HOMEDRIVE", "HOMEPATH", "USERPROFILE", "APPDATA", "LOCALAPPDATA",
    "PROGRAMDATA", "ProgramFiles", "ProgramFiles(x86)", "ProgramW6432",
    "TEMP", "TMP",
)

# The minimal, non-secret base a spawned child may inherit. Deliberately small:
# enough to find executables and behave with correct locale/temp paths, nothing
# that identifies or authenticates the deployment.
BASE_SUBPROCESS_ENV_PASSTHROUGH: tuple[str, ...] = (
    "PATH",
    "HOME",
    "LANG",
    "LC_ALL",
    "TMPDIR",
) + WINDOWS_RUNTIME_PASSTHROUGH

# Non-secret runtime/network/TLS variables that package managers (npm, uv,
# pip), HTTPS clients, and TLS trust stores need but that carry no Nymeria
# secret. The surfaces that fetch/build/network (MCP stdio launch, MCP install,
# Python custom tools) opt these back in so a proxied / custom-CA / Windows
# deployment keeps working; they used to arrive via ``os.environ.copy()`` and
# the minimal base deliberately omits them. None of these are Nymeria's own
# secrets (master key, DB/Redis creds, provider keys, service token). Proxy
# URLs may embed the operator's own proxy credentials, which are required for
# connectivity and are the operator's infra, not the platform's secrets.
NETWORK_RUNTIME_PASSTHROUGH: tuple[str, ...] = (
    # HTTP(S) proxy (upper and lower case both honored by common clients)
    "HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY", "NO_PROXY",
    "http_proxy", "https_proxy", "all_proxy", "no_proxy",
    # TLS trust stores / custom CA bundles
    "SSL_CERT_FILE", "SSL_CERT_DIR", "REQUESTS_CA_BUNDLE",
    "CURL_CA_BUNDLE", "NODE_EXTRA_CA_CERTS", "PIP_CERT",
    # XDG base dirs (npm/uv/pip cache/config/data locations)
    "XDG_CACHE_HOME", "XDG_CONFIG_HOME", "XDG_DATA_HOME",
    # The Windows runtime essentials formerly listed here moved into the
    # always-on base (WINDOWS_RUNTIME_PASSTHROUGH): every exec surface needs
    # them on Windows, not just the fetch/build/network ones.
)


# What the `docker` CLI needs to reach the daemon the operator actually uses.
# Without these a scrubbed probe silently answers about the WRONG docker (or no
# docker) on a rootless, remote, or multi-context host, which in the setup
# wizard means telling someone Docker is unavailable when it is running fine.
# None are Nymeria secrets; DOCKER_CERT_PATH names a directory of client certs
# the operator already owns.
DOCKER_CLI_PASSTHROUGH: tuple[str, ...] = (
    "DOCKER_HOST",
    "DOCKER_CONTEXT",
    "DOCKER_CONFIG",
    "DOCKER_CERT_PATH",
    "DOCKER_TLS_VERIFY",
)


def scrubbed_subprocess_env(extra_names: Iterable[str] = ()) -> dict[str, str]:
    """Return an allowlisted copy of ``os.environ`` for a child process.

    Includes only :data:`BASE_SUBPROCESS_ENV_PASSTHROUGH` plus any
    ``extra_names`` the caller explicitly opts in, and only when the variable
    is actually set. Everything else, crucially the API process's secrets, is
    dropped. Callers add their own non-secret variables to the returned dict.
    """
    names = set(BASE_SUBPROCESS_ENV_PASSTHROUGH)
    names.update(name for name in extra_names if name)
    return {name: os.environ[name] for name in names if name in os.environ}


__all__ = [
    "BASE_SUBPROCESS_ENV_PASSTHROUGH",
    "DOCKER_CLI_PASSTHROUGH",
    "NETWORK_RUNTIME_PASSTHROUGH",
    "WINDOWS_RUNTIME_PASSTHROUGH",
    "scrubbed_subprocess_env",
]
