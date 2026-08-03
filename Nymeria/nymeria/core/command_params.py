"""Declared argument schemas for slash commands (backlog #129).

One grammar for every schema'd command: :class:`CommandParam` declares an
argument, :func:`bind_args` parses and validates a command's raw args against
the declared params, and :func:`generated_usage` renders the canonical usage
string so usage copy can never drift from what the dispatcher enforces.

``command_service`` imports this module; nothing here imports from
``command_service`` (one-way dependency, same rule as the executor mixins).

Grammar notes, deliberate and load-bearing:

- Options accept BOTH ``--name value`` and ``--name=value``. Before this
  module the two spellings were supported by different ad-hoc parsers with
  different behavior per command.
- A ``scope`` param is the trailing ``global|thread`` token: it is popped
  from the END of the token list before positionals are assigned, mirroring
  the strongest hand-rolled precedent (``/think``). A command cannot declare
  both ``scope`` and ``rest`` (the pop would steal the last rest word).
- A ``rest`` param is the JOIN of the remaining tokens, matching the
  ``" ".join(args)`` idiom the title/prompt handlers already used. Handlers
  that need the true raw tail (quote-preserving) must not adopt params.
- Once the first rest/repeatable-positional token is consumed, later
  ``--tokens`` belong to that value verbatim, so option-looking words inside
  a title never error. Options therefore must precede free text.
- Binding is STRICT: unknown options and unexpected extra positionals are
  errors. Commands where trailing words must never block execution
  (``/stop``, ``/clear``, ``/restart``) simply stay unadopted.
"""

from __future__ import annotations

import re
import shlex
from dataclasses import dataclass, field
from typing import Any, Literal

ParamKind = Literal["positional", "option", "flag", "rest", "scope"]
ParamType = Literal["str", "int", "bool"]

SCOPE_CHOICES: tuple[str, ...] = ("global", "thread")

_NAME_RE = re.compile(r"^[a-z][a-z0-9_]*$")


@dataclass(frozen=True)
class CommandParam:
    """One declared argument of a slash command.

    ``choices`` is the statically enforced value set; ``choices_ref`` names a
    DYNAMIC value set ("models", "tools", ...) as advisory metadata for
    consumers (Discord autocomplete, form generation). The dispatcher never
    enforces ``choices_ref``; dynamic validation stays in the handler where
    the live data lives.

    ``no_echo`` keeps a rejected value out of the error copy: a mistyped
    invocation of a command that handles credentials may paste a secret where
    an argument was expected (the ``_parse_provider_values`` precedent).
    """

    name: str
    kind: ParamKind = "positional"
    type: ParamType = "str"
    required: bool = False
    choices: tuple[str, ...] = ()
    choices_ref: str | None = None
    default: str | int | bool | None = None
    repeatable: bool = False
    aliases: tuple[str, ...] = ()
    description: str = ""
    no_echo: bool = False
    # Display override for usage strings and error copy. Names are constrained
    # snake_case identifiers; ``label`` frees the ADVERTISED form ("id-or-title",
    # "on|off|toggle") without widening what the binder enforces. This is the
    # companion of the no-choices rule: when a handler accepts more synonyms
    # than the advertised set, declare no ``choices`` and put the advertised
    # set in ``label``.
    label: str | None = None

    @property
    def option_spelling(self) -> str:
        """Canonical ``--name`` spelling for option/flag kinds."""
        return "--" + self.name.replace("_", "-")

    @property
    def display(self) -> str:
        """How errors and help refer to this param."""
        if self.kind in ("option", "flag"):
            return self.option_spelling
        return self.label or self.name


@dataclass(frozen=True)
class BoundArgs:
    """Validated, coerced values handed to an adopted handler.

    ``values`` maps param name to its coerced value: absent optional params
    are simply absent (``get`` returns the fallback), absent flags are
    ``False``, absent repeatable params are ``[]``, and declared defaults are
    applied. ``tokens`` and ``rest`` keep the original input readable for the
    rare handler that needs it; new parsing on top of them is a review smell.
    """

    values: dict[str, Any] = field(default_factory=dict)
    tokens: tuple[str, ...] = ()
    rest: str = ""

    def get(self, name: str, default: Any = None) -> Any:
        return self.values.get(name, default)


