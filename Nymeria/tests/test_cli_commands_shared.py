"""Tests for the shared CLI command toolkit (`commands/_shared.py`).

These cover the byte-identical `mapping_sequence`/`string_list` helpers (F3)
and the larger command toolkit relocated here from `system.py` (F2): the
transport shim, confirmation helpers, formatting helpers, and the scalar parser
(also reused by `tools.py` per F10). Each behavior test pins a relocated
helper's semantics; the wiring tests assert every importer resolves the shared
copy (so a future re-introduction of a local copy is caught).
"""

from __future__ import annotations

import asyncio
from types import ModuleType
from typing import Any

import pytest

from nymeria.triggers.cli.commands import (
    _shared,
    account,
    artifacts,
    doctor,
    mcp,
    skills,
    system,
    todos,
    tools,
    triggers,
)
from nymeria.triggers.cli.commands.base import CommandContext


class TestMappingSequence:
    def test_keeps_only_mappings_in_order(self) -> None:
        value = [{"a": 1}, "skip", 7, {"b": 2}]
        assert _shared.mapping_sequence(value) == [{"a": 1}, {"b": 2}]

    def test_accepts_tuples(self) -> None:
        assert _shared.mapping_sequence(({"a": 1},)) == [{"a": 1}]

    def test_rejects_str_and_bytes(self) -> None:
        assert _shared.mapping_sequence("abc") == []
        assert _shared.mapping_sequence(b"abc") == []

    def test_rejects_non_sequence(self) -> None:
        # A bare mapping is not a Sequence, so it yields nothing.
        assert _shared.mapping_sequence({"a": 1}) == []
        assert _shared.mapping_sequence(None) == []
        assert _shared.mapping_sequence(42) == []

    def test_empty_sequence(self) -> None:
        assert _shared.mapping_sequence([]) == []


class TestStringList:
    def test_stringifies_items(self) -> None:
        assert _shared.string_list(["x", "y"]) == ["x", "y"]
        assert _shared.string_list((1, 2)) == ["1", "2"]

    def test_filters_empty_string_renderings(self) -> None:
        # Only items whose str() is falsy (the empty string) are dropped.
        assert _shared.string_list(["x", "", "y"]) == ["x", "y"]

    def test_keeps_items_that_are_falsy_but_render_nonempty(self) -> None:
        # The filter is `if str(item)`, so 0 and False survive (as "0"/"False");
        # only an empty-string rendering is dropped.
        assert _shared.string_list([0, False, ""]) == ["0", "False"]

    def test_rejects_str_and_bytes(self) -> None:
        assert _shared.string_list("xy") == []
        assert _shared.string_list(b"xy") == []

    def test_rejects_non_sequence(self) -> None:
        assert _shared.string_list(None) == []
        assert _shared.string_list(123) == []


@pytest.mark.parametrize("module", [account, skills, mcp, triggers, todos, tools])
def test_modules_share_mapping_sequence(module: ModuleType) -> None:
    assert module._mapping_sequence is _shared.mapping_sequence


@pytest.mark.parametrize("module", [skills, mcp, tools])
def test_modules_share_string_list(module: ModuleType) -> None:
    assert module._string_list is _shared.string_list


class TestParseScalar:
    def test_boolean_words(self) -> None:
        for truthy in ("true", "yes", "on", "TRUE", "On"):
            assert _shared.parse_scalar(truthy) is True
        for falsy in ("false", "no", "off", "FALSE", "Off"):
            assert _shared.parse_scalar(falsy) is False

    def test_none_words(self) -> None:
        assert _shared.parse_scalar("none") is None
        assert _shared.parse_scalar("NULL") is None

    def test_int_then_float_then_string(self) -> None:
        parsed = _shared.parse_scalar("42")
        assert parsed == 42
        assert isinstance(parsed, int)
        assert _shared.parse_scalar("3.14") == pytest.approx(3.14)
        assert _shared.parse_scalar("1e3") == pytest.approx(1000.0)
        assert _shared.parse_scalar("hello") == "hello"

    def test_coerces_non_str_and_strips(self) -> None:
        # The shared parser wraps str(value); the old tools.py copy assumed str.
        assert _shared.parse_scalar(7) == 7  # type: ignore[bad-argument-type]
        assert _shared.parse_scalar("  on  ") is True


class TestCompactId:
    def test_truncates_to_width(self) -> None:
        assert _shared.compact_id("abcdefghij") == "abcdefgh"
        assert _shared.compact_id("abcdefghij", width=4) == "abcd"

    def test_short_value_unchanged_and_none(self) -> None:
        assert _shared.compact_id("abc") == "abc"
        assert _shared.compact_id(None) == ""


class TestOneLine:
    def test_collapses_whitespace(self) -> None:
        assert _shared.one_line("a\n  b\t c") == "a b c"
        assert _shared.one_line(None) == ""

    def test_truncates_with_ellipsis(self) -> None:
        out = _shared.one_line("x" * 50, limit=10)
        assert out.endswith("...")
        assert len(out) <= 10


