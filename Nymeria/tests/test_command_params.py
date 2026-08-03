"""Tests for the declared-argument substrate (backlog #129).

Covers the binder grammar, the usage generator, the declaration validator,
and the dispatcher's parse-and-bind wiring. Family-level adoption behavior
lives in the per-family suites.
"""

from __future__ import annotations

import pytest
from cli_fixtures import run

from nymeria.api.schemas.commands import CommandParamModel
from nymeria.core.command_params import (
    BoundArgs,
    CommandParam,
    bind_args,
    generated_usage,
    params_to_payload,
    validate_params,
)
from nymeria.core.command_service import (
    CommandContext,
    CommandService,
    _CommandExecutor,
)


def _bind(params: tuple[CommandParam, ...], text: str):
    args = text.split() if text else []
    return bind_args(params, args, text)


# ── Binder: options and flags ────────────────────────────────────────────────


def test_option_accepts_both_spellings() -> None:
    params = (CommandParam("limit", kind="option", type="int"),)
    for text in ("--limit 5", "--limit=5"):
        bound, error = _bind(params, text)
        assert error is None
        assert bound is not None and bound.get("limit") == 5


def test_option_last_wins_and_repeatable_collects() -> None:
    single = (CommandParam("tag", kind="option"),)
    bound, error = _bind(single, "--tag a --tag b")
    assert error is None and bound is not None
    assert bound.get("tag") == "b"

    repeat = (CommandParam("cond", kind="option", repeatable=True),)
    bound, error = _bind(repeat, "--cond a --cond=b")
    assert error is None and bound is not None
    assert bound.get("cond") == ["a", "b"]


def test_option_missing_value_and_unknown_option() -> None:
    params = (CommandParam("limit", kind="option", type="int"),)
    _, error = _bind(params, "--limit")
    assert error is not None and "--limit requires a value" in error.problem

    _, error = _bind(params, "--bogus 3")
    assert error is not None and "Unknown option `--bogus`" in error.problem


def test_option_immediately_followed_by_another_option_errors() -> None:
    # The lookahead must not consume a following option as the value; a
    # mutation dropping it survived the original suite (review finding).
    params = (
        CommandParam("tag", kind="option"),
        CommandParam("yes", kind="flag", type="bool", aliases=("-y",)),
    )
    _, error = _bind(params, "--tag --yes")
    assert error is not None and "--tag requires a value" in error.problem

    # Single-dash tokens are legal option VALUES (negative numbers), so the
    # lookahead blocks only double-dash tokens: "-y" binds as --tag's value
    # even though it is a declared alias, and "x" is then an extra.
    _, error = _bind(params, "--tag -y x")
    assert error is not None and "Unexpected argument `x`" in error.problem

    bound, error = _bind((CommandParam("tag", kind="option"),), "--tag -5")
    assert error is None and bound is not None
    assert bound.get("tag") == "-5"


def test_flag_short_alias_and_inline_value_rejection() -> None:
    params = (CommandParam("yes", kind="flag", type="bool", aliases=("-y",)),)
    for text in ("--yes", "-y"):
        bound, error = _bind(params, text)
        assert error is None
        assert bound is not None and bound.get("yes") is True

    _, error = _bind(params, "--yes=1")
    assert error is not None and "does not take a value" in error.problem


def test_int_coercion_error_names_the_option() -> None:
    params = (CommandParam("from_index", kind="option", type="int", aliases=("-f",)),)
    _, error = _bind(params, "--from-index x")
    assert error is not None
    assert "--from-index must be an integer" in error.problem
    assert "`x`" in error.problem


# ── Binder: positionals, choices, scope, rest ────────────────────────────────


def test_choices_casefold_to_canonical_and_reject() -> None:
    params = (CommandParam("state", choices=("on", "off", "toggle")),)
    bound, error = _bind(params, "ON")
    assert error is None
    assert bound is not None and bound.get("state") == "on"

    _, error = _bind(params, "sideways")
    assert error is not None
    assert "not a valid state" in error.problem
    assert "on, off, toggle" in error.problem


def test_required_positional_missing() -> None:
    params = (CommandParam("id", required=True),)
    _, error = _bind(params, "")
    assert error is not None and "Missing required argument: id" in error.problem


def test_zero_param_declaration_is_strict() -> None:
    bound, error = _bind((), "")
    assert error is None and bound is not None

    _, error = _bind((), "bogus")
    assert error is not None and "Unexpected argument `bogus`" in error.problem


