"""User-defined command alias bodies (backlog #133).

House style: backend handler families live in their own domain mixin module
rather than being appended to ``command_service.py`` (see the siblings
``command_executor_llm.py`` and ``command_executor_cliproxy.py``).
``_CommandExecutor`` inherits :class:`AliasCommandsMixin`, which owns the
``/alias`` family: the overview/list (with liveness flags from the ONE
policy, ``CommandService.user_alias_status``), create (name shape, catalog
collisions, target resolution, the unquoted-single-word rule), and delete.
Handler methods are resolved by ``CommandService.execute`` via
``getattr(executor, "_cmd_<path>")``. The store and its authoring-stamp
control: ``core/user_aliases.py``; the dispatch-time expansion seam lives in
``CommandService.execute`` itself.

Runtime leaf: imports ``command_forms`` and ``user_aliases``, never
``command_service`` at module scope, so there is no import cycle.
"""

from __future__ import annotations

import shlex
from typing import TYPE_CHECKING

from .command_forms import (
    CommandOutput,
    command_error,
    command_info,
    command_success,
)
from .command_params import BoundArgs

if TYPE_CHECKING:
    from .command_service import CommandService

_ALIAS_NAME_CHARS = frozenset("abcdefghijklmnopqrstuvwxyz0123456789_")

_STATUS_COPY = {
    "inert": " [INERT: failed its authoring stamp; delete and re-create it]",
    "shadowed": " [dormant: a built-in command now claims this spelling]",
    "target_gone": " [dormant: its target command no longer exists]",
}


class AliasCommandsMixin:
    """/alias command bodies mixed into ``_CommandExecutor``.

    The host provides ``user_id``, ``actor``, and ``_service``; the
    annotations below let the static checker see them on the mixin in
    isolation.
    """

    user_id: str
    actor: str
    _service: "CommandService | None"

    def _alias_service(self) -> "CommandService":
        if self._service is not None:
            return self._service
        from .command_service import get_command_service

        return get_command_service()

    async def _cmd_alias(self, bound: BoundArgs) -> str | CommandOutput:
        # Bare family root = overview (structure rule 1): the listing.
        return await self._cmd_alias_list(bound)

    async def _cmd_alias_list(self, bound: BoundArgs) -> str | CommandOutput:
        from .user_aliases import get_user_aliases_repo

        service = self._alias_service()
        aliases = get_user_aliases_repo().list_aliases(self.user_id)
        if not aliases:
            return command_info(
                "No aliases yet. Create one with "
                "`/alias create <name> <command...>`, for example "
                "`/alias create gpt5 model openai/gpt-5.5`."
            )
        lines = [f"### Your Command Aliases ({len(aliases)})", ""]
        for alias in aliases:
            expansion = " ".join(alias.tokens)
            note = _STATUS_COPY.get(service.user_alias_status(alias) or "", "")
            author = " (agent-authored)" if alias.author_actor == "agent" else ""
            lines.append(f"- `/{alias.name}` -> `/{expansion}`{author}{note}")
        return "\n".join(lines)

    async def _cmd_alias_create(self, bound: BoundArgs) -> str | CommandOutput:
        from .command_service import _normalize_token
        from .user_aliases import (
            AliasAlreadyExists,
            AliasLimitReached,
            get_user_aliases_repo,
        )

        service = self._alias_service()
        raw_name = str(bound.get("name") or "")
        name = _normalize_token(raw_name)
        if not name or not set(name) <= _ALIAS_NAME_CHARS:
            return command_error(
                f"Alias names are one word of letters, digits, hyphens or "
                f"underscores; got {raw_name!r}."
            )
        if service._builtin_claims_token(name):
            return command_error(
                f"`/{name}` is a registered command or built-in alias "
                "spelling; an alias must not shadow the catalog."
            )
        # Tokenize the RAW invocation tail, not the bound rest param: the
        # binder recomposes rest from shlex-split args, so grouping quotes
        # are already flattened there and a quoted phrase would silently
        # rebind as separate words at dispatch (`memory save "two words"`
        # becoming key=two value=words). Two shapes are legitimate:
        # plain words, and ONE quoted blob wrapping the WHOLE expansion
        # (the Discord cog and the CLI form rescue both shlex-quote the
        # rest value they compose), which re-splits into plain words. A
        # quoted multi-word VALUE inside the expansion stays refused: a
        # recorded v1 non-goal.
        from .command_service import _split_rest_after_tokens

        raw_tail = _split_rest_after_tokens(bound.rest, 1).lstrip("/")
        try:
            expansion_tokens = shlex.split(raw_tail, posix=True)
        except ValueError:
            return command_error("Unbalanced quote in the alias expansion.")
        if len(expansion_tokens) == 1 and any(
            ch.isspace() for ch in expansion_tokens[0]
        ):
            expansion_tokens = expansion_tokens[0].split()
        if any(
            any(ch.isspace() for ch in token) for token in expansion_tokens
        ):
            return command_error(
                "Expansion values must be single unquoted words; quoted "
                "phrases are not supported in aliases."
            )
        expansion_text = " ".join(expansion_tokens)
        parsed = service._parse_for_registry("/" + expansion_text)
        if parsed.definition is None:
            return command_error(
                f"`/{expansion_text}` does not resolve to a registered "
                "command; an alias must expand to a real one."
            )
        if not parsed.definition.executable:
            return command_error(
                f"`/{parsed.definition.name}` runs as a chat turn, not a "
                "dispatched command; aliasing it is not supported."
            )
        # An expansion that can NEVER dispatch is refused now, not
        # discovered later: extras and invalid values are permanent (no
        # typed tail can remove a token). A MISSING required argument is
        # deliberately allowed: a prefix alias is legitimate (`/nk` for
        # `memory save`, completed as `/nk color blue`).
        if parsed.definition.params is not None:
            from .command_params import bind_args

            _bound, bind_error = bind_args(
                parsed.definition.params, parsed.args, parsed.rest
            )
            if bind_error is not None and not bind_error.missing:
                return command_error(
                    f"`/{expansion_text}` would never dispatch: "
                    f"{bind_error.problem}"
                )
        # Store the CANONICAL expansion (path + bound values): a built-in
        # alias typed in the expansion is resolved once, here, so later
        # catalog renames cannot silently re-point the user's spelling.
        tokens = tuple(parsed.definition.path) + tuple(parsed.args)
        author = "agent" if self.actor == "agent" else "user"
        try:
            created = get_user_aliases_repo().create_alias(
                user_id=self.user_id,
                name=name,
                command_id=parsed.definition.id,
                tokens=tokens,
                author_actor=author,
            )
        except AliasAlreadyExists:
            return command_error(
                f"You already have an alias `/{name}`. "
                f"Delete it first: `/alias delete {name}`."
            )
        except AliasLimitReached as e:
            return command_error(str(e))
        display = " ".join(created.tokens)
        return command_success(f"Alias created: `/{name}` -> `/{display}`.")

    async def _cmd_alias_delete(self, bound: BoundArgs) -> str | CommandOutput:
        from .command_service import _normalize_token
        from .user_aliases import get_user_aliases_repo

        name = _normalize_token(str(bound.get("name") or ""))
        removed = get_user_aliases_repo().delete_alias(
            user_id=self.user_id, name=name
        )
        if not removed:
            return command_error(
                f"No alias `/{name}`. `/alias list` shows yours."
            )
        return command_success(f"Alias `/{name}` deleted.")


__all__ = ["AliasCommandsMixin"]
