"""Unit tests for the shared secondary-LLM extraction step.

`run_extraction` and `build_extraction_llm_config` back both
`fetch_url_nymeria(extraction_prompt=...)` and `file_read(extraction_prompt=...)`.
"""

from __future__ import annotations

from nymeria.tools import llm_extract


def test_run_extraction_invokes_secondary_model(monkeypatch):
    from nymeria.vendor.react_agent import providers

    class FakeMessage:
        content = "EXTRACTED SUMMARY"

    seen = {}

    class FakeLLM:
        def invoke(self, messages, config=None):
            seen["config"] = config
            return FakeMessage()

    class FakeConfig:
        model = "fake-model"

    monkeypatch.setattr(llm_extract, "build_extraction_llm_config", lambda settings: FakeConfig())
    monkeypatch.setattr(providers, "create_llm", lambda config: FakeLLM())

    text, model, _truncated = llm_extract.run_extraction("page body content", "extract the pricing")
    assert text == "EXTRACTED SUMMARY"
    assert model == "fake-model"
    # The nested call MUST sever callbacks so its tokens never leak into the
    # parent agent's astream_events transcript.
    assert seen["config"] == {"callbacks": []}


def test_run_extraction_handles_list_content(monkeypatch):
    from nymeria.vendor.react_agent import providers

    class FakeMessage:
        content = [
            {"type": "text", "text": "PART ONE"},
            {"type": "text", "text": None},   # must not crash the join
            {"type": "tool_use"},             # no text key
        ]

    class FakeLLM:
        def invoke(self, messages, config=None):
            return FakeMessage()

    class FakeConfig:
        model = "fake"

    monkeypatch.setattr(llm_extract, "build_extraction_llm_config", lambda settings: FakeConfig())
    monkeypatch.setattr(providers, "create_llm", lambda config: FakeLLM())

    text, model, _truncated = llm_extract.run_extraction("page body", "prompt")
    assert text == "PART ONE"
    assert model == "fake"


def _fake_llm_with_metadata(monkeypatch, *, metadata, content="EXTRACTED"):
    from nymeria.vendor.react_agent import providers

    class FakeMessage:
        pass

    FakeMessage.content = content
    FakeMessage.response_metadata = metadata

    class FakeLLM:
        def invoke(self, messages, config=None):
            return FakeMessage()

    class FakeConfig:
        model = "fake-model"

    monkeypatch.setattr(llm_extract, "build_extraction_llm_config", lambda settings: FakeConfig())
    monkeypatch.setattr(providers, "create_llm", lambda config: FakeLLM())


def test_run_extraction_flags_an_output_ceiling_cut_provider_blind(monkeypatch):
    """#198: the truncated flag comes from the response's own stop reason,
    across every provider spelling `is_truncated_metadata` knows (OpenAI
    `length`, Anthropic `max_tokens`, Gemini's UPPERCASE enum, the
    Responses-API `status: incomplete`), and stays False on a clean stop."""
    cases = [
        ({"finish_reason": "length"}, True),
        ({"stop_reason": "max_tokens"}, True),
        ({"finish_reason": "MAX_TOKENS"}, True),
        ({"status": "incomplete"}, True),
        ({"finish_reason": "stop"}, False),
        ({"stop_reason": "end_turn"}, False),
        ({}, False),
    ]
    for metadata, expected in cases:
        _fake_llm_with_metadata(monkeypatch, metadata=metadata)
        result = llm_extract.run_extraction("body", "prompt")
        assert result.truncated is expected, metadata
        assert result.text == "EXTRACTED"
        assert result.model == "fake-model"


def test_extraction_attribution_is_the_one_spelling():
    """Every consumer renders through this helper, so the completeness claim
    and its retraction cannot drift apart per tool."""
    assert llm_extract.extraction_attribution("m", False) == "[Extracted by m]"
    cut = llm_extract.extraction_attribution("m", True)
    assert cut.startswith("[Extracted by m;")
    assert "hit its output limit" in cut
    assert "the tail may be missing" in cut