def test_scope_pops_trailing_token_before_positionals() -> None:
    params = (CommandParam("name"), CommandParam("scope", kind="scope"))
    bound, error = _bind(params, "fable thread")
    assert error is None and bound is not None
    assert bound.get("name") == "fable"
    assert bound.get("scope") == "thread"

    # A lone scope word is scope, never the optional positional.
    bound, error = _bind(params, "global")
    assert error is None and bound is not None
    assert bound.get("name") is None
    assert bound.get("scope") == "global"


def test_scope_never_steals_an_option_value() -> None:
    params = (
        CommandParam("tag", kind="option"),
        CommandParam("scope", kind="scope"),
    )
    bound, error = _bind(params, "--tag thread")
    assert error is None and bound is not None
    assert bound.get("tag") == "thread"
    assert bound.get("scope") is None


def test_rest_joins_and_swallows_option_lookalikes() -> None:
    params = (
        CommandParam("from_index", kind="option", type="int", aliases=("--from",)),
        CommandParam("title", kind="rest"),
    )
    bound, error = _bind(params, "--from 3 my --cool title")
    assert error is None and bound is not None
    assert bound.get("from_index") == 3
    assert bound.get("title") == "my --cool title"


def test_repeatable_positional_collects_all() -> None:
    params = (CommandParam("pairs", repeatable=True),)
    bound, error = _bind(params, "a=1 b=2")
    assert error is None and bound is not None
    assert bound.get("pairs") == ["a=1", "b=2"]


def test_repeatable_positional_keeps_parsing_options_around_it() -> None:
    """A repeatable positional is not a raw tail, unlike ``rest``.

    ``/hook create <name ...> --event E`` interleaves the two: its hand parser
    pulled option pairs out of the list from any position and joined what was
    left, so bare words on both sides of an option belong to the positional
    and a typo'd option is still an error.
    """
    params = (
        CommandParam("name", repeatable=True),
        CommandParam("event", kind="option"),
        CommandParam("once", kind="flag", type="bool"),
    )
    bound, error = _bind(params, "Rate limit --event done guard --once")
    assert error is None and bound is not None
    assert bound.get("name") == ["Rate", "limit", "guard"]
    assert bound.get("event") == "done"
    assert bound.get("once") is True

    _, error = _bind(params, "Rate limit --evnt done")
    assert error is not None and "Unknown option `--evnt`" in error.problem


def test_unbalanced_quote_is_an_honest_error() -> None:
    params = (CommandParam("key"),)
    _, error = bind_args(params, ["k", '"unclosed'], 'k "unclosed')
    assert error is not None and "Unbalanced quote" in error.problem


def test_repeatable_positional_tail_tolerates_apostrophes() -> None:
    # Same argument as the rest exemption: the tail collects bare words, so
    # the whitespace fallback is the intended tokenization ("don't" in a
    # skills search must not error).
    params = (CommandParam("query", repeatable=True),)
    bound, error = bind_args(params, ["don't", "panic"], "don't panic")
    assert error is None and bound is not None
    assert bound.get("query") == ["don't", "panic"]


def test_unbalanced_double_quote_errors_even_with_a_free_tail() -> None:
    # An unbalanced DOUBLE quote is attempted phrase-quoting, not English:
    # the fallback would mis-assign tokens across options and the tail
    # (measured on /hook create, where it quietly renamed the hook), so the
    # honest error survives the apostrophe exemption.
    params = (
        CommandParam("name", repeatable=True),
        CommandParam("cond", kind="option", repeatable=True),
    )
    _, error = bind_args(
        params,
        ["Guard", "--cond", '"command', "contains", "rm"],
        'Guard --cond "command contains rm',
    )
    assert error is not None and "Unbalanced quote" in error.problem


def test_rest_param_tolerates_apostrophes() -> None:
    # Free text re-joins the tokens, so the whitespace fallback IS the
    # intended value; "Bob's plan" is English, not a quoting mistake.
    params = (
        CommandParam("from_index", kind="option", type="int"),
        CommandParam("title", kind="rest"),
    )
    bound, error = bind_args(params, ["Bob's", "plan"], "Bob's plan")
    assert error is None and bound is not None
    assert bound.get("title") == "Bob's plan"


