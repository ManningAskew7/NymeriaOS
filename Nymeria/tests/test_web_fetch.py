"""Tests for the opt-in page-fetch tool (fetch_url_nymeria).

Covers registration in the Web group, the WEB metadata category/security level,
the SSRF-gated fetch path, content extraction dispatch, batch handling, error
envelopes, and the optional summarize step (secondary-model wiring).
"""

import httpx
import pytest

from nymeria.tools import web_fetch
from nymeria.core.http_policy import HTTPPolicyDecision, HTTPPolicyViolation


@pytest.fixture(autouse=True)
def clear_settings_cache():
    from nymeria.config.settings import get_settings

    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


class FakeResponse:
    """Minimal stand-in for an httpx.Response returned by the policy wrapper."""

    def __init__(self, *, content=b"", content_type="text/html", url="https://example.com/page", status=200, encoding="utf-8"):
        self.content = content
        self.headers = {"content-type": content_type}
        self.url = url
        self.status_code = status
        self.encoding = encoding

    def raise_for_status(self):
        if self.status_code >= 400:
            raise httpx.HTTPStatusError("error", request=None, response=self)

    @property
    def text(self):
        return self.content.decode(self.encoding or "utf-8", errors="replace")


def _patch_fetch(monkeypatch, response):
    """Make the policy wrapper return ``response`` without touching the network."""
    monkeypatch.setattr(
        web_fetch,
        "httpx_request_with_policy",
        lambda *a, **k: (response, [], None),
    )


_ARTICLE_HTML = (
    b"<html><head><title>Real &amp; Title</title></head><body>"
    b"<nav>Home About Contact</nav>"
    b"<article><h1>The Heading</h1>"
    b"<p>This is the first substantial paragraph of genuine article content that the "
    b"extraction pipeline should keep. It comfortably clears the thin-content threshold "
    b"so the primary extractor returns it rather than falling through.</p>"
    b"<p>A second paragraph with even more prose so there is no doubt that real body text "
    b"is present and the boilerplate navigation and footer are removed cleanly.</p>"
    b"</article><footer>Copyright 2026</footer></body></html>"
)


# --- registration / metadata --------------------------------------------------


def test_registered_in_web_group():
    from nymeria.tools import OPTIONAL_TOOLS
    from nymeria.tools.web_fetch import WEB_FETCH_TOOLS

    assert "fetch_url_nymeria" in OPTIONAL_TOOLS
    assert [t.name for t in WEB_FETCH_TOOLS] == ["fetch_url_nymeria"]


def test_metadata_general_category_and_safe():
    from nymeria.tools.metadata import (
        SecurityLevel,
        ToolCategory,
        TOOL_METADATA,
        ensure_builtin_tool_metadata,
    )

    ensure_builtin_tool_metadata()
    meta = TOOL_METADATA["fetch_url_nymeria"]
    # Lives in the GENERAL group alongside the web_search_* tools so it renders
    # in every deployed frontend build (no hardcoded category list to update).
    assert meta.category == ToolCategory.GENERAL
    assert meta.security_level == SecurityLevel.SAFE
    assert meta.default_enabled is False
    assert TOOL_METADATA["web_search_perplexity"].category == ToolCategory.GENERAL


# --- argument handling --------------------------------------------------------


def test_requires_a_url():
    assert web_fetch.fetch_url_nymeria.func() == (
        "[Error]: Provide a url or pipe/comma-separated urls."
    )


# --- extraction ---------------------------------------------------------------


def test_extract_html_markdown_strips_boilerplate():
    md = web_fetch._extract_html(_ARTICLE_HTML.decode(), "markdown")
    assert "# The Heading" in md
    assert "substantial paragraph" in md
    assert "About Contact" not in md  # nav removed
    assert "Copyright 2026" not in md  # footer removed


def test_extract_title_unescapes():
    assert web_fetch._extract_title(_ARTICLE_HTML.decode()) == "Real & Title"


def test_pdf_dispatch(monkeypatch):
    monkeypatch.setattr(web_fetch, "_extract_pdf", lambda raw: "PDF BODY TEXT")
    resp = FakeResponse(content=b"%PDF-1.4 ...", content_type="application/pdf")
    assert web_fetch._extract_content(resp, "markdown") == "PDF BODY TEXT"


