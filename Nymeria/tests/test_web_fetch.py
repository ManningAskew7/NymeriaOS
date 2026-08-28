"""Tests for the opt-in page-fetch tool (fetch_url_nymeria).

Covers registration in the Web group, the WEB metadata category/security level,
the SSRF-gated fetch path, content extraction dispatch, batch handling, error
envelopes, and the optional extraction step (secondary-model gating + attribution).
"""


from nymeria.tools import web_fetch
from nymeria.core.http_policy import HTTPPolicyDecision, HTTPPolicyViolation


class FakeResponse:
    """Stand-in usable both as the streamed requests response consumed by
    _fetch_one (iter_content/raise_for_status/headers) and as the object
    _extract_content consumes (.headers/.content/.url/.encoding)."""

    def __init__(self, *, content=b"", content_type="text/html", url="https://example.com/page", status=200, encoding="utf-8", with_content_length=True):
        self.content = content
        self.headers = {"content-type": content_type}
        if with_content_length:
            self.headers["content-length"] = str(len(content))
        self.url = url
        self.status_code = status
        self.encoding = encoding

    def raise_for_status(self):
        if self.status_code >= 400:
            import requests
            raise requests.exceptions.HTTPError(str(self.status_code), response=self)

    def iter_content(self, chunk_size=65536):
        for i in range(0, len(self.content), chunk_size):
            yield self.content[i:i + chunk_size]

    def close(self):
        pass

    @property
    def text(self):
        return self.content.decode(self.encoding or "utf-8", errors="replace")