@dataclass(frozen=True)
class BindError:
    """A validation failure, rendered by the dispatcher in the usage idiom."""

    problem: str
    param: str | None = None


def validate_params(command_id: str, params: tuple[CommandParam, ...]) -> None:
    """Registry-construction checks for one command's param declaration.

    Raises ``ValueError`` naming the command and the rule, mirroring
    ``validate_registry()``'s style. Every rule here exists because the
    binder or the usage renderer relies on it.
    """
    seen_names: set[str] = set()
    seen_spellings: set[str] = set()
    saw_optional_positional = False
    saw_free_tail = False  # rest or repeatable positional
    kinds = [p.kind for p in params]
    if kinds.count("rest") > 1:
        raise ValueError(f"Command {command_id} declares more than one rest param")
    if kinds.count("scope") > 1:
        raise ValueError(f"Command {command_id} declares more than one scope param")
    if "rest" in kinds and "scope" in kinds:
        raise ValueError(
            f"Command {command_id} declares both scope and rest params; the "
            "trailing scope pop would steal the last rest word"
        )
    for index, param in enumerate(params):
        if not _NAME_RE.match(param.name):
            raise ValueError(
                f"Command {command_id} param {param.name!r} is not a valid "
                "snake_case identifier"
            )
        if param.name in seen_names:
            raise ValueError(f"Command {command_id} duplicates param {param.name}")
        seen_names.add(param.name)

        if param.kind in ("option", "flag"):
            spellings = (param.option_spelling, *param.aliases)
            for spelling in spellings:
                if not spelling.startswith("-"):
                    raise ValueError(
                        f"Command {command_id} param {param.name} alias "
                        f"{spelling!r} must start with '-'"
                    )
                if spelling in seen_spellings:
                    raise ValueError(
                        f"Command {command_id} reuses option spelling {spelling}"
                    )
                seen_spellings.add(spelling)
        elif param.aliases:
            raise ValueError(
                f"Command {command_id} param {param.name}: only options and "
                "flags may declare aliases"
            )

        if param.kind == "flag":
            if param.type != "bool":
                raise ValueError(
                    f"Command {command_id} flag {param.name} must be type bool"
                )
            if param.choices or param.repeatable or param.required:
                raise ValueError(
                    f"Command {command_id} flag {param.name} cannot be "
                    "required, repeatable, or carry choices"
                )
            if param.default not in (None, False):
                raise ValueError(
                    f"Command {command_id} flag {param.name} default must be "
                    "absent or False"
                )
        else:
            if param.type == "bool":
                raise ValueError(
                    f"Command {command_id} param {param.name}: type bool is "
                    "for flags; use choices for on/off positionals"
                )

        if param.choices:
            if param.type != "str":
                raise ValueError(
                    f"Command {command_id} param {param.name}: choices "
                    "require type str"
                )
            if param.default is not None and str(param.default) not in param.choices:
                raise ValueError(
                    f"Command {command_id} param {param.name} default "
                    f"{param.default!r} is not among its choices"
                )

        if param.kind == "scope":
            if param.choices and param.choices != SCOPE_CHOICES:
                raise ValueError(
                    f"Command {command_id} scope param {param.name} may not "
                    "override the global|thread choice set"
                )
            if param.required or param.repeatable:
                raise ValueError(
                    f"Command {command_id} scope param {param.name} cannot be "
                    "required or repeatable"
                )

        if param.repeatable and param.kind not in ("option", "positional"):
            raise ValueError(
                f"Command {command_id} param {param.name}: only options and "
                "positionals may be repeatable"
            )

        if param.kind == "positional":
            if saw_free_tail:
                raise ValueError(
                    f"Command {command_id} param {param.name}: no positional "
                    "may follow a rest or repeatable positional param"
                )
            if param.required and saw_optional_positional:
                raise ValueError(
                    f"Command {command_id} param {param.name}: required "
                    "positional after an optional one"
                )
            if not param.required:
                saw_optional_positional = True
            if param.repeatable:
                saw_free_tail = True
        if param.kind == "rest":
            if index != len(params) - 1 and any(
                p.kind in ("positional", "rest") for p in params[index + 1 :]
            ):
                raise ValueError(
                    f"Command {command_id} rest param {param.name} must be "
                    "the last value-bearing param"
                )
            saw_free_tail = True


