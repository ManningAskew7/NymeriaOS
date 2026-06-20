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

    text, model = llm_extract.run_extraction("page body content", "extract the pricing")
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

    text, model = llm_extract.run_extraction("page body", "prompt")
    assert text == "PART ONE"
    assert model == "fake"


def test_run_extraction_no_model_configured_returns_error(monkeypatch):
    class FakeConfig:
        model = ""

    monkeypatch.setattr(llm_extract, "build_extraction_llm_config", lambda settings: FakeConfig())

    text, model = llm_extract.run_extraction("body", "prompt")
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

    text, model = llm_extract.run_extraction("body", "prompt")
    # Error is generic (type name only); the exception text never leaks.
    assert text.startswith("[Error]: Extraction step failed: RuntimeError")
    assert "secret-url" not in text
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