class TestFormatBool:
    def test_bool_to_yes_no(self) -> None:
        assert _shared.format_bool(True) == "yes"
        assert _shared.format_bool(False) == "no"

    def test_non_bool_passthrough(self) -> None:
        assert _shared.format_bool("auto") == "auto"
        assert _shared.format_bool(3) == "3"


class TestThreadHelpers:
    def test_normalize_thread_id_prefers_thread_id(self) -> None:
        assert _shared.normalize_thread_id({"thread_id": "t1", "id": "x"}) == "t1"
        assert _shared.normalize_thread_id({"id": "x"}) == "x"
        assert _shared.normalize_thread_id({}) == ""

    def test_thread_title_default(self) -> None:
        assert _shared.thread_title({"title": "  Hi "}) == "Hi"
        assert _shared.thread_title({"title": ""}) == "New Chat"
        assert _shared.thread_title({}) == "New Chat"


class TestStripConfirmationFlags:
    def test_removes_flags_and_reports_confirmed(self) -> None:
        remaining, confirmed = _shared.strip_confirmation_flags(["a", "--yes", "b"])
        assert remaining == ["a", "b"]
        assert confirmed is True

    def test_no_flags(self) -> None:
        remaining, confirmed = _shared.strip_confirmation_flags(["a", "b"])
        assert remaining == ["a", "b"]
        assert confirmed is False

    def test_short_flag(self) -> None:
        remaining, confirmed = _shared.strip_confirmation_flags(["-y"])
        assert remaining == []
        assert confirmed is True


class TestConfirmationGranted:
    def test_explicit_confirmation_skips_prompt(self) -> None:
        calls: list[str] = []

        def _confirm(prompt: str) -> bool:
            calls.append(prompt)
            return True

        ctx = CommandContext(confirm_handler=_confirm)
        granted = asyncio.run(
            _shared.confirmation_granted(ctx, "Proceed?", explicitly_confirmed=True)
        )
        assert granted is True
        assert calls == []  # the handler is not consulted when already confirmed

    def test_delegates_to_context_confirm(self) -> None:
        def _yes(_prompt: str) -> bool:
            return True

        ctx_yes = CommandContext(confirm_handler=_yes)
        assert asyncio.run(_shared.confirmation_granted(ctx_yes, "Proceed?")) is True

        # No handler falls back to the safe default (False).
        ctx_none = CommandContext(confirm_handler=None)
        assert asyncio.run(_shared.confirmation_granted(ctx_none, "Proceed?")) is False


class TestMappingGet:
    def test_reads_mapping(self) -> None:
        assert _shared.mapping_get({"k": 1}, "k") == 1
        assert _shared.mapping_get({}, "k", "d") == "d"

    def test_reads_object_attr(self) -> None:
        class _Obj:
            k = 5

        assert _shared.mapping_get(_Obj(), "k") == 5
        assert _shared.mapping_get(_Obj(), "missing", "d") == "d"


class TestTransportResults:
    def test_unsupported_transport_result(self) -> None:
        result = _shared.unsupported_transport_result("/x", method_name="foo")
        assert result.status == "error"
        assert result.error_code == "unsupported_transport"
        assert result.payload == {"method": "foo"}
        assert "foo" in result.messages[0].content

    def test_confirmation_required_result(self) -> None:
        result = _shared.confirmation_required_result("/x")
        assert result.ok
        assert result.messages[0].level == "warning"
        assert "--yes" in result.messages[0].content


class _FakeApi:
    def echo(self, value: object) -> object:
        return value


class _FakeClient:
    def ping(self, value: object) -> object:
        return value

    async def aping(self, value: object) -> object:
        return value


class _WrappedClient:
    def __init__(self) -> None:
        self.api = _FakeApi()


class TestTransportShim:
    def test_has_client_method_direct_and_missing(self) -> None:
        ctx = CommandContext(client=_FakeClient())
        assert _shared.has_client_method(ctx, "ping") is True
        assert _shared.has_client_method(ctx, "nope") is False

    def test_has_client_method_via_api_fallback(self) -> None:
        ctx = CommandContext(client=_WrappedClient())
        assert _shared.has_client_method(ctx, "echo") is True

    def test_has_client_method_no_client(self) -> None:
        assert _shared.has_client_method(CommandContext(client=None), "ping") is False

    def test_call_client_method_sync(self) -> None:
        ctx = CommandContext(client=_FakeClient())
        assert asyncio.run(_shared.call_client_method(ctx, "ping", 9)) == 9

    def test_call_client_method_async(self) -> None:
        ctx = CommandContext(client=_FakeClient())
        assert asyncio.run(_shared.call_client_method(ctx, "aping", 11)) == 11

    def test_call_client_method_missing_raises(self) -> None:
        ctx = CommandContext(client=_FakeClient())
        with pytest.raises(_shared.CommandClientMethodUnavailable) as exc:
            asyncio.run(_shared.call_client_method(ctx, "nope"))
        assert exc.value.method_name == "nope"


class _UserScopedClient:
    """Accepts user_id as a keyword; records the call args/kwargs."""

    def __init__(self) -> None:
        self.calls: list[tuple[tuple[Any, ...], dict[str, Any]]] = []

    def echo(self, *args: Any, **kwargs: Any) -> dict[str, Any]:
        self.calls.append((args, kwargs))
        return {"args": args, "kwargs": kwargs}