def test_error_copy_prefers_the_display_label() -> None:
    params = (
        CommandParam("id_or_title", kind="rest", required=True, label="id-or-title"),
    )
    _, error = bind_args(params, [], "")
    assert error is not None
    assert "Missing required argument: id-or-title" in error.problem


def test_absence_shapes_and_defaults() -> None:
    params = (
        CommandParam("name"),
        CommandParam("mode", choices=("a", "b"), default="a"),
        CommandParam("yes", kind="flag", type="bool"),
        CommandParam("cond", kind="option", repeatable=True),
    )
    bound, error = _bind(params, "")
    assert error is None and bound is not None
    assert bound.get("name") is None
    assert bound.get("mode") == "a"
    assert bound.get("yes") is False
    assert bound.get("cond") == []


def test_no_echo_hides_the_rejected_value() -> None:
    params = (CommandParam("value", choices=("safe",), no_echo=True),)
    _, error = _bind(params, "sk-secret")
    assert error is not None
    assert "sk-secret" not in error.problem
    assert "the provided value" in error.problem


# ── Usage generation ─────────────────────────────────────────────────────────


def test_generated_usage_matches_catalog_idiom() -> None:
    assert generated_usage(
        ("thread", "delete"),
        (
            CommandParam("id", required=True),
            CommandParam("yes", kind="flag", type="bool", aliases=("-y",)),
        ),
    ) == "/thread delete <id> [--yes]"

    assert generated_usage(
        ("think",),
        (CommandParam("mode", choices=("off", "on", "low")),),
    ) == "/think [off|on|low]"

    # The label override advertises a canonical set the binder does NOT
    # enforce (the real /thread pin also accepts yes/no/true/false).
    assert generated_usage(
        ("thread", "pin"),
        (
            CommandParam("id"),
            CommandParam("state", label="on|off|toggle"),
        ),
    ) == "/thread pin [id] [on|off|toggle]"

    assert generated_usage(
        ("thread", "switch"),
        (
            CommandParam(
                "id_or_title", kind="rest", required=True, label="id-or-title"
            ),
        ),
    ) == "/thread switch <id-or-title>"

    assert generated_usage(
        ("thread", "branch"),
        (
            CommandParam("from_index", kind="option", type="int", aliases=("--from",)),
            CommandParam("title", kind="rest"),
        ),
    ) == "/thread branch [--from-index N] [title]"

    # Flags declare BEFORE the scope: the trailing-scope pop inspects the
    # last token only, so the advertised order must keep the scope word last.
    assert generated_usage(
        ("model",),
        (
            CommandParam("name"),
            CommandParam("force", kind="flag", type="bool"),
            CommandParam("scope", kind="scope"),
        ),
    ) == "/model [name] [--force] [global|thread]"

    # Registry paths store hyphenated tokens as underscores; usage renders
    # them back the way users type them.
    assert generated_usage(
        ("provider", "reasoning_passback"), ()
    ) == "/provider reasoning-passback"

    assert generated_usage(
        ("hook", "log"),
        (CommandParam("cond", kind="option", repeatable=True),),
    ) == "/hook log [--cond COND ...]"


def test_generated_usage_always_starts_with_the_path() -> None:
    # The CLI inline hint prefix-matches typed text against usage; the
    # leading "/path" form is an invariant, not a style choice.
    usage = generated_usage(("mcp", "logs"), (CommandParam("limit", type="int"),))
    assert usage.startswith("/mcp logs")


# ── Declaration validation ───────────────────────────────────────────────────


@pytest.mark.parametrize(
    ("params", "fragment"),
    [
        ((CommandParam("a"), CommandParam("a")), "duplicates param"),
        (
            (CommandParam("tail", kind="rest"), CommandParam("late")),
            "must be the last",
        ),
        (
            (CommandParam("tail", kind="rest"), CommandParam("scope", kind="scope")),
            "scope and rest",
        ),
        (
            (CommandParam("opt"), CommandParam("req", required=True)),
            "required positional after an optional",
        ),
        ((CommandParam("yes", kind="flag"),), "must be type bool"),
        ((CommandParam("state", type="bool"),), "type bool is for flags"),
        (
            (CommandParam("mode", choices=("a",), default="z"),),
            "not among its choices",
        ),
        ((CommandParam("name", aliases=("-n",)),), "only options and flags"),
        (
            (
                CommandParam("alpha", kind="option"),
                CommandParam("beta", kind="option", aliases=("--alpha",)),
            ),
            "reuses option spelling",
        ),
        ((CommandParam("Bad-Name"),), "snake_case"),
        (
            (CommandParam("yes", kind="flag", type="bool", repeatable=True),),
            "cannot be required, repeatable",
        ),
        (
            (CommandParam("s1", kind="scope"), CommandParam("s2", kind="scope")),
            "more than one scope",
        ),
        (
            (CommandParam("many", repeatable=True), CommandParam("late")),
            "no positional may follow",
        ),
        (
            (CommandParam("cond", kind="option", repeatable=True, default="x"),),
            "never applied",
        ),
        (
            (CommandParam("id", required=True, default="x"),),
            "never applied",
        ),
    ],
)
def test_validate_params_rejects(params: tuple[CommandParam, ...], fragment: str) -> None:
    with pytest.raises(ValueError, match=fragment):
        validate_params("test.cmd", params)


