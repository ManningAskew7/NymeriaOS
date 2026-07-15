"""Regression tests for output-token ceiling resolution.

Background. Leaving ``max_tokens`` unset is normally the correct dynamic
behaviour: the parameter is omitted, the provider applies its own current
server-side default, and no local table can beat that. langchain-anthropic is
the sole exception. Its ``set_default_max_tokens`` validator substitutes
``_FALLBACK_MAX_OUTPUT_TOKENS = 4096`` for any model its bundled profile table
does not recognise, so the request never reaches Anthropic blank.

The Anthropic factory did not resolve a ceiling, which meant every Claude turn
on a model newer than the pinned library ran with a 4096 total output budget.
With extended thinking at high effort that is fatal rather than merely short:
thinking bills against ``max_tokens`` and is emitted BEFORE any text or
tool_use, so exhausting the cap yields a lone thinking block, no text, no tool
calls, which the graph reads as a clean finish and the bots render as silence.

A note on testing style here, learned the hard way. Several tests in this file
verify that something is NOT called. They do so by counting calls, never by
raising from the fake: ``resolve_max_output_tokens`` deliberately swallows
``Exception`` so discovery can never break a turn, and ``AssertionError`` is an
``Exception``. A raising fake is silently eaten and the test passes no matter
what the code does.
"""

from __future__ import annotations

import asyncio

import pytest

from _provider_test_helpers import llm_config  # type: ignore[import-not-found]

from nymeria.vendor.react_agent import providers
from nymeria.vendor.react_agent.providers import (
    MIN_THINKING_BUDGET_TOKENS,
    NONSTREAMING_MAX_OUTPUT_TOKENS,
    instance_max_output_tokens,
    langchain_fallback_max_tokens,
    resolve_max_output_tokens,
    streaming_call_kwargs,
)


def _anthropic_config(**overrides):
    return llm_config(
        {
            "provider": "anthropic",
            "model": "claude-sonnet-5",
            "base_url": "http://cli-proxy.test:8317",
        },
        **overrides,
    )


# ---------------------------------------------------------------------------
# The vendor-drift guards
# ---------------------------------------------------------------------------


def test_nonstreaming_ceiling_matches_installed_sdk():
    """Our clamp must track the SDK's real non-streaming ceiling.

    ``NONSTREAMING_MAX_OUTPUT_TOKENS`` is derived from vendor internals
    (``anthropic/_base_client.py::_calculate_nonstreaming_timeout``:
    ``3600 * max_tokens / 128_000 > 600``). If Anthropic ever changes that
    formula a hardcoded constant would drift silently, so ask the installed SDK
    what its ceiling actually is rather than trusting the number.

    Deliberately asymmetric. A ceiling that ROSE only makes our clamp
    conservative, which is safe, so it must not fail a routine dependency bump.
    A ceiling that FELL is the real bug: it means we are now sending requests
    the SDK will reject.
    """
    anthropic = pytest.importorskip("anthropic")
    client = anthropic.Anthropic(api_key="not-used")

    # Assert the seam exists before probing it. Without this a rename makes
    # every probe fail, the search converges on 0, and the failure message
    # claims the ceiling dropped to zero: the opposite of the truth.
    assert hasattr(client, "_calculate_nonstreaming_timeout"), (
        "The Anthropic SDK no longer exposes _calculate_nonstreaming_timeout. "
        "The guard this clamp is built on has moved; re-derive "
        "NONSTREAMING_MAX_OUTPUT_TOKENS from the new shape."
    )

    def accepted(max_tokens: int) -> bool:
        try:
            client._calculate_nonstreaming_timeout(max_tokens, None)
            return True
        except ValueError:
            return False

    lo, hi = 0, 1_000_000
    while lo < hi:
        mid = (lo + hi + 1) // 2
        if accepted(mid):
            lo = mid
        else:
            hi = mid - 1

    assert lo >= NONSTREAMING_MAX_OUTPUT_TOKENS, (
        f"The Anthropic SDK now rejects non-streaming requests above {lo} "
        f"max_tokens, but NONSTREAMING_MAX_OUTPUT_TOKENS is "
        f"{NONSTREAMING_MAX_OUTPUT_TOKENS}, so we would send requests it "
        f"refuses. Lower the constant."
    )
    assert accepted(NONSTREAMING_MAX_OUTPUT_TOKENS)