class _PositionalOnlyClient:
    """Rejects user_id as a keyword (forces the positional retry)."""

    def fetch(self, value: Any, uid: Any) -> dict[str, Any]:
        return {"value": value, "uid": uid}


class _AsyncUserScopedClient:
    """Mirrors production: the real converted client methods are async."""

    async def fetch(self, *args: Any, **kwargs: Any) -> dict[str, Any]:
        return {"args": args, "kwargs": kwargs}


class _KeywordRejectingThenUnavailableClient:
    """Raises TypeError on the keyword call and CMU on the positional retry.

    Pins the helper's contract: it catches ONLY TypeError, so a
    CommandClientMethodUnavailable from the positional retry must propagate.
    """

    def act(self, *args: Any, **kwargs: Any) -> Any:
        if "user_id" in kwargs:
            raise TypeError("user_id keyword not accepted")
        raise _shared.CommandClientMethodUnavailable("act")


class TestCallClientUserScoped:
    def test_keyword_form_succeeds_and_passes_user_id(self) -> None:
        client = _UserScopedClient()
        ctx = CommandContext(client=client, user_id="u1")
        result = asyncio.run(_shared.call_client_user_scoped(ctx, "echo", "a", "b"))
        assert result == {"args": ("a", "b"), "kwargs": {"user_id": "u1"}}
        # Only the keyword form ran; no positional retry.
        assert client.calls == [(("a", "b"), {"user_id": "u1"})]

    def test_positional_retry_on_type_error(self) -> None:
        ctx = CommandContext(client=_PositionalOnlyClient(), user_id="u2")
        result = asyncio.run(_shared.call_client_user_scoped(ctx, "fetch", "x"))
        # Keyword form raised TypeError; retried with user_id appended positionally.
        assert result == {"value": "x", "uid": "u2"}

    def test_awaits_async_method(self) -> None:
        # The real converted client methods are async; confirm the helper awaits.
        ctx = CommandContext(client=_AsyncUserScopedClient(), user_id="u3")
        result = asyncio.run(_shared.call_client_user_scoped(ctx, "fetch", "z"))
        assert result == {"args": ("z",), "kwargs": {"user_id": "u3"}}

    def test_unavailable_method_propagates(self) -> None:
        # Method missing entirely: the keyword call raises CMU; the helper must
        # not swallow it and must not attempt a positional retry.
        ctx = CommandContext(client=_FakeClient())
        with pytest.raises(_shared.CommandClientMethodUnavailable) as exc:
            asyncio.run(_shared.call_client_user_scoped(ctx, "nope"))
        assert exc.value.method_name == "nope"

    def test_cmu_from_positional_retry_propagates(self) -> None:
        # Contract: the helper catches ONLY TypeError. A CMU raised by the
        # positional retry must propagate (not be swallowed or retried).
        ctx = CommandContext(client=_KeywordRejectingThenUnavailableClient())
        with pytest.raises(_shared.CommandClientMethodUnavailable):
            asyncio.run(_shared.call_client_user_scoped(ctx, "act"))


# F2/F10 wiring: importers resolve the shared toolkit copy.

_TOOLKIT_WIRING = [
    (account, "call_client_method"),
    (account, "mapping_get"),
    (account, "compact_id"),
    (skills, "one_line"),
    (mcp, "confirmation_granted"),
    (todos, "unsupported_transport_result"),
    (triggers, "parse_scalar"),
    (tools, "parse_scalar"),
    # system.py stopped importing parse_scalar when /settings moved to the
    # backend registry (config-group migration).
    (system, "call_client_method"),
    # F9: the 6 importers of the call-shim helper resolve the shared copy.
    (tools, "call_client_user_scoped"),
    (skills, "call_client_user_scoped"),
    (doctor, "call_client_user_scoped"),
    (account, "call_client_user_scoped"),
    (artifacts, "call_client_user_scoped"),
    (triggers, "call_client_user_scoped"),
]


@pytest.mark.parametrize("module, attr", _TOOLKIT_WIRING)
def test_modules_share_toolkit_symbol(module: ModuleType, attr: str) -> None:
    assert getattr(module, attr) is getattr(_shared, attr)


def test_tools_local_scalar_parser_removed() -> None:
    # F10: tools.py must reuse the shared parse_scalar, not keep a private copy.
    assert not hasattr(tools, "_parse_scalar")
    assert tools.parse_scalar is _shared.parse_scalar


class TestToolsParseParams:
    def test_key_value_branch_uses_shared_parser(self) -> None:
        params, error = tools._parse_params(["a=1", "b=true", "c=hi"])
        assert error == ""
        assert params == {"a": 1, "b": True, "c": "hi"}

    def test_json_object_branch_preserved(self) -> None:
        params, error = tools._parse_params(['{"x": 1, "y": "z"}'])
        assert error == ""
        assert params == {"x": 1, "y": "z"}

    def test_invalid_pair_reports_error(self) -> None:
        params, error = tools._parse_params(["bad"])
        assert params == {}
        assert "key=value" in error
