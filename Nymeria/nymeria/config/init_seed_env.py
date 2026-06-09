"""Contract for carrying `nymeria init` picks into a Docker container's first boot.

The setup wizard runs on the host and cannot write into the container's named
`/data` volume, so for the Docker single-container shape the bootstrap admin's
intended `default_thread_tools` and `enabled_global_skills` ride in `.env.docker`
as two name-list env vars. The container reads them exactly once, when it first
creates that profile, reaching parity with a local install. The writer
(`setup/finalize.py` via `setup/tool_seed.docker_init_seed_env`) and the readers, both
on `core/user_profile.UserProfileManager.get_profile`'s lazy creation path
(`_migrate_default_thread_tools` for tools, `_migrate_default_global_skills` for skills),
all import names and (de)serialization from here so the two sides cannot drift.

Values are joined with ``:`` (never a comma): every character of a tool/skill
identifier plus the separator then falls in the safe set of
`config/env_file.format_env_value`, so the line writes UNQUOTED and the
version-fragile Docker `env_file` quote handling is never exercised. Names here
are always simple identifiers (`web_search_tavily`, `tool-management`), so they
never contain the separator.

This module is intentionally dependency-free (stdlib only) so both `config`/`core`
and `setup` can import it without an import cycle.
"""

from __future__ import annotations

import os
from typing import Mapping, Sequence

INIT_DEFAULT_THREAD_TOOLS_ENV = "NYMERIA_INIT_DEFAULT_THREAD_TOOLS"
INIT_ENABLED_GLOBAL_SKILLS_ENV = "NYMERIA_INIT_ENABLED_GLOBAL_SKILLS"

# Separator: in `format_env_value`'s safe set (so values write unquoted) and never
# part of a tool/skill identifier.
_SEP = ":"


def format_init_name_list(names: Sequence[str]) -> str:
    """Render a tool/skill name list as the `:`-joined dotenv RHS.

    Order-preserving dedup with blanks dropped, so the written value matches what
    the reader parses back (`parse_init_name_list` is its inverse).
    """
    out: list[str] = []
    for name in names:
        cleaned = str(name).strip()
        if cleaned and cleaned not in out:
            out.append(cleaned)
    return _SEP.join(out)


def parse_init_name_list(raw: str | None) -> list[str]:
    """Inverse of :func:`format_init_name_list`: a `:`-joined string to a name list.

    Empty/None becomes ``[]``. Blanks are dropped and order-preserving dedup is
    applied so a hand-edited value cannot inject duplicates.
    """
    if not raw:
        return []
    out: list[str] = []
    for part in raw.split(_SEP):
        cleaned = part.strip()
        if cleaned and cleaned not in out:
            out.append(cleaned)
    return out


def init_default_thread_tools_from_env(
    env: Mapping[str, str] | None = None,
) -> list[str] | None:
    """The init-seeded `default_thread_tools`, or None when the var is unset/empty.

    Best-effort and never raises: a missing or blank var returns None so the caller
    falls back to its normal core-seed default.
    """
    source = os.environ if env is None else env
    names = parse_init_name_list(source.get(INIT_DEFAULT_THREAD_TOOLS_ENV))
    return names or None


def init_enabled_global_skills_from_env(
    env: Mapping[str, str] | None = None,
) -> list[str] | None:
    """The init-seeded `enabled_global_skills`, or None when the var is unset/empty.

    Best-effort and never raises: a missing or blank var returns None so the caller
    falls back to `DEFAULT_GLOBAL_SKILLS`.
    """
    source = os.environ if env is None else env
    names = parse_init_name_list(source.get(INIT_ENABLED_GLOBAL_SKILLS_ENV))
    return names or None


__all__ = [
    "INIT_DEFAULT_THREAD_TOOLS_ENV",
    "INIT_ENABLED_GLOBAL_SKILLS_ENV",
    "format_init_name_list",
    "parse_init_name_list",
    "init_default_thread_tools_from_env",
    "init_enabled_global_skills_from_env",
]
