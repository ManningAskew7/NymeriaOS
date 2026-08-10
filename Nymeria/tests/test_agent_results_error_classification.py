"""Tests for classify_stream_exception in nymeria.core.agent_results.

Covers the provider-error -> frontend payload mapping, including the Anthropic
subscription "third-party / extra usage" 400 that is really a tool-name
collision (e.g. a tool named exactly like a known MCP/Exa integration tool).
"""

from nymeria.core.agent_results import classify_stream_exception


# --- Anthropic third-party / extra-usage 400 (tool-name collision) ------------

# The exact provider text seen when a bound tool name collides with a known
# Anthropic integration tool (e.g. the old `web_search_exa`).
_THIRD_PARTY_400 = (
    "Error code: 400 - {'type': 'error', 'error': {'type': 'invalid_request_error', "
    "'message': 'Third-party apps now draw from your extra usage, not your plan "
    "limits. Add more at claude.ai/settings/usage and keep going.'}, "
    "'request_id': 'req_011CbbpNTiH5e9oC782gjNww'}"
)


def test_third_party_extra_usage_maps_to_tool_name_hint():
    result = classify_stream_exception(Exception(_THIRD_PARTY_400))

    assert result["type"] == "error"
    assert result["code"] == "anthropic_third_party_tool_name"
    # Actionable, not the raw provider text.
    assert "tool-name collision" in result["content"]
    assert "rename" in result["content"].lower()
    assert "An error occurred" not in result["content"]
    assert result["details"]["http_status"] == 400
    assert result["details"]["hint"] == "tool_name_collision"


def test_third_party_detection_is_phrase_based_not_status_only():
    # A plain 400 without the third-party/extra-usage wording must NOT be
    # misclassified as a tool-name collision.
    result = classify_stream_exception(
        Exception("Error code: 400 - {'message': 'temperature must be <= 1'}")
    )
    assert result["code"] == "agent_runtime_error"


def test_extra_usage_phrase_alone_matches():
    # Robust to minor wording changes that keep the distinctive phrase.
    result = classify_stream_exception(
        Exception("400 - you must draw from your extra usage to continue")
    )
    assert result["code"] == "anthropic_third_party_tool_name"


# --- Regression guards for the other branches ---------------------------------


def test_openrouter_402_still_classified():
    err = Exception(
        "Error code: 402 - This request requires more credits. Requested up to "
        "8192 tokens, but can only afford 1200."
    )
    result = classify_stream_exception(err)
    assert result["code"] == "openrouter_insufficient_credits"
    assert result["details"]["http_status"] == 402
    assert result["details"]["requested_max_tokens"] == 8192
    assert result["details"]["affordable_max_tokens"] == 1200


def test_generic_error_falls_back_to_runtime_error():
    result = classify_stream_exception(Exception("kaboom"))
    assert result["code"] == "agent_runtime_error"
    assert result["content"] == "An error occurred: kaboom"


# --- CLIProxy turn-time hints (#148) ------------------------------------------
# The three taxonomy shapes gain the same actionable line /provider test
# renders, gated on the destination actually being a CLIProxy base. See
# docs/private/cliproxy.md "Interpreting proxy auth errors".

_CLIPROXY_BASE = "http://cli-proxy-api:8317"

_UNKNOWN_MODEL_502 = (
    "Error code: 502 - unknown provider for model gemini-flash-5"
)
_NOT_FOUND_404 = (
    "Error code: 404 - {'error': {'code': 404, 'message': "
    "'Requested entity was not found.', 'status': 'NOT_FOUND'}}"
)
_AUTH_UNAVAILABLE_503 = (
    "Error code: 503 - auth_unavailable: no auth available "
    "(providers=claude, model=claude-fable-5)"
)


def test_cliproxy_unknown_model_gains_hint_on_cliproxy_base():
    result = classify_stream_exception(
        Exception(_UNKNOWN_MODEL_502), base_url=_CLIPROXY_BASE
    )
    assert result["code"] == "cliproxy_model_error"
    # Raw provider text retained, hint appended.
    assert "unknown provider for model gemini-flash-5" in result["content"]
    assert "no logged-in subscription" in result["content"]
    assert result["details"]["http_status"] == 502


def test_cliproxy_upstream_not_found_gains_hint_on_cliproxy_base():
    result = classify_stream_exception(
        Exception(_NOT_FOUND_404), base_url=_CLIPROXY_BASE
    )
    assert result["code"] == "cliproxy_model_error"
    assert "does not serve this model id" in result["content"]
    assert result["details"]["http_status"] == 404


def test_cliproxy_auth_unavailable_gains_backoff_hint():
    result = classify_stream_exception(
        Exception(_AUTH_UNAVAILABLE_503), base_url=_CLIPROXY_BASE
    )
    assert result["code"] == "cliproxy_model_error"
    assert "error backoff" in result["content"]
    # Both causes named: the self-clearing backoff AND the revoked login
    # (which shares the wire shape and where re-login IS the fix).
    assert "re-login does not help" in result["content"]
    assert "revoked" in result["content"]
    assert result["details"]["http_status"] == 503