def test_register_rejects_hand_written_usage_with_params() -> None:
    service = CommandService()
    with pytest.raises(ValueError, match="usage is generated"):
        service.register(
            "zzparams",
            description="test",
            category="Test",
            usage="/zzparams [x]",
            params=(CommandParam("x"),),
        )


def test_register_generates_usage_and_validates_at_declaration() -> None:
    service = CommandService()
    service.register(
        "zzparams",
        description="test",
        category="Test",
        params=(CommandParam("id", required=True),),
    )
    info = service.find_command("zzparams")
    assert info is not None
    assert info.usage == "/zzparams <id>"
    assert info.params == [
        {
            "name": "id",
            "kind": "positional",
            "type": "str",
            "required": True,
            "choices": [],
            "choices_ref": None,
            "default": None,
            "repeatable": False,
            "aliases": [],
            "description": "",
            "no_echo": False,
            "label": None,
        }
    ]

    with pytest.raises(ValueError, match="duplicates param"):
        service.register(
            "zzbad",
            description="test",
            category="Test",
            params=(CommandParam("a"), CommandParam("a")),
        )


def test_params_payload_distinguishes_unadopted_from_zero_args() -> None:
    assert params_to_payload(None) is None
    assert params_to_payload(()) == []
    payload = params_to_payload((CommandParam("x"),))
    assert payload is not None
    # The wire payload round-trips through the API schema model unchanged.
    model = CommandParamModel(**payload[0])
    assert model.name == "x" and model.kind == "positional"


def test_param_model_and_dataclass_declare_the_same_fields() -> None:
    """Field-parity gate: the wire schema cannot silently lag the dataclass.

    params_to_payload serializes via dataclasses.asdict, so a new
    CommandParam field reaches the wire automatically; pydantic ignores
    unknown fields, so without this gate the model would silently drop it.
    """
    import dataclasses

    dataclass_fields = {f.name for f in dataclasses.fields(CommandParam)}
    model_fields = set(CommandParamModel.model_fields)
    assert model_fields == dataclass_fields


# ── Adoption ratchet ─────────────────────────────────────────────────────────

# The full-catalog adoption invariant (#129): every executable command
# declares params unless it is on this list, and every entry here must stay
# genuinely exempt. Growing this list is a design decision, not a shortcut;
# each entry names its reason.
_PARAMS_EXEMPT: frozenset[str] = frozenset(
    {
        # Act-now commands: trailing words must never block the action.
        "stop",
        "clear",
        "restart.api",
        # The dispatcher special-cases /help before binding runs.
        "help",
        # Raw-rest grammars: prefix parsing or quote fidelity on the raw tail.
        "notepad.write",
        "todos.add",
        # Free-text tail with int-ambiguity (minutes-or-note).
        "fallback.approve",
        "fallback.deny",
        # Server-state step rails (backlog 08-cli.md exemption).
        "provider.setup",
        "provider.cliproxy",
    }
)


def test_every_executable_command_declares_params_unless_exempt() -> None:
    service = CommandService()
    missing = sorted(
        cmd.id
        for cmd in service._commands.values()
        if cmd.executable and cmd.params is None and cmd.id not in _PARAMS_EXEMPT
    )
    assert missing == [], f"Executable commands without declared params: {missing}"

    stale = sorted(
        command_id
        for command_id in _PARAMS_EXEMPT
        if command_id not in service._commands
        or service._commands[command_id].params is not None
    )
    assert stale == [], f"Exemption entries no longer exempt: {stale}"

    adopted = sum(
        1
        for cmd in service._commands.values()
        if cmd.executable and cmd.params is not None
    )
    assert adopted >= 115  # anti-vacuity floor, mirrors the binding guard's
    # The exemption list may only shrink without a recorded design decision.
    assert len(_PARAMS_EXEMPT) <= 10