def test_run_extraction_no_model_configured_returns_error(monkeypatch):
    class FakeConfig:
        model = ""

    monkeypatch.setattr(llm_extract, "build_extraction_llm_config", lambda settings: FakeConfig())

    text, model, _truncated = llm_extract.run_extraction("body", "prompt")
    assert text.startswith("[Error]: No extraction model configured")
    assert model == ""


def test_run_extraction_swallows_exceptions(monkeypatch):
    from nymeria.vendor.react_agent import providers

    class FakeConfig:
        model = "m"

    def boom(config):
        raise RuntimeError("provider blew up with secret-url://x")

    monkeypatch.setattr(llm_extract, "build_extraction_llm_config", lambda settings: FakeConfig())
    monkeypatch.setattr(providers, "create_llm", boom)

    text, model, _truncated = llm_extract.run_extraction("body", "prompt")
    # Error is generic (type name only); the exception text never leaks.
    assert text.startswith("[Error]: Extraction step failed: RuntimeError")
    assert "secret-url" not in text
    assert model == ""


def test_run_extraction_cliproxy_429_surfaces_quota_hint_without_retry(monkeypatch):
    # #161: a 429 on a CLIProxy route is a documented quota-window condition,
    # but the error surface flattened it to a bare class name, which read as a
    # mystery provider bug for three sessions. The shared cliproxy_failure_hint
    # copy must ride the tool error, the base URL must not, and a 429 must NOT
    # be retried (the window clears in hours, not seconds).
    from nymeria.vendor.react_agent import providers

    class FakeRateLimitError(Exception):
        status_code = 429

    calls = {"n": 0}

    class FakeLLM:
        def invoke(self, messages, config=None):
            calls["n"] += 1
            raise FakeRateLimitError("Error code: 429 rate_limit_error")

    class FakeConfig:
        model = "claude-opus-4-8"
        base_url = "http://cli-proxy-api:8317"

    monkeypatch.setattr(llm_extract, "build_extraction_llm_config", lambda settings: FakeConfig())
    monkeypatch.setattr(providers, "create_llm", lambda config: FakeLLM())

    text, model, _truncated = llm_extract.run_extraction("body", "prompt")

    assert text.startswith("[Error]: Extraction step failed: FakeRateLimitError")
    assert "usage window" in text
    assert "cli-proxy-api" not in text
    assert calls["n"] == 1
    assert model == ""


def test_run_extraction_does_not_retry_429_with_bland_text(monkeypatch):
    # The real CLIProxy wire shape is `429 {'type':'rate_limit_error',
    # 'message':'Error'}`: the status attribute alone must block the retry
    # even when the text carries no rate-limit words (kills the mutant that
    # drops the status guard and leans on the text guard).
    from nymeria.vendor.react_agent import providers

    class FakeStatusOnlyError(Exception):
        status_code = 429

    calls = {"n": 0}

    class FakeLLM:
        def invoke(self, messages, config=None):
            calls["n"] += 1
            raise FakeStatusOnlyError("Error")

    class FakeConfig:
        model = "m"
        base_url = None

    monkeypatch.setattr(llm_extract, "build_extraction_llm_config", lambda settings: FakeConfig())
    monkeypatch.setattr(providers, "create_llm", lambda config: FakeLLM())

    text, _model, _truncated = llm_extract.run_extraction("body", "prompt")

    assert text.startswith("[Error]: Extraction step failed: FakeStatusOnlyError")
    assert calls["n"] == 1