def generated_usage(path: tuple[str, ...], params: tuple[CommandParam, ...]) -> str:
    """Render the canonical usage string in the existing catalog idiom.

    Always starts with ``/`` + the joined path: the CLI inline hint
    prefix-matches typed text against this string and silently disappears on
    drift, so the leading form is an invariant, not a style choice.
    """
    parts = ["/" + " ".join(path)]
    for param in params:
        if param.kind == "flag":
            parts.append(f"[{param.option_spelling}]")
            continue
        if param.kind == "scope":
            parts.append("[" + "|".join(SCOPE_CHOICES) + "]")
            continue
        if param.kind == "option":
            value_label = "N" if param.type == "int" else param.name.upper().replace("-", "_")
            if param.choices:
                value_label = "|".join(param.choices)
            if param.label:
                value_label = param.label
            body = f"{param.option_spelling} {value_label}"
            if param.repeatable:
                body += " ..."
            parts.append(body if param.required else f"[{body}]")
            continue
        # positional / rest
        token = param.label or ("|".join(param.choices) if param.choices else param.name)
        if param.repeatable:
            token += " ..."
        parts.append(f"<{token}>" if param.required else f"[{token}]")
    return " ".join(parts)


def params_to_payload(
    params: tuple[CommandParam, ...] | None,
) -> list[dict[str, Any]] | None:
    """Serialize a params declaration for CommandInfo / GET /commands.

    ``None`` (unadopted) stays ``None`` on the wire so consumers can tell
    "no schema" from "schema'd, zero arguments" (``[]``).
    """
    if params is None:
        return None
    return [
        {
            "name": p.name,
            "kind": p.kind,
            "type": p.type,
            "required": p.required,
            "choices": list(p.choices),
            "choices_ref": p.choices_ref,
            "default": p.default,
            "repeatable": p.repeatable,
            "aliases": list(p.aliases),
            "description": p.description,
            "no_echo": p.no_echo,
            "label": p.label,
        }
        for p in params
    ]


def _echo(param: CommandParam | None, token: str) -> str:
    """Quote a rejected value for error copy, unless the param is no_echo."""
    if param is not None and param.no_echo:
        return "the provided value"
    return f"`{token}`"


def _coerce(param: CommandParam, token: str) -> tuple[Any, BindError | None]:
    if param.choices:
        folded = token.casefold()
        for choice in param.choices:
            if choice.casefold() == folded:
                return choice, None
        return None, BindError(
            f"{_echo(param, token)} is not a valid {param.display}. "
            f"Valid: {', '.join(param.choices)}.",
            param=param.name,
        )
    if param.type == "int":
        try:
            return int(token), None
        except ValueError:
            return None, BindError(
                f"{param.display} must be an integer, got {_echo(param, token)}.",
                param=param.name,
            )
    return token, None


