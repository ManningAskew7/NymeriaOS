"""Generated picker forms from declared params (backlog #110).

When a form-capable client invokes a schema'd command without its required
argument, the dispatcher rescues the missing-required bind failure into a
picker generated HERE from the command's declaration, instead of the usage
error every other caller keeps. One builder, driven entirely by
``CommandParam`` metadata plus the shared option-resolver registry; nothing
per-command is hand-authored.

Generation rules (per-leaf single-tab, dev-locked 2026-08-03):

- The form fills exactly ONE param: the sole required positional/rest param
  (the "primary"). A command with two required params, or a required option,
  cannot be completed by a one-field form and generates nothing.
- Pickers only where picking helps: the primary needs an option set, from
  static ``choices`` or a resolver-backed ``choices_ref``. Free-text
  primaries keep the plain usage error (typing the argument IS the form).
  Label-only alternations are free text by the #129 rule and never become
  options.
- A ``scope`` param expands to one tab per offerable scope (the /think
  pattern): distinct field keys per tab, per-tab submit templates, thread
  tab only when the caller has a thread. Flags and optional params stay out
  of generated forms entirely; advanced spellings stay typed.
- A declared ``no_echo`` param anywhere on the command disables generation
  (secret-adjacent grammar must not become a typed form), as does an entry
  in ``EXCLUDED_COMMANDS``.

Consumed by ``CommandService.execute()``; imports only sibling leaf modules
(``command_forms``, ``command_params``, ``command_option_resolvers``), never
``command_service``.
"""

from __future__ import annotations

import logging
from typing import Any

from .command_forms import (
    form_option,
    form_payload,
    form_tab,
    radio_field,
    search_field,
)
from .command_option_resolvers import OPTION_RESOLVERS
from .command_params import SCOPE_CHOICES, CommandParam

logger = logging.getLogger(__name__)

# Commands whose declaration LOOKS generatable but whose semantics a
# single-select picker would betray. Key = command id, value = the reason
# (kept here so a future adoption is a recorded decision, mirroring the
# Discord generator's exclusion idiom).
EXCLUDED_COMMANDS: dict[str, str] = {
    "fallback.set": (
        "ordered multi-model chain; a single-select picker would quietly "
        "write a one-model chain"
    ),
}

# Above this option count the tab gets a filter line (the /model shape);
# at or below it a bare radio is faster to arrow through.
SEARCH_THRESHOLD = 10

# Refs whose resolver-computed `current` marker is correct for EVERY command
# declaring the ref. The marker drives the picker's default action (the
# cursor parks on it, a reflexive Enter applies it), and a resolver cannot
# know which command it is feeding: the models ref marks the CHAT model,
# which is the wrong default for /fast set, /background set, and friends
# (measured: a reflexive Enter overwrote the tier with the chat model).
# threads is safe by construction: current = the calling thread, and
# re-switching to it is a no-op. Everything else is stripped.
KEEP_CURRENT_REFS: frozenset[str] = frozenset({"threads"})

_SCOPE_TAB_LABELS = {"thread": "This thread", "global": "Global"}


def _primary_param(params: tuple[CommandParam, ...]) -> CommandParam | None:
    """The single param a generated form fills, or None when the shape
    cannot be completed by one field."""
    required = [p for p in params if p.required]
    if len(required) != 1:
        return None
    primary = required[0]
    if primary.kind not in ("positional", "rest"):
        return None
    return primary


def _command_token_string(path: tuple[str, ...]) -> str:
    """The dispatchable command string for a path, hyphen-rendered the way
    ``generated_usage`` advertises it (parsing normalizes back)."""
    return " ".join(token.replace("_", "-") for token in path)


async def _primary_options(
    primary: CommandParam, executor: Any
) -> list[dict[str, Any]]:
    if primary.choices:
        return [form_option(choice) for choice in primary.choices]
    if primary.choices_ref:
        resolver = OPTION_RESOLVERS.get(primary.choices_ref)
        if resolver is None:
            return []
        try:
            options = await resolver(executor)
        except Exception:  # noqa: BLE001 - a form is an optional enhancement.
            logger.debug(
                "form generation: resolver %s failed",
                primary.choices_ref,
                exc_info=True,
            )
            return []
        if primary.choices_ref not in KEEP_CURRENT_REFS:
            options = [{**option, "current": False} for option in options]
        return options
    return []


def _offerable_scopes(scope_param: CommandParam, executor: Any) -> list[str]:
    scopes = [
        scope
        for scope in (scope_param.choices or SCOPE_CHOICES)
        if scope in SCOPE_CHOICES
    ]
    if not getattr(executor, "thread_id", None):
        scopes = [scope for scope in scopes if scope != "thread"]
    if getattr(executor, "is_admin", None) is False:
        # WRITABLE scopes only (the /think picker's rule, and the provider
        # action step's one non-registry condition): the global scope's
        # write gate is per-scope inside the handlers, so a non-admin tab
        # would submit a command the gate then refuses.
        scopes = [scope for scope in scopes if scope != "global"]
    # Thread first when present: the /think tab order, and the scope a
    # bare invocation would target.
    return sorted(scopes, key=lambda scope: 0 if scope == "thread" else 1)


async def generate_param_form(
    definition: Any, executor: Any
) -> dict[str, Any] | None:
    """Build the generated picker for ``definition``, or None when the
    declaration does not support one (the caller falls through to the
    normal usage error).

    ``definition`` is a ``CommandDefinition`` (duck-typed to keep this
    module import-light); ``executor`` is the constructed
    ``_CommandExecutor`` whose doors the option resolvers read through.
    """
    params: tuple[CommandParam, ...] = definition.params or ()
    if not params:
        return None
    if definition.id in EXCLUDED_COMMANDS:
        return None
    if any(param.no_echo for param in params):
        return None
    primary = _primary_param(params)
    if primary is None:
        return None

    options = await _primary_options(primary, executor)
    if not options:
        return None

    fields: list[dict[str, Any]] = []
    if len(options) > SEARCH_THRESHOLD:
        fields.append(
            search_field("filter", placeholder=f"Filter {primary.display}…")
        )

    command_tokens = _command_token_string(definition.path)
    scope_param = next((p for p in params if p.kind == "scope"), None)

    if scope_param is None:
        key = primary.name
        tabs = [
            form_tab(
                definition.path[-1].replace("_", "-").capitalize(),
                [*fields, radio_field(key, options)],
            )
        ]
        submit = f"{command_tokens} {{{key}}}"
        footer = "Enter apply · Esc cancel"
        return form_payload(
            f"/{command_tokens}", tabs, submit_command=submit, footer_hint=footer
        )

    scopes = _offerable_scopes(scope_param, executor)
    if not scopes:
        return None
    tabs = []
    for scope in scopes:
        # Distinct field keys per tab: the CLI renderer keys its cursor by
        # field key alone, so a shared key would park both tabs' cursors on
        # one value (the /think collision rule).
        key = f"{primary.name}_{scope}"
        tabs.append(
            form_tab(
                _SCOPE_TAB_LABELS.get(scope, scope.capitalize()),
                [*fields, radio_field(key, options)],
                submit_command=f"{command_tokens} {{{key}}} {scope}",
            )
        )
    first_key = f"{primary.name}_{scopes[0]}"
    footer = (
        "Enter apply · Esc cancel"
        if len(tabs) == 1
        else "←→ tab · Enter apply · Esc cancel"
    )
    return form_payload(
        f"/{command_tokens}",
        tabs,
        submit_command=f"{command_tokens} {{{first_key}}} {scopes[0]}",
        footer_hint=footer,
    )