def test_the_nonstreaming_guard_is_actually_reachable_on_our_async_client(monkeypatch):
    """Pins REACHABILITY, not the formula. This is the load-bearing one.

    The clamp only earns its place if the guard can actually fire on an object
    this factory produces. That is genuinely non-obvious: langchain passes
    `timeout=None` for both its clients, which defeats the guard's
    `client.timeout == DEFAULT_TIMEOUT` precondition, so on a BARE ChatAnthropic
    the guard IS dead code. Two separate reviews read langchain's source, drew
    exactly that conclusion, and were wrong about us, because our subclass
    overrides `_async_client` for loop-local pooling and pops `timeout` when it
    is None, which makes the SDK substitute DEFAULT_TIMEOUT and arms the guard.

    So assert it functionally, on a real factory-built model, end to end through
    our override + langchain + the SDK. If any of those three change, this fails
    instead of the clamp silently becoming either dead weight or a missing
    guard. No network: the guard raises before the request, and the sub-ceiling
    case only ever reaches a closed port.
    """
    monkeypatch.setattr(
        "nymeria.config.model_capabilities.get_max_output_tokens", lambda m: 128000
    )
    llm = providers._create_anthropic_llm(
        _anthropic_config(max_tokens=None, base_url="http://127.0.0.1:9")
    )
    messages = [{"role": "user", "content": "hi"}]

    # Above the ceiling: the guard must fire, pre-flight.
    with pytest.raises(ValueError, match="Streaming is required"):
        asyncio.run(llm.model_copy(update={"max_tokens": 128000}).ainvoke(messages))

    # At the clamped value the instance actually carries: must get past the
    # guard and fail at the socket instead. This is what the clamp buys.
    assert llm.max_tokens == NONSTREAMING_MAX_OUTPUT_TOKENS
    with pytest.raises(Exception) as excinfo:
        asyncio.run(llm.ainvoke(messages))
    assert not isinstance(excinfo.value, ValueError), (
        "The clamped value should reach the network, not trip the SDK guard."
    )


def test_per_model_nonstreaming_table_is_read_from_the_sdk():
    """The guard has a second clause the formula does not cover.

    ``messages.create`` passes ``MODEL_NONSTREAMING_TOKENS.get(model)`` into the
    same check, and the opus-4 / opus-4-1 families are pinned at 8192, well
    under the formula's 21333. Clamping to the formula alone would still send a
    request the SDK refuses, so read the table rather than copying its numbers.
    """
    # Import, do not importorskip. `anthropic` is a hard runtime dependency of
    # this project, so its absence is a broken environment, not a platform this
    # test is inapplicable to. A skipped test is a GREEN test: skipping here
    # would let the SDK rename this table, silently strand _model_nonstreaming_
    # ceiling at None, clamp opus-4 to 21333 instead of its real 8192 pin, and
    # break every ainvoke while CI stayed green. Fail loudly instead.
    from anthropic import _constants as constants

    table = getattr(constants, "MODEL_NONSTREAMING_TOKENS", None)
    assert table, (
        "anthropic._constants.MODEL_NONSTREAMING_TOKENS is gone or empty. "
        "_model_nonstreaming_ceiling now returns None for every model, so the "
        "per-model pin (opus-4 @ 8192) is no longer honoured. Re-derive it from "
        "the SDK before deleting this assertion."
    )

    model, pinned = next(iter(table.items()))
    assert instance_max_output_tokens(128_000, model) == min(
        pinned, NONSTREAMING_MAX_OUTPUT_TOKENS
    )
    # And the clamp honours it even when the formula would not have fired.
    assert instance_max_output_tokens(pinned + 1, model) == pinned


def test_langchain_fallback_matches_the_library_that_caused_the_outage():
    """Pins the substitution this whole module exists to defeat.

    claude-sonnet-5 is absent from the pinned library's profile table, so
    omitting max_tokens does not reach Anthropic blank: it arrives as 4096.
    """
    # Import, do not importorskip: see the sibling test. langchain-anthropic is
    # a hard dependency, and this constant is the exact value that caused the
    # outage, so losing sight of it must fail rather than skip.
    from langchain_anthropic import chat_models

    fallback = getattr(chat_models, "_FALLBACK_MAX_OUTPUT_TOKENS", None)
    assert fallback is not None, (
        "langchain-anthropic no longer exposes _FALLBACK_MAX_OUTPUT_TOKENS. "
        "langchain_fallback_max_tokens now degrades to its hardcoded floor, so "
        "this suite can no longer see what the library really substitutes."
    )

    assert langchain_fallback_max_tokens("claude-sonnet-5") == fallback
    # A model the library DOES know resolves to its profile value instead.
    assert langchain_fallback_max_tokens("claude-sonnet-4-5") != fallback