def bind_args(
    params: tuple[CommandParam, ...],
    args: list[str],
    rest: str,
) -> tuple[BoundArgs | None, BindError | None]:
    """Parse and validate ``args`` against a params declaration.

    Returns ``(bound, None)`` on success or ``(None, error)`` on failure;
    exactly one side is set. ``rest`` is only inspected for the unbalanced
    -quote check and carried through for reference: the value grammar runs
    on the already-split tokens.
    """
    # The upstream splitter silently falls back to whitespace splitting when
    # shlex rejects the input (unbalanced quote). For a STRUCTURED argument
    # list that would validate a tokenization the user never meant, so fail
    # honestly. A declaration with a rest param is exempt: free text joins
    # the tokens back together, so the whitespace fallback IS the intended
    # value, and an apostrophe in a title ("Bob's plan") is ordinary English,
    # not a quoting mistake.
    if rest and not any(p.kind == "rest" for p in params):
        try:
            shlex.split(rest, posix=True)
        except ValueError:
            return None, BindError("Unbalanced quote in arguments.")

    by_spelling: dict[str, CommandParam] = {}
    for param in params:
        if param.kind in ("option", "flag"):
            by_spelling[param.option_spelling] = param
            for alias in param.aliases:
                by_spelling[alias] = param

    positionals = [p for p in params if p.kind == "positional"]
    rest_param = next((p for p in params if p.kind == "rest"), None)
    scope_param = next((p for p in params if p.kind == "scope"), None)

    tokens = list(args)
    values: dict[str, Any] = {}

    # Trailing scope token pops FIRST (the /think precedent), so an optional
    # leading positional can never swallow a scope word. Guarded: when the
    # token before it is a value-taking option, the last token is that
    # option's value, not a scope word.
    if scope_param is not None and tokens:
        folded = tokens[-1].casefold()
        prev_is_option_awaiting_value = False
        if len(tokens) >= 2:
            prev = by_spelling.get(tokens[-2])
            prev_is_option_awaiting_value = (
                prev is not None and prev.kind == "option"
            )
        if folded in SCOPE_CHOICES and not prev_is_option_awaiting_value:
            values[scope_param.name] = folded
            tokens.pop()

    positional_index = 0
    tail_tokens: list[str] = []
    tail_param = rest_param or next(
        (p for p in positionals if p.repeatable), None
    )
    in_tail = False

    index = 0
    while index < len(tokens):
        token = tokens[index]
        if in_tail:
            tail_tokens.append(token)
            index += 1
            continue
        if token.startswith("--") and len(token) > 2:
            head, eq, inline_value = token.partition("=")
            param = by_spelling.get(head)
            if param is None:
                return None, BindError(f"Unknown option `{head}`.")
            if param.kind == "flag":
                if eq:
                    return None, BindError(
                        f"{param.display} does not take a value.",
                        param=param.name,
                    )
                values[param.name] = True
                index += 1
                continue
            if eq:
                raw_value = inline_value
                index += 1
            else:
                if index + 1 >= len(tokens) or tokens[index + 1].startswith("--"):
                    return None, BindError(
                        f"{param.display} requires a value.", param=param.name
                    )
                raw_value = tokens[index + 1]
                index += 2
            coerced, error = _coerce(param, raw_value)
            if error is not None:
                return None, error
            if param.repeatable:
                values.setdefault(param.name, []).append(coerced)
            else:
                values[param.name] = coerced
            continue
        short_param = by_spelling.get(token)
        if short_param is not None and token.startswith("-"):
            if short_param.kind == "flag":
                values[short_param.name] = True
                index += 1
                continue
            if index + 1 >= len(tokens) or tokens[index + 1].startswith("--"):
                return None, BindError(
                    f"{short_param.display} requires a value.",
                    param=short_param.name,
                )
            coerced, error = _coerce(short_param, tokens[index + 1])
            if error is not None:
                return None, error
            if short_param.repeatable:
                values.setdefault(short_param.name, []).append(coerced)
            else:
                values[short_param.name] = coerced
            index += 2
            continue
        # Bare value token: next non-repeatable positional wins; a repeatable
        # positional is the tail and collects below.
        if positional_index < len(positionals) and not positionals[positional_index].repeatable:
            param = positionals[positional_index]
            coerced, error = _coerce(param, token)
            if error is not None:
                return None, error
            values[param.name] = coerced
            positional_index += 1
            index += 1
            continue
        if tail_param is not None:
            in_tail = True
            tail_tokens.append(token)
            index += 1
            continue
        return None, BindError(f"Unexpected argument {_echo(None, token)}.")

    if tail_param is not None and tail_tokens:
        if tail_param.kind == "rest":
            values[tail_param.name] = " ".join(tail_tokens)
        else:
            collected = []
            for token in tail_tokens:
                coerced, error = _coerce(tail_param, token)
                if error is not None:
                    return None, error
                collected.append(coerced)
            values[tail_param.name] = collected

    # Defaults, absence shapes, and required checks.
    for param in params:
        if param.name in values:
            continue
        if param.required:
            return None, BindError(
                f"Missing required argument: {param.display}.", param=param.name
            )
        if param.kind == "flag":
            values[param.name] = False
        elif param.repeatable:
            values[param.name] = []
        elif param.default is not None:
            values[param.name] = param.default

    return BoundArgs(values=values, tokens=tuple(args), rest=rest), None