def test_cliproxy_shapes_untouched_off_cliproxy_base():
    """A direct-API failure carrying the same phrase gains no editorial, and
    renders byte-identically to the no-base-url call."""
    for text in (_UNKNOWN_MODEL_502, _NOT_FOUND_404, _AUTH_UNAVAILABLE_503):
        bare = classify_stream_exception(Exception(text))
        off_proxy = classify_stream_exception(
            Exception(text), base_url="https://api.example.com/v1"
        )
        assert off_proxy == bare
        assert off_proxy["code"] == "agent_runtime_error"


def test_cliproxy_base_without_matching_shape_stays_runtime_error():
    result = classify_stream_exception(
        Exception("kaboom"), base_url=_CLIPROXY_BASE
    )
    assert result["code"] == "agent_runtime_error"
    assert result["content"] == "An error occurred: kaboom"


# --- Facade wiring: the agent resolves the active destination -----------------


def _bare_agent():
    from types import SimpleNamespace

    from nymeria.core.agent import NymeriaAgent

    agent = object.__new__(NymeriaAgent)
    agent.settings = SimpleNamespace(llm_base_url=None)
    return agent


def test_facade_uses_thread_config_base_url():
    from types import SimpleNamespace

    agent = _bare_agent()
    agent._get_llm_config_for_thread = lambda thread_id: SimpleNamespace(
        base_url=_CLIPROXY_BASE
    )

    result = agent._classify_stream_exception(
        Exception(_UNKNOWN_MODEL_502), thread_id="t1"
    )
    assert result["code"] == "cliproxy_model_error"


def test_facade_never_falls_back_to_global_settings_base_url():
    """Without a thread there is no truthful destination: a global CLIProxy
    base must NOT editorialize a turn that may have run direct-API."""
    agent = _bare_agent()
    agent.settings.llm_base_url = _CLIPROXY_BASE

    result = agent._classify_stream_exception(Exception(_UNKNOWN_MODEL_502))
    assert result["code"] == "agent_runtime_error"


def test_facade_thread_config_without_base_url_gets_no_hint():
    """A thread whose provider resolves no base_url ran against the
    provider's default public endpoint; no CLIProxy hint applies."""
    from types import SimpleNamespace

    agent = _bare_agent()
    agent.settings.llm_base_url = _CLIPROXY_BASE
    agent._get_llm_config_for_thread = lambda thread_id: SimpleNamespace(
        base_url=None
    )

    result = agent._classify_stream_exception(
        Exception(_UNKNOWN_MODEL_502), thread_id="t1"
    )
    assert result["code"] == "agent_runtime_error"


def test_facade_resolution_fault_never_masks_the_turn_error():
    def _boom(thread_id):
        raise RuntimeError("config store down")

    agent = _bare_agent()
    agent._get_llm_config_for_thread = _boom

    result = agent._classify_stream_exception(
        Exception(_UNKNOWN_MODEL_502), thread_id="t1"
    )
    assert result["code"] == "agent_runtime_error"
    assert "unknown provider for model" in result["content"]


_INVALID_KEY_401 = "Error code: 401 - {'error': 'Invalid API key'}"
_RATE_LIMIT_429 = "Error code: 429 - {'type': 'rate_limit_error', 'message': 'Error'}"


def test_cliproxy_401_gains_subscription_relogin_hint():
    """The raw SDK copy says "Invalid API key", which misleads on a
    subscription-OAuth route (live 2026-08-10: lapsed Codex sub). Both real
    causes are named; classification is by status, not message text."""
    result = classify_stream_exception(
        Exception(_INVALID_KEY_401), base_url=_CLIPROXY_BASE
    )
    assert result["code"] == "cliproxy_model_error"
    assert "Invalid API key" in result["content"]  # raw text retained
    assert "subscription login" in result["content"]
    assert "/provider cliproxy" in result["content"]
    assert "gatekeeper" in result["content"]
    assert result["details"]["http_status"] == 401


def test_cliproxy_429_gains_quota_window_hint():
    result = classify_stream_exception(
        Exception(_RATE_LIMIT_429), base_url=_CLIPROXY_BASE
    )
    assert result["code"] == "cliproxy_model_error"
    assert "usage window" in result["content"]
    assert result["details"]["http_status"] == 429


def test_auth_and_quota_hints_untouched_off_cliproxy_base():
    for text in (_INVALID_KEY_401, _RATE_LIMIT_429):
        bare = classify_stream_exception(Exception(text))
        off_proxy = classify_stream_exception(
            Exception(text), base_url="https://api.example.com/v1"
        )
        assert off_proxy == bare
        assert bare["code"] == "agent_runtime_error"