def test_run_extraction_does_not_retry_rate_limit_text_on_retryable_status(monkeypatch):
    # A retryable status (503) whose text names a rate limit must NOT retry:
    # the text guard is the deliberate belt over the status check (kills the
    # mutant that drops the text guard, which the 429-status tests cannot).
    from nymeria.vendor.react_agent import providers

    class FakeOverloadedError(Exception):
        status_code = 503

    calls = {"n": 0}

    class FakeLLM:
        def invoke(self, messages, config=None):
            calls["n"] += 1
            raise FakeOverloadedError("503 rate_limit: please slow down")

    class FakeConfig:
        model = "m"
        base_url = None

    monkeypatch.setattr(llm_extract, "build_extraction_llm_config", lambda settings: FakeConfig())
    monkeypatch.setattr(providers, "create_llm", lambda config: FakeLLM())

    text, _model, _truncated = llm_extract.run_extraction("body", "prompt")

    assert text.startswith("[Error]: Extraction step failed: FakeOverloadedError")
    assert calls["n"] == 1


def test_run_extraction_retries_transient_fault_once(monkeypatch):
    # One short retry on transient faults (5xx/timeout/connection) only: a
    # blip must not kill an individual tool result, but the second failure is
    # final (no retry storm inside a user-facing tool call).
    import time as time_module

    from nymeria.vendor.react_agent import providers

    class FakeServerError(Exception):
        status_code = 500

    class FakeMessage:
        content = "RECOVERED"

    calls = {"n": 0}

    class FakeLLM:
        def invoke(self, messages, config=None):
            calls["n"] += 1
            if calls["n"] == 1:
                raise FakeServerError("Error code: 500 upstream hiccup")
            return FakeMessage()

    class FakeConfig:
        model = "m"
        base_url = None

    monkeypatch.setattr(time_module, "sleep", lambda seconds: None)
    monkeypatch.setattr(llm_extract, "build_extraction_llm_config", lambda settings: FakeConfig())
    monkeypatch.setattr(providers, "create_llm", lambda config: FakeLLM())

    text, model, _truncated = llm_extract.run_extraction("body", "prompt")

    assert text == "RECOVERED"
    assert model == "m"
    assert calls["n"] == 2


def test_run_extraction_transient_fault_fails_after_single_retry(monkeypatch):
    import time as time_module

    from nymeria.vendor.react_agent import providers

    class FakeServerError(Exception):
        status_code = 500

    calls = {"n": 0}

    class FakeLLM:
        def invoke(self, messages, config=None):
            calls["n"] += 1
            raise FakeServerError("Error code: 500 upstream down")

    class FakeConfig:
        model = "m"
        base_url = None

    monkeypatch.setattr(time_module, "sleep", lambda seconds: None)
    monkeypatch.setattr(llm_extract, "build_extraction_llm_config", lambda settings: FakeConfig())
    monkeypatch.setattr(providers, "create_llm", lambda config: FakeLLM())

    text, model, _truncated = llm_extract.run_extraction("body", "prompt")

    assert text.startswith("[Error]: Extraction step failed: FakeServerError")
    # No base_url => no CLIProxy hint appended; the generic pointer stands.
    assert text.endswith("(see server logs)")
    assert calls["n"] == 2
    assert model == ""


def test_build_extraction_config_falls_back_to_main_model():
    # Background tier unset => resolves to the main provider + model, inheriting
    # the main base_url and key (same-provider branch).
    class FakeSettings:
        llm_background_model = None
        llm_background_base_url = None
        llm_provider = "openai"
        llm_model = "gpt-main"
        llm_base_url = None
        openai_api_key = "k"
        openrouter_api_key = None
        anthropic_api_key = None
        anthropic_direct_api_key = None
        llm_provider_route = None
        openai_api_mode = "responses"

        def get_api_key_for_provider(self):
            return "k"

    cfg = llm_extract.build_extraction_llm_config(FakeSettings())
    assert cfg.provider == "openai"
    assert cfg.model == "gpt-main"
    assert cfg.api_key == "k"