def _patch_fetch(monkeypatch, response):
    """Make the gated fetch return ``response`` without touching the network."""
    monkeypatch.setattr(
        web_fetch,
        "requests_get_with_policy",
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
    from nymeria.tools import CATALOG_TOOLS
    from nymeria.tools.web_fetch import WEB_FETCH_TOOLS

    assert "fetch_url_nymeria" in CATALOG_TOOLS
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


def test_pdf_detected_by_magic_bytes_despite_octet_stream(monkeypatch):
    # Many servers send PDFs as application/octet-stream; the %PDF- header wins.
    monkeypatch.setattr(web_fetch, "_extract_pdf", lambda raw: "PDF BODY TEXT")
    resp = FakeResponse(content=b"%PDF-1.7\nbinary...", content_type="application/octet-stream")
    assert web_fetch._extract_content(resp, "markdown") == "PDF BODY TEXT"


def test_pdf_detected_by_url_extension(monkeypatch):
    monkeypatch.setattr(web_fetch, "_extract_pdf", lambda raw: "PDF BODY TEXT")
    resp = FakeResponse(
        content=b"no-magic-here",
        content_type="application/octet-stream",
        url="https://example.com/report.pdf?dl=1",
    )
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
    # No thread in config -> plain truncation (no spill path).
    _patch_fetch(monkeypatch, FakeResponse(content=_ARTICLE_HTML))
    monkeypatch.setattr(web_fetch, "_extract_html", lambda html, extract: "word " * 400)
    out = web_fetch.fetch_url_nymeria.func(url="https://example.com/page", max_length=500)
    assert "Content truncated to 500 of 2000 characters" in out
    assert "Full text saved to" not in out


def test_overflow_spills_full_text_to_thread_dir(monkeypatch, tmp_path):
    monkeypatch.setenv("NYMERIA_WORKSPACE_DIR", str(tmp_path))
    _patch_fetch(monkeypatch, FakeResponse(content=_ARTICLE_HTML))
    full = "word " * 4000  # 20000 chars, well over max_length
    monkeypatch.setattr(web_fetch, "_extract_html", lambda html, extract: full)
    out = web_fetch.fetch_url_nymeria.func(
        url="https://ex.com/page",
        max_length=500,
        config={"configurable": {"thread_id": "t-spill"}},
    )
    assert "Content truncated to 500 of 20000 characters" in out
    assert "Full text saved to" in out
    fetch_dir = tmp_path / "threads" / "t-spill" / "fetched"
    files = list(fetch_dir.glob("*.md"))
    assert len(files) == 1
    assert files[0].read_text() == full          # full content preserved on disk
    assert str(files[0]) in out                    # path surfaced to the agent
    assert len(out) < len(full)                    # returned preview is truncated


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

    monkeypatch.setattr(web_fetch, "requests_get_with_policy", boom)
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


# --- extraction (extraction_prompt) -------------------------------------------
# The extraction helpers themselves live in nymeria/tools/llm_extract.py and are
# unit-tested in test_llm_extract.py. These cover only the fetch integration: the
# gating knob and the model attribution rendered into the tool result.


def test_extraction_prompt_gates_the_llm_call(monkeypatch):
    # The optional `extraction_prompt` string is the knob: empty -> full page
    # (no LLM), non-empty -> the secondary model runs with that prompt and the
    # result is tagged with the model that produced it.
    _patch_fetch(monkeypatch, FakeResponse(content=_ARTICLE_HTML))
    calls = {}

    def fake_extraction(content, prompt):
        calls["prompt"] = prompt
        return "EXTRACTED", "fake-model", False

    monkeypatch.setattr(web_fetch, "run_extraction", fake_extraction)

    full = web_fetch.fetch_url_nymeria.func(url="https://example.com/page")
    assert "prompt" not in calls       # empty extraction_prompt never calls the model
    assert "The Heading" in full

    out = web_fetch.fetch_url_nymeria.func(
        url="https://example.com/page", extraction_prompt="pricing tiers"
    )
    assert calls["prompt"] == "pricing tiers"
    assert "EXTRACTED" in out
    assert "[Extracted by fake-model]" in out


def test_truncated_extraction_owns_up_in_the_attribution(monkeypatch):
    """#198: the fetch integration renders the shared attribution, so a cut
    extraction says so instead of presenting a prefix as the page's answer."""
    _patch_fetch(monkeypatch, FakeResponse(content=_ARTICLE_HTML))
    monkeypatch.setattr(
        web_fetch, "run_extraction", lambda content, prompt: ("EXTRACTED", "fake-model", True)
    )

    out = web_fetch.fetch_url_nymeria.func(
        url="https://example.com/page", extraction_prompt="pricing tiers"
    )
    assert "[Extracted by fake-model;" in out
    assert "hit its output limit" in out


def test_extraction_error_short_circuits(monkeypatch):
    # An [Error]: from the extraction step is returned as-is, with no attribution.
    _patch_fetch(monkeypatch, FakeResponse(content=_ARTICLE_HTML))
    monkeypatch.setattr(
        web_fetch,
        "run_extraction",
        lambda content, prompt: ("[Error]: Extraction step failed: X", "", False),
    )

    out = web_fetch.fetch_url_nymeria.func(
        url="https://example.com/page", extraction_prompt="anything"
    )
    assert out.startswith("[Error]: Extraction step failed")
    assert "Extracted by" not in out


# --- review hardening: size cap, redaction, spill fallback, sanitization ------


def test_redact_url_strips_credentials():
    assert web_fetch._redact_url("https://user:pass@host.example/p?q=1") == "https://host.example/p?q=1"
    assert web_fetch._redact_url("https://host.example/p") == "https://host.example/p"


def test_oversize_declared_content_length_rejected(monkeypatch):
    monkeypatch.setattr(web_fetch, "_MAX_FETCH_BYTES", 100)
    _patch_fetch(monkeypatch, FakeResponse(content=b"x" * 500, content_type="text/plain"))
    out = web_fetch.fetch_url_nymeria.func(url="https://ex.com/big")
    assert out.startswith("[Error]: Response too large")


def test_oversize_streamed_body_capped(monkeypatch):
    # No Content-Length -> the streaming loop must enforce the cap.
    monkeypatch.setattr(web_fetch, "_MAX_FETCH_BYTES", 100)
    _patch_fetch(monkeypatch, FakeResponse(content=b"x" * 500, content_type="text/plain", with_content_length=False))
    out = web_fetch.fetch_url_nymeria.func(url="https://ex.com/chunked")
    assert out.startswith("[Error]") and "exceeded" in out


def test_get_thread_fetch_dir_sanitizes_thread_id(monkeypatch, tmp_path):
    monkeypatch.setenv("NYMERIA_WORKSPACE_DIR", str(tmp_path))
    import nymeria.core.attachment_sandbox as sandbox

    d = sandbox.get_thread_fetch_dir("../../etc/evil")
    rel = d.relative_to(tmp_path.resolve())
    # Traversal collapses into a single sanitized path segment under threads/.
    assert rel.parts[0] == "threads"
    assert rel.parts[-1] == "fetched"
    assert len(rel.parts) == 3


def test_spill_write_failure_falls_back_to_plain_truncation(monkeypatch, tmp_path):
    monkeypatch.setenv("NYMERIA_WORKSPACE_DIR", str(tmp_path))
    _patch_fetch(monkeypatch, FakeResponse(content=_ARTICLE_HTML))
    monkeypatch.setattr(web_fetch, "_extract_html", lambda html, extract: "word " * 4000)
    import nymeria.core.attachment_sandbox as sandbox

    def boom(thread_id):
        raise OSError("disk full")

    monkeypatch.setattr(sandbox, "get_thread_fetch_dir", boom)
    out = web_fetch.fetch_url_nymeria.func(
        url="https://ex.com/page",
        max_length=500,
        config={"configurable": {"thread_id": "t-x"}},
    )
    assert "Content truncated to 500 of 20000 characters" in out
    assert "Full text saved to" not in out


# --- round 2: encoding + comma-split ------------------------------------------


_UTF8_ACCENTED_HTML = (
    "<html><head><title>t</title></head><body><article>"
    "<p>This is a sufficiently long paragraph of genuine article body content so it "
    "clears the thin-content threshold. Café résumé naïve accents and unicode must "
    "survive extraction without mojibake corruption of any kind here.</p>"
    "</article></body></html>"
).encode("utf-8")


def test_charsetless_utf8_decodes_without_mojibake(monkeypatch):
    # text/html WITHOUT a charset, and requests-style encoding=ISO-8859-1: the tool
    # must default to utf-8 (not latin-1) so accented text survives.
    _patch_fetch(
        monkeypatch,
        FakeResponse(content=_UTF8_ACCENTED_HTML, content_type="text/html", encoding="ISO-8859-1"),
    )
    out = web_fetch.fetch_url_nymeria.func(url="https://ex.com/article")
    assert "Café résumé naïve" in out
    assert "CafÃ©" not in out  # the mojibake form


def test_comma_inside_query_not_split(monkeypatch):
    _patch_fetch(monkeypatch, FakeResponse(content=_ARTICLE_HTML, url="https://a.example/p"))
    out = web_fetch.fetch_url_nymeria.func(urls="https://a.example/p?lat=1,2,3")
    assert out.count("=== URL") == 0  # single URL, not fragmented by query commas


def test_comma_between_full_urls_splits(monkeypatch):
    _patch_fetch(monkeypatch, FakeResponse(content=_ARTICLE_HTML))
    out = web_fetch.fetch_url_nymeria.func(urls="https://a.example/p, https://b.example/p")
    assert "=== URL 1/2: https://a.example/p ===" in out
    assert "=== URL 2/2: https://b.example/p ===" in out