def test_langchain_fallback_degrades_to_the_floor_not_to_unknown(monkeypatch):
    """Vendor drift must degrade, not crash. The polarity is the whole point.

    Returning None here would make the caller skip the budget clamp, so a mere
    library rename would ship budget_tokens above langchain's invented cap and
    hard-400 every turn. 4096 is the lowest value it can invent, so sizing
    against it is always safe.
    """
    import builtins

    real_import = builtins.__import__

    def blocked(name, *args, **kwargs):
        if name == "langchain_anthropic.chat_models":
            raise ImportError("simulated vendor drift")
        return real_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", blocked)
    assert langchain_fallback_max_tokens("claude-sonnet-5") == 4096


# ---------------------------------------------------------------------------
# resolve_max_output_tokens
# ---------------------------------------------------------------------------


def test_explicit_setting_always_wins(monkeypatch):
    calls: list = []
    monkeypatch.setattr(
        "nymeria.config.model_capabilities.get_max_output_tokens",
        lambda model: calls.append(model),
    )
    assert resolve_max_output_tokens(_anthropic_config(max_tokens=8000)) == 8000
    assert calls == []  # counted, not raised: see the module docstring.


def test_resolves_from_catalog_when_unset(monkeypatch):
    monkeypatch.setattr(
        "nymeria.config.model_capabilities.get_max_output_tokens",
        lambda model: 128000,
    )
    assert resolve_max_output_tokens(_anthropic_config(max_tokens=None)) == 128000


def test_falls_back_to_probe_when_no_tier_knows_the_model(monkeypatch):
    # claude-sonnet-5 was absent from every catalog tier on the day it broke.
    # The provider itself is the only source that can know a model it has just
    # shipped, and the only one reachable through a metadata-blind gateway.
    monkeypatch.setattr(
        "nymeria.config.model_capabilities.get_max_output_tokens", lambda model: None
    )
    seen: dict = {}

    def fake_probe(model, *, base_url, api_key, **kwargs):
        seen.update(model=model, base_url=base_url, api_key=api_key)
        return 128000

    monkeypatch.setattr(
        "nymeria.config.model_capabilities.probe_max_output_tokens", fake_probe
    )
    resolved = resolve_max_output_tokens(_anthropic_config(max_tokens=None), probe=True)
    assert resolved == 128000
    assert seen["model"] == "claude-sonnet-5"
    assert seen["base_url"] == "http://cli-proxy.test:8317"


def test_probe_is_opt_in(monkeypatch):
    """The probe costs a live round trip, so it must never fire unasked.

    Counted rather than raised: the resolver swallows Exception by design, so
    a raising fake would be eaten and this test would pass unconditionally.
    """
    monkeypatch.setattr(
        "nymeria.config.model_capabilities.get_max_output_tokens", lambda model: None
    )
    calls: list = []
    monkeypatch.setattr(
        "nymeria.config.model_capabilities.probe_max_output_tokens",
        lambda *a, **k: calls.append(a),
    )
    assert resolve_max_output_tokens(_anthropic_config(max_tokens=None)) is None
    assert calls == []


def test_probe_is_skipped_without_credentials(monkeypatch):
    monkeypatch.setattr(
        "nymeria.config.model_capabilities.get_max_output_tokens", lambda model: None
    )
    calls: list = []
    monkeypatch.setattr(
        "nymeria.config.model_capabilities.probe_max_output_tokens",
        lambda *a, **k: calls.append(a),
    )
    assert (
        resolve_max_output_tokens(
            _anthropic_config(max_tokens=None, api_key=None), probe=True
        )
        is None
    )
    assert calls == []


def test_returns_none_and_warns_when_nothing_knows(monkeypatch, caplog):
    monkeypatch.setattr(
        "nymeria.config.model_capabilities.get_max_output_tokens", lambda model: None
    )
    monkeypatch.setattr(
        "nymeria.config.model_capabilities.probe_max_output_tokens",
        lambda *a, **k: None,
    )
    with caplog.at_level("WARNING", logger="nymeria"):
        assert (
            resolve_max_output_tokens(_anthropic_config(max_tokens=None), probe=True)
            is None
        )
    # The silent version of this condition is what made the original outage
    # invisible for two and a half weeks.
    assert "No output ceiling known" in caplog.text