def test_build_extraction_config_same_provider_inherits_main_base_url():
    # A bare background model on the same provider as main inherits the main
    # base_url + anthropic key nuance. This is what keeps CLIProxy working: the
    # proxy is reached via the generic LLM_BASE_URL, not a provider-specific env.
    class FakeSettings:
        llm_background_model = "claude-haiku-4-5-20251001"
        llm_background_base_url = None
        llm_provider = "anthropic"
        llm_model = "claude-opus-4-8"
        llm_base_url = "http://cli-proxy-api:8317"
        openai_api_key = None
        openrouter_api_key = None
        anthropic_api_key = "cpx-key"
        anthropic_direct_api_key = "direct-key"
        llm_provider_route = None
        openai_api_mode = "responses"

        def get_api_key_for_provider(self):
            return None

    cfg = llm_extract.build_extraction_llm_config(FakeSettings())
    assert cfg.provider == "anthropic"
    assert cfg.model == "claude-haiku-4-5-20251001"
    assert cfg.base_url == "http://cli-proxy-api:8317"
    # base_url present => the proxy key, not the anthropic-direct key.
    assert cfg.api_key == "cpx-key"


def test_build_extraction_config_cross_provider_with_base_url_override():
    # A cross-provider background model with an explicit base_url override goes
    # through the target provider's own resolution.
    class FakeSettings:
        llm_background_model = "openai:qwen2.5-7b"
        llm_background_base_url = "http://localhost:1234/v1"
        llm_provider = "anthropic"
        llm_model = "claude-x"
        llm_base_url = None
        openai_api_key = None
        openrouter_api_key = None
        anthropic_api_key = None
        anthropic_direct_api_key = None
        llm_provider_route = None
        openai_api_mode = "responses"

        def get_api_key_for_provider(self):
            return None

    cfg = llm_extract.build_extraction_llm_config(FakeSettings())
    assert cfg.model == "qwen2.5-7b"
    assert cfg.provider == "openai"
    assert cfg.base_url == "http://localhost:1234/v1"


def test_build_extraction_config_cross_provider_derives_cliproxy_openai_url():
    # main=anthropic via CLIProxy, background=openai with NO base_url override:
    # must derive the openai-compat /v1 path on the same proxy, NOT bill to
    # api.openai.com direct.
    class FakeSettings:
        llm_background_model = "openai:gpt-4o-mini"
        llm_background_base_url = None
        llm_provider = "anthropic"
        llm_model = "claude-opus-4-8"
        llm_base_url = "http://cli-proxy-api:8317"
        openai_api_key = "cpx-key"
        openrouter_api_key = None
        anthropic_api_key = "cpx-key"
        anthropic_direct_api_key = None
        llm_provider_route = None
        openai_api_mode = "responses"

        def get_api_key_for_provider(self):
            return "cpx-key"

    cfg = llm_extract.build_extraction_llm_config(FakeSettings())
    assert cfg.provider == "openai"
    assert cfg.model == "gpt-4o-mini"
    assert cfg.base_url == "http://cli-proxy-api:8317/v1"


def test_build_extraction_config_cross_provider_derives_cliproxy_anthropic_url():
    # main=openai via CLIProxy (/v1), background=anthropic with no override:
    # must strip /v1 to reach the anthropic OAuth path at the proxy root, and
    # use the proxy key (base_url present), not the direct key.
    class FakeSettings:
        llm_background_model = "anthropic:claude-haiku-4-5-20251001"
        llm_background_base_url = None
        llm_provider = "openai"
        llm_model = "gpt-5.5"
        llm_base_url = "http://cli-proxy-api:8317/v1"
        openai_api_key = None
        openrouter_api_key = None
        anthropic_api_key = "cpx-anthropic"
        anthropic_direct_api_key = "direct-key"
        llm_provider_route = None
        openai_api_mode = "responses"

        def get_api_key_for_provider(self):
            return None

    cfg = llm_extract.build_extraction_llm_config(FakeSettings())
    assert cfg.provider == "anthropic"
    assert cfg.model == "claude-haiku-4-5-20251001"
    assert cfg.base_url == "http://cli-proxy-api:8317"
    assert cfg.api_key == "cpx-anthropic"