def test_slash_command_tool_description_derives_the_blocked_list() -> None:
    """The agent tool's description is generated from AGENT_BLOCKED.

    The old hand-written literal drifted (it omitted /start). The description
    is the only syntax reference the model sees before calling /help, so
    every blocked root must appear in it.
    """
    from nymeria.core.command_service import AGENT_BLOCKED
    from nymeria.tools.slash_command import slash_command

    description = slash_command.description or ""
    for name in AGENT_BLOCKED:
        assert f"/{name}" in description
    assert "argument schema" in description


# ── Dispatcher wiring (parse-and-bind) ───────────────────────────────────────


def _ctx() -> CommandContext:
    return CommandContext(user_id="alice", thread_id="t1", surface="cli", is_admin=True)


def _service_with_synthetic(
    monkeypatch: pytest.MonkeyPatch,
    params: tuple[CommandParam, ...] | None,
) -> CommandService:
    service = CommandService()
    service.register(
        "zzsynth",
        description="synthetic test command",
        category="Test",
        params=params,
    )

    async def _cmd_zzsynth(self, *invoke_args):  # noqa: ANN001, ANN202
        if params is not None:
            (bound,) = invoke_args
            assert isinstance(bound, BoundArgs)
            return f"[Success]: limit={bound.get('limit')}"
        args, rest = invoke_args
        return f"[Success]: legacy args={args} rest={rest}"

    monkeypatch.setattr(_CommandExecutor, "_cmd_zzsynth", _cmd_zzsynth, raising=False)
    return service


def test_dispatcher_binds_for_adopted_commands(monkeypatch: pytest.MonkeyPatch) -> None:
    service = _service_with_synthetic(
        monkeypatch, (CommandParam("limit", kind="option", type="int"),)
    )
    result = run(service.execute(_ctx(), "/zzsynth --limit=4", api=object()))
    assert result.success is True
    assert "limit=4" in result.markdown


def test_dispatcher_renders_bind_errors_in_usage_idiom(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    service = _service_with_synthetic(
        monkeypatch, (CommandParam("limit", kind="option", type="int"),)
    )
    result = run(service.execute(_ctx(), "/zzsynth --limit x", api=object()))
    assert result.success is False
    assert result.markdown.startswith("**Error:** --limit must be an integer")
    assert "Usage: `/zzsynth [--limit N]`" in result.markdown
    assert "See `/help zzsynth`" in result.markdown


def test_dispatcher_strict_zero_param_extras(monkeypatch: pytest.MonkeyPatch) -> None:
    service = _service_with_synthetic(monkeypatch, ())
    result = run(service.execute(_ctx(), "/zzsynth bogus", api=object()))
    assert result.success is False
    assert "Unexpected argument `bogus`" in result.markdown


def test_dispatcher_legacy_path_is_untouched(monkeypatch: pytest.MonkeyPatch) -> None:
    service = _service_with_synthetic(monkeypatch, None)
    result = run(service.execute(_ctx(), "/zzsynth a b", api=object()))
    assert result.success is True
    assert "legacy args=['a', 'b'] rest=a b" in result.markdown


def test_bind_error_on_a_family_root_suggests_subcommands(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    service = _service_with_synthetic(monkeypatch, ())
    service.register(
        "zzsynth list",
        description="synthetic subcommand",
        category="Test",
        params=(),
    )
    result = run(service.execute(_ctx(), "/zzsynth lst", api=object()))
    assert result.success is False
    assert "Unexpected argument `lst`" in result.markdown
    assert "Did you mean `/zzsynth list`?" in result.markdown
    assert "Valid subcommands: list." in result.markdown


def test_help_card_renders_params_table(monkeypatch: pytest.MonkeyPatch) -> None:
    service = CommandService()
    service.register(
        "zzsynth",
        description="synthetic test command",
        category="Test",
        params=(
            CommandParam(
                "limit",
                kind="option",
                type="int",
                description="Max rows",
                aliases=("-l",),
            ),
        ),
    )
    card = service._command_help_markdown(("zzsynth",))
    assert card is not None
    assert "| Argument | Required | Description |" in card
    assert "| --limit (-l) | optional | Max rows |" in card