def test_discovery_failure_does_not_raise(monkeypatch):
    def boom(*args, **kwargs):
        raise RuntimeError("catalog exploded")

    monkeypatch.setattr(
        "nymeria.config.model_capabilities.get_max_output_tokens", boom
    )
    monkeypatch.setattr(
        "nymeria.config.model_capabilities.probe_max_output_tokens", boom
    )
    assert (
        resolve_max_output_tokens(_anthropic_config(max_tokens=None), probe=True) is None
    )


# ---------------------------------------------------------------------------
# The Anthropic factory: the actual regression
# ---------------------------------------------------------------------------


def test_anthropic_factory_sends_a_resolved_ceiling(monkeypatch):
    """The bug, pinned. Unset max_tokens must not reach langchain's 4096."""
    monkeypatch.setattr(
        "nymeria.config.model_capabilities.get_max_output_tokens", lambda model: None
    )
    monkeypatch.setattr(
        "nymeria.config.model_capabilities.probe_max_output_tokens",
        lambda *a, **k: 128000,
    )
    llm = providers._create_anthropic_llm(
        _anthropic_config(max_tokens=None, extended_thinking=True, reasoning_effort="high")
    )
    assert llm.max_tokens != 4096
    # The instance carries the non-streaming-safe clamp; the streaming graph
    # node opts back up per call (test_output_truncation covers that path).
    assert llm.max_tokens == NONSTREAMING_MAX_OUTPUT_TOKENS


def test_anthropic_factory_honours_explicit_max_tokens(monkeypatch):
    monkeypatch.setattr(
        "nymeria.config.model_capabilities.get_max_output_tokens", lambda model: 128000
    )
    llm = providers._create_anthropic_llm(_anthropic_config(max_tokens=5000))
    assert llm.max_tokens == 5000


# ---------------------------------------------------------------------------
# instance_max_output_tokens / streaming_call_kwargs (decision (b): path-aware)
# ---------------------------------------------------------------------------


def test_instance_clamps_only_above_the_sdk_ceiling():
    assert instance_max_output_tokens(128000) == NONSTREAMING_MAX_OUTPUT_TOKENS
    assert instance_max_output_tokens(8000) == 8000
    assert instance_max_output_tokens(None) is None


def _anthropic_llm(monkeypatch, ceiling, model="claude-sonnet-5"):
    monkeypatch.setattr(
        "nymeria.config.model_capabilities.get_max_output_tokens", lambda m: ceiling
    )
    return providers._create_anthropic_llm(
        _anthropic_config(model=model, max_tokens=None)
    )


def test_streaming_opts_back_up_to_the_true_ceiling(monkeypatch):
    llm = _anthropic_llm(monkeypatch, 128000)
    assert llm.max_tokens == NONSTREAMING_MAX_OUTPUT_TOKENS
    assert streaming_call_kwargs(llm, 128000) == {"max_tokens": 128000}


def test_no_streaming_override_when_nothing_was_clamped(monkeypatch):
    llm = _anthropic_llm(monkeypatch, NONSTREAMING_MAX_OUTPUT_TOKENS)
    assert streaming_call_kwargs(llm, NONSTREAMING_MAX_OUTPUT_TOKENS) == {}
    assert streaming_call_kwargs(llm, None) == {}


def test_streaming_override_sees_through_bind_tools(monkeypatch):
    # The graph calls the tool-bound runnable, not the raw model.
    from langchain_core.tools import tool

    @tool
    def noop(x: str) -> str:
        """No-op."""
        return x

    llm = _anthropic_llm(monkeypatch, 128000)
    bound = llm.bind_tools([noop])
    assert streaming_call_kwargs(bound, 128000) == {"max_tokens": 128000}


def test_no_streaming_override_for_non_anthropic_providers():
    # The guard is an Anthropic SDK behaviour; nothing else has it.
    class _NotAnthropic:
        max_tokens = 128000

    assert streaming_call_kwargs(_NotAnthropic(), 128000) == {}
    assert streaming_call_kwargs(object(), 128000) == {}


# ---------------------------------------------------------------------------
# The budget_tokens invariant: budget must stay strictly below max_tokens
# ---------------------------------------------------------------------------