def test_plaintext_passthrough():
    resp = FakeResponse(content=b"just some plain text", content_type="text/plain")
    assert web_fetch._extract_content(resp, "text") == "just some plain text"


def test_unsupported_content_type():
    resp = FakeResponse(content=b"\x00\x01", content_type="application/octet-stream")
    out = web_fetch._extract_content(resp, "markdown")
    assert out.startswith("[Error]: Unsupported content type")


# --- single + batch render ----------------------------------------------------


def test_single_fetch_renders_header_and_content(monkeypatch):
    _patch_fetch(monkeypatch, FakeResponse(content=_ARTICLE_HTML, url="https://example.com/page"))
    out = web_fetch.fetch_url_nymeria.func(url="https://example.com/page")
    assert "# Real & Title" in out
    assert "Source: https://example.com/page" in out
    assert "The Heading" in out


def test_truncation_marker(monkeypatch):
    _patch_fetch(monkeypatch, FakeResponse(content=_ARTICLE_HTML))
    monkeypatch.setattr(web_fetch, "_extract_html", lambda html, extract: "word " * 400)
    out = web_fetch.fetch_url_nymeria.func(url="https://example.com/page", max_length=500)
    assert "[Content truncated to 500 characters]" in out


def test_batch_urls_split_and_headers(monkeypatch):
    _patch_fetch(monkeypatch, FakeResponse(content=_ARTICLE_HTML))
    out = web_fetch.fetch_url_nymeria.func(urls="https://a.example | https://b.example, https://c.example")
    assert "=== URL 1/3: https://a.example ===" in out
    assert "=== URL 2/3: https://b.example ===" in out
    assert "=== URL 3/3: https://c.example ===" in out


# --- error envelopes ----------------------------------------------------------


def test_ssrf_blocked_returns_error(monkeypatch):
    def boom(*a, **k):
        raise HTTPPolicyViolation(
            HTTPPolicyDecision(False, "metadata_target", "http://169.254.169.254", "169.254.169.254")
        )

    monkeypatch.setattr(web_fetch, "httpx_request_with_policy", boom)
    out = web_fetch.fetch_url_nymeria.func(url="http://169.254.169.254/latest/meta-data")
    assert out.startswith("[Error]: Blocked by egress policy")


def test_http_error_returns_error(monkeypatch):
    _patch_fetch(monkeypatch, FakeResponse(status=404))
    out = web_fetch.fetch_url_nymeria.func(url="https://example.com/missing")
    assert out.startswith("[Error]: HTTP 404")


def test_thin_content_returns_error(monkeypatch):
    _patch_fetch(monkeypatch, FakeResponse(content=b"<html><body><nav>x</nav></body></html>"))
    monkeypatch.setattr(web_fetch, "_extract_html", lambda html, extract: "")
    out = web_fetch.fetch_url_nymeria.func(url="https://example.com/spa")
    assert out.startswith("[Error]: Could not extract readable content")


# --- summarize ----------------------------------------------------------------


def test_summarize_invokes_secondary_model(monkeypatch):
    from nymeria.vendor.react_agent import providers

    class FakeMessage:
        content = "EXTRACTED SUMMARY"

    class FakeLLM:
        def invoke(self, messages):
            return FakeMessage()

    class FakeConfig:
        model = "fake-model"

    monkeypatch.setattr(web_fetch, "_build_fetch_summary_llm_config", lambda settings: FakeConfig())
    monkeypatch.setattr(providers, "create_llm", lambda config: FakeLLM())

    out = web_fetch._maybe_summarize("page body content", "extract the pricing")
    assert out == "EXTRACTED SUMMARY"


def test_summary_config_falls_back_to_main_model():
    class FakeSettings:
        fetch_summary_model = None
        fetch_summary_provider = None
        fetch_summary_base_url = None
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

    cfg = web_fetch._build_fetch_summary_llm_config(FakeSettings())
    assert cfg.provider == "openai"
    assert cfg.model == "gpt-main"


def test_summary_config_uses_override_model():
    class FakeSettings:
        fetch_summary_model = "qwen2.5-7b"
        fetch_summary_provider = "openai"
        fetch_summary_base_url = "http://localhost:1234/v1"
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

    cfg = web_fetch._build_fetch_summary_llm_config(FakeSettings())
    assert cfg.model == "qwen2.5-7b"
    assert cfg.provider == "openai"
    assert cfg.base_url == "http://localhost:1234/v1"