def _legacy_thinking_llm(
    monkeypatch, ceiling, effort="max", model="claude-3-7-sonnet-20250219"
):
    monkeypatch.setattr(
        "nymeria.config.model_capabilities.get_max_output_tokens", lambda m: ceiling
    )
    monkeypatch.setattr(
        "nymeria.config.model_capabilities.probe_max_output_tokens",
        lambda *a, **k: None,
    )
    return providers._create_anthropic_llm(
        _anthropic_config(
            model=model,
            max_tokens=None,
            extended_thinking=True,
            reasoning_effort=effort,
        )
    )


def test_legacy_thinking_budget_clamps_against_the_resolved_ceiling(monkeypatch):
    # The clamp used to guard on config.max_tokens, which is None in exactly the
    # case that matters: unset. A legacy model would then send budget_tokens
    # above the cap the client library was about to invent.
    llm = _legacy_thinking_llm(monkeypatch, 8000, effort="high")
    assert llm.thinking["type"] == "enabled"
    assert llm.thinking["budget_tokens"] < llm.max_tokens


def test_legacy_thinking_budget_clamps_against_langchains_invented_cap(monkeypatch):
    """The hole the resolved-ceiling clamp alone leaves open.

    When no tier knows the ceiling we send no max_tokens, but the request does
    not go out uncapped: langchain substitutes its own value. Sizing the budget
    against "nothing known" would ship budget_tokens above a cap we never saw,
    which is the original bug wearing a different hat.

    claude-opus-4-0 is the real case, not a contrived one: our catalog returns
    None for it, langchain invents 32000 from its own profile, and effort=max
    asks for a 49152 budget. Without this clamp that pair is a 400 on every
    turn. Pick the model deliberately here: on a model whose invented cap
    happens to exceed the effort budget (claude-3-7-sonnet invents 64000 > 49152)
    the invariant holds by luck and the test proves nothing.
    """
    llm = _legacy_thinking_llm(monkeypatch, None, model="claude-opus-4-0")
    invented = langchain_fallback_max_tokens("claude-opus-4-0")
    assert invented == 32000, "guard: langchain's profile for this model moved"

    # The instance carries the CLAMPED invention, not the invention itself.
    # Asserting equality with `invented` here would be asserting the bug: 32000
    # is above this model's 8192 SDK non-streaming pin, so every ainvoke would
    # raise ValueError before reaching the network (measured).
    assert llm.max_tokens == instance_max_output_tokens(invented, "claude-opus-4-0")
    assert llm.max_tokens == 8192
    assert llm.thinking["budget_tokens"] < llm.max_tokens

    # ...and the streaming path opts back up to the full invention, so clamping
    # for the non-streaming callers costs the user-facing path nothing.
    assert streaming_call_kwargs(llm, None) == {"max_tokens": invented}


@pytest.mark.parametrize("cap", [2048, 8000, 64000])
def test_legacy_thinking_budget_invariant_holds_across_caps(monkeypatch, cap):
    # Asserted against the value that actually ships, not against `cap`: a cap
    # above the SDK's non-streaming ceiling is clamped on the instance, and the
    # budget has to stay below THAT.
    llm = _legacy_thinking_llm(monkeypatch, cap)
    assert llm.max_tokens == min(cap, NONSTREAMING_MAX_OUTPUT_TOKENS)
    assert llm.thinking["budget_tokens"] < llm.max_tokens


@pytest.mark.parametrize("cap", [MIN_THINKING_BUDGET_TOKENS, 900])
def test_thinking_is_dropped_when_no_valid_budget_exists(monkeypatch, cap, caplog):
    """A cap at or below the budget floor is unsatisfiable, not clampable.

    Anthropic floors budget_tokens at 1024 AND requires it strictly below
    max_tokens, so no valid pair exists here. Clamping anyway would ship
    budget == max_tokens and 400 every turn.
    """
    with caplog.at_level("WARNING", logger="nymeria"):
        llm = _legacy_thinking_llm(monkeypatch, cap)
    assert llm.thinking in (None, {})
    assert "cannot fit extended thinking" in caplog.text


def test_adaptive_warns_when_the_cap_cannot_fit_the_reasoning(monkeypatch, caplog):
    # Adaptive exposes no budget knob, so a cap too small to fit the reasoning
    # can only be surfaced, not clamped. This is the authoring-time signal; the
    # runtime backstop is the truncation detector in nodes._finish_response.
    monkeypatch.setattr(
        "nymeria.config.model_capabilities.get_max_output_tokens", lambda model: None
    )
    with caplog.at_level("WARNING", logger="nymeria"):
        providers._create_anthropic_llm(
            _anthropic_config(
                max_tokens=4096, extended_thinking=True, reasoning_effort="high"
            )
        )
    assert "too small for adaptive thinking" in caplog.text


# ---------------------------------------------------------------------------
# The ceiling probe is credential egress. These tests exist because it leaked.
# ---------------------------------------------------------------------------


class _ProbeSpy:
    """Records every host the probe would POST a credential to."""

    def __init__(self):
        self.egress: list[tuple[str, str]] = []

    def __call__(self, url, **kwargs):
        self.egress.append((url, kwargs.get("headers", {}).get("x-api-key", "")))

        class _Rejected:
            status_code = 400
            text = ""

            @staticmethod
            def json():
                return {
                    "error": {
                        "message": (
                            "max_tokens: 99999999 > 128000, which is the maximum "
                            "allowed number of output tokens for claude-sonnet-5"
                        )
                    }
                }

        return _Rejected()


@pytest.fixture
def probe_spy(monkeypatch):
    from nymeria.config import model_capabilities

    spy = _ProbeSpy()
    monkeypatch.setattr(model_capabilities.httpx, "post", spy)
    model_capabilities._probe_cache.clear()
    model_capabilities._probe_cache_ts.clear()
    yield spy
    model_capabilities._probe_cache.clear()
    model_capabilities._probe_cache_ts.clear()


@pytest.mark.parametrize(
    "provider,secret",
    [
        ("openai", "sk-proj-PRETEND-OPENAI-SECRET"),
        ("openrouter", "sk-or-PRETEND-OPENROUTER-SECRET"),
        ("google_genai", "AIza-PRETEND-GOOGLE-SECRET"),
    ],
)
def test_probe_never_ships_a_foreign_credential_to_anthropic(
    probe_spy, provider, secret
):
    """REGRESSION, measured in production code, not hypothesised.

    The probe posts Anthropic-shaped headers to ``{base_url}/v1/messages`` and
    read its answer out of an Anthropic-worded error. It was gated on
    ``probe and config.api_key`` and nothing else, and ``base_url`` defaulted to
    ``https://api.anthropic.com``. So an OpenAI / OpenRouter / Google config
    whose model no catalog knew sent THAT vendor's live API key to Anthropic.

    The assertion is deliberately "zero egress", not "no ceiling returned": a
    version that fired the request and merely discarded the answer would still
    have leaked the key, and would still pass a result-shaped assertion.
    """
    config = _anthropic_config(
        provider=provider, model="model-no-catalog-knows", api_key=secret, base_url=None
    )
    resolve_max_output_tokens(config, probe=True)
    assert probe_spy.egress == [], (
        f"{provider}'s credential was transmitted to {probe_spy.egress}"
    )


def test_probe_still_runs_for_anthropic_and_for_a_claude_gateway(probe_spy):
    """The gate must not buy safety by disabling the feature.

    A metadata-blind gateway fronting a Claude subscription (CLIProxy, the
    reference deployment) is the ONE case where the probe is the only source of
    truth, so it has to survive the gate that blocks everyone else.
    """
    from nymeria.vendor.react_agent.providers import anthropic_probe_base_url

    native = _anthropic_config(model="claude-sonnet-5", api_key="k", base_url=None)
    assert anthropic_probe_base_url(native) == "https://api.anthropic.com"
    assert resolve_max_output_tokens(native, probe=True) == 128000
    assert probe_spy.egress[0][0].startswith("https://api.anthropic.com")

    gateway = _anthropic_config(
        model="claude-sonnet-5", api_key="k", base_url="http://cli-proxy-api:8317"
    )
    assert anthropic_probe_base_url(gateway) == "http://cli-proxy-api:8317"


def test_a_claude_gateway_route_without_a_base_url_does_not_fall_back_to_anthropic(
    probe_spy,
):
    """The subtle half of the leak.

    A gateway route with no base_url must resolve to "nowhere", not to
    api.anthropic.com: falling back there would hand the GATEWAY's credential to
    Anthropic. Only the native provider may imply Anthropic's own endpoint.
    """
    from nymeria.vendor.react_agent.providers import anthropic_probe_base_url

    config = _anthropic_config(provider="openai", api_key="sk-gateway", base_url=None)
    assert anthropic_probe_base_url(config) is None
