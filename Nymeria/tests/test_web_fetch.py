"""Tests for the opt-in page-fetch tool (fetch_url_nymeria).

Covers registration in the Web group, the WEB metadata category/security level,
the SSRF-gated fetch path, content extraction dispatch, batch handling, error
envelopes, and the optional extraction step (secondary-model gating + attribution).
"""


import pytest

from nymeria.tools import web_fetch
from nymeria.core.http_policy import HTTPPolicyDecision, HTTPPolicyViolation


class FakeResponse:
    """Stand-in for the streamed requests response consumed by _fetch_one
    (iter_content/raise_for_status/headers).

    Headers use requests' own CaseInsensitiveDict with CAPITALIZED keys, exactly
    as a real server sends them and a real response holds them: _fetch_one's own
    reads stay case-insensitive, while copying the dict preserves the server's
    casing. The plain lowercase dict this double used to carry is a shape that
    cannot occur in production, and it hid a bug that made every content type
    read as absent. Do not "tidy" this back to a plain dict: _fetch_one owns the
    normalization and these tests exist to prove it happens. Build the downstream
    carrier with _fetched(), never by hand, so extraction is always exercised
    through that normalization.
    """

    def __init__(self, *, content=b"", content_type="text/html", url="https://example.com/page", status=200, encoding="utf-8", with_content_length=True):
        from requests.structures import CaseInsensitiveDict
        from requests.utils import get_encoding_from_headers

        self.content = content
        self.headers = CaseInsensitiveDict({"Content-Type": content_type})
        if with_content_length:
            self.headers["Content-Length"] = str(len(content))
        self.url = url
        self.status_code = status
        # requests derives .encoding from the header's charset, so a test that
        # declares one gets the real behaviour rather than the caller's guess.
        self.encoding = (
            get_encoding_from_headers(self.headers)
            if "charset=" in content_type.lower()
            else encoding
        )

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


def _fetched(monkeypatch, **kwargs):
    """Build the carrier _extract_content consumes the way production does.

    Routing through _fetch_one is the whole point: that is where header keys are
    normalized, so a test that constructs the carrier by hand cannot catch a
    case-sensitivity regression.
    """
    _patch_fetch(monkeypatch, FakeResponse(**kwargs))
    fetched, error, _final = web_fetch._fetch_one(kwargs.get("url", "https://example.com/page"))
    assert error is None, error
    return fetched


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
    f = _fetched(monkeypatch, content=b"%PDF-1.4 ...", content_type="application/pdf")
    assert web_fetch._extract_content(f, "markdown") == ("PDF BODY TEXT", "")


def test_pdf_detected_by_magic_bytes_despite_octet_stream(monkeypatch):
    # B8: many servers send PDFs as application/octet-stream; the %PDF- header
    # wins, and must keep winning over the new binary/text sniff.
    monkeypatch.setattr(web_fetch, "_extract_pdf", lambda raw: "PDF BODY TEXT")
    f = _fetched(monkeypatch, content=b"%PDF-1.7\nbinary...", content_type="application/octet-stream")
    assert web_fetch._extract_content(f, "markdown") == ("PDF BODY TEXT", "")


def test_pdf_detected_by_url_extension(monkeypatch):
    monkeypatch.setattr(web_fetch, "_extract_pdf", lambda raw: "PDF BODY TEXT")
    f = _fetched(
        monkeypatch,
        content=b"no-magic-here",
        content_type="application/octet-stream",
        url="https://example.com/report.pdf?dl=1",
    )
    assert web_fetch._extract_content(f, "markdown") == ("PDF BODY TEXT", "")


def test_plaintext_passthrough(monkeypatch):
    f = _fetched(monkeypatch, content=b"just some plain text", content_type="text/plain")
    assert web_fetch._extract_content(f, "text") == ("just some plain text", "")


# --- content-type handling: casing, structural acceptance, sniffing ------------
# Regression family for the 2026-09-01 bug: _fetch_one copied requests' headers
# verbatim, so the server's "Content-Type" never matched the lowercase lookups
# downstream. Every content type read as absent, which refused feeds, JSON and
# plain text as 'unknown' and silently killed title extraction on every page.


def test_fetch_one_lowercases_header_keys(monkeypatch):
    """B17: the normalization itself. requests preserves the server's casing."""
    f = _fetched(monkeypatch, content=b"hi", content_type="text/plain")
    assert "content-type" in f.headers
    assert f.headers.get("content-type") == "text/plain"


def test_server_cased_feed_type_is_read_not_refused(monkeypatch):
    """B1: the end-to-end shape from the bug report, a feed no longer refused.

    Deliberately NOT the guard for the casing fix: the body sniff would rescue
    this even with the header bug restored (verified by mutation). Its job is to
    pin the reported symptom. `test_declared_type_beats_the_body_sniff` and
    `test_fetch_one_lowercases_header_keys` are what actually hold the fix.
    """
    f = _fetched(monkeypatch, content=_RSS_FEED, content_type="application/rss+xml")
    body, _title = web_fetch._extract_content(f, "markdown")
    assert "First Post About Agents" in body


def test_bogus_charset_does_not_raise_out_of_the_tool(monkeypatch):
    """The tool's contract is that failures are RETURNED, never raised. requests
    copies the header charset verbatim, so a server sending `charset=foobar`
    hands the decoder an encoding Python does not know. This branch only became
    reachable once content types started being read correctly."""
    _patch_fetch(
        monkeypatch,
        FakeResponse(content=b'{"ok": true}', content_type="application/json; charset=foobar"),
    )
    out = web_fetch.fetch_url_nymeria.func(url="https://example.com/data.json")
    assert '{"ok": true}' in out          # decoded via the utf-8 fallback


def test_declared_charset_is_honoured_by_the_binary_sniff(monkeypatch):
    """A legacy-encoded document served under a type that says nothing useful is
    text, not binary. Judging it as UTF-8 while the server declared latin-1
    turned readable pages into a binary refusal."""
    latin1 = "Café über schön naïve résumé. ".encode("latin-1") * 4
    f = _fetched(
        monkeypatch, content=latin1, content_type="application/octet-stream; charset=iso-8859-1"
    )
    body, _title = web_fetch._extract_content(f, "text")
    assert "Café über schön" in body


def test_nbsp_and_unicode_whitespace_are_not_binary(monkeypatch):
    """NBSP is not `isprintable()`, and real prose is full of it."""
    prose = ("Real prose with non-breaking spaces and — dashes. " * 20).encode()
    f = _fetched(monkeypatch, content=prose, content_type="application/octet-stream")
    body, _title = web_fetch._extract_content(f, "text")
    assert "Real prose" in body


def test_empty_body_is_reported_not_rendered_as_a_blank_page(monkeypatch):
    _patch_fetch(monkeypatch, FakeResponse(content=b"", content_type="text/html"))
    out = web_fetch.fetch_url_nymeria.func(url="https://example.com/nothing")
    assert out.startswith("[Error]: Empty response body")


def test_body_beginning_with_the_error_prefix_is_not_mistaken_for_a_failure(monkeypatch):
    """Extraction failures are signalled by exception, not by a string prefix.
    Now that raw bodies are returned for many more content types, a document
    that happens to start with the sentinel must survive as content."""
    _patch_fetch(
        monkeypatch,
        FakeResponse(content=b"[Error]: this is the log file's own text", content_type="text/plain"),
    )
    out = web_fetch.fetch_url_nymeria.func(url="https://example.com/app.log")
    assert out.startswith("# ") or out.startswith("Source:")   # rendered with a header
    assert "this is the log file's own text" in out


def test_binary_error_redacts_credentials_in_the_url(monkeypatch):
    """Every other error path redacts userinfo; this one used to interpolate the
    URL raw, dropping user:pass into the agent transcript and the logs."""
    f = _fetched(
        monkeypatch,
        content=b"\x00\x01binary\x00",
        content_type="application/octet-stream",
        url="https://user:hunter2@example.com/blob",
    )
    with pytest.raises(web_fetch._ExtractionError) as excinfo:
        web_fetch._extract_content(f, "markdown")
    assert "hunter2" not in str(excinfo.value)
    assert "example.com/blob" in str(excinfo.value)


def test_declared_type_beats_the_body_sniff(monkeypatch):
    """B1, with teeth. The sniffing fallbacks are good enough to rescue a feed or
    a page even when the declared type reads as absent, which means most tests
    here cannot tell the casing fix from the fallback. This one can: a text/plain
    document that happens to CONTAIN html must come back verbatim, which only
    happens if the declared type is genuinely read. If the header lookup ever
    misses again, this body gets run through the HTML extractor instead."""
    source_listing = b"<html><head><title>Not a page</title></head><body><p>x</p></body></html>"
    f = _fetched(monkeypatch, content=source_listing, content_type="text/plain")
    body, title = web_fetch._extract_content(f, "markdown")
    assert body == source_listing.decode()   # verbatim, not extracted
    assert title == ""                       # and not treated as a titled page


def test_html_page_renders_its_title(monkeypatch):
    """B2: the second symptom. No fetch has ever emitted its `# title` header."""
    _patch_fetch(monkeypatch, FakeResponse(content=_ARTICLE_HTML))
    out = web_fetch.fetch_url_nymeria.func(url="https://example.com/page")
    assert out.splitlines()[0] == "# Real & Title"


def test_textual_types_accepted(monkeypatch):
    """B3/B4/B5: acceptance is structural, not an allowlist of exact spellings.

    The body embeds `<html` deliberately. A merely-readable body would be
    rescued by the sniffing fallback whether or not the type was recognized, so
    this would pass even with the structural rule deleted. Embedded markup
    discriminates: recognized-as-text returns it verbatim, while falling through
    to the sniff would hand it to the HTML extractor and lose the rest.
    """
    body_bytes = b'{"note": "<html><body>markup inside a data payload</body></html>", "n": 1}'
    for content_type in (
        "text/plain",
        "text/csv",
        "text/markdown",
        "text/xml",
        "application/xml",
        "application/json",
        "application/javascript",
        "application/ld+json",          # RFC 6839 structured suffix
        "application/vnd.api+json",     # vendor spelling nobody enumerates
        "application/x-unknown+xml",
    ):
        f = _fetched(monkeypatch, content=body_bytes, content_type=content_type)
        body, _title = web_fetch._extract_content(f, "text")
        assert body == body_bytes.decode(), f"{content_type} was not read as text"


def test_unknown_type_with_text_body_falls_back_instead_of_refusing(monkeypatch):
    """B6: an unrecognized type earns a look at the body, not a dead end."""
    for content_type in ("application/octet-stream", "", "application/x-yaml"):
        f = _fetched(monkeypatch, content=b"key: value\nother: thing\n", content_type=content_type)
        body, _title = web_fetch._extract_content(f, "text")
        assert "key: value" in body, f"{content_type!r} refused a readable body"


def test_binary_body_returns_an_honest_error(monkeypatch):
    """B7: real binary still refuses, naming the type and size, with no mojibake."""
    png = b"\x89PNG\r\n\x1a\n" + bytes(range(256)) * 4
    f = _fetched(monkeypatch, content=png, content_type="application/octet-stream")
    with pytest.raises(web_fetch._ExtractionError) as excinfo:
        web_fetch._extract_content(f, "markdown")
    assert "Binary content" in str(excinfo.value)
    assert "application/octet-stream" in str(excinfo.value) and str(len(png)) in str(excinfo.value)

    # A declared media family refuses without needing the sniff at all.
    f2 = _fetched(monkeypatch, content=b"whatever", content_type="image/png")
    with pytest.raises(web_fetch._ExtractionError):
        web_fetch._extract_content(f2, "markdown")

    # And the tool still RETURNS that failure rather than raising it.
    _patch_fetch(monkeypatch, FakeResponse(content=png, content_type="application/octet-stream"))
    assert web_fetch.fetch_url_nymeria.func(url="https://ex.com/b").startswith(
        "[Error]: Binary content"
    )


def test_html_served_as_octet_stream_still_extracts(monkeypatch):
    """B9: the HTML body sniff used to fire only when the type was fully absent."""
    f = _fetched(monkeypatch, content=_ARTICLE_HTML, content_type="application/octet-stream")
    body, title = web_fetch._extract_content(f, "markdown")
    assert "The Heading" in body
    assert title == "Real & Title"


# --- feed rendering -----------------------------------------------------------

_RSS_FEED = b"""<?xml version="1.0" encoding="utf-8"?>
<rss version="2.0">
  <channel>
    <title>Example Feed Title</title>
    <link>https://example.com/</link>
    <item>
      <title>First Post About Agents</title>
      <link>https://example.com/1</link>
      <author>alice@example.com (Alice)</author>
      <pubDate>Mon, 01 Sep 2026 01:01:08 +0000</pubDate>
      <description>&lt;p&gt;Body &lt;b&gt;with markup&lt;/b&gt; inside.&lt;/p&gt;</description>
    </item>
    <item>
      <title>Second Post</title>
      <link>https://example.com/2</link>
      <pubDate>Mon, 01 Sep 2026 02:00:00 +0000</pubDate>
      <description>Plain summary.</description>
    </item>
  </channel>
</rss>
"""

_ATOM_FEED = b"""<?xml version="1.0" encoding="utf-8"?>
<feed xmlns="http://www.w3.org/2005/Atom">
  <title>Example Atom Title</title>
  <entry>
    <title>Atom Entry One</title>
    <link href="https://example.com/a1"/>
    <author><name>Bob</name></author>
    <published>2026-08-31T18:32:51-04:00</published>
    <summary>Atom summary text.</summary>
  </entry>
</feed>
"""

_EMPTY_RSS = b"""<?xml version="1.0" encoding="utf-8"?>
<rss version="2.0"><channel><title>Quiet Feed</title></channel></rss>
"""

_SITEMAP_XML = b"""<?xml version="1.0" encoding="utf-8"?>
<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">
  <url><loc>https://example.com/a</loc></url>
  <url><loc>https://example.com/b</loc></url>
</urlset>
"""


def test_rss_served_as_generic_xml_renders_items(monkeypatch):
    """B10: hnrss serves RSS as application/xml, so the declared type is not enough."""
    f = _fetched(monkeypatch, content=_RSS_FEED, content_type="application/xml")
    body, title = web_fetch._extract_content(f, "markdown")
    assert title == "Example Feed Title"                       # B12
    assert "1. First Post About Agents" in body
    assert "2. Second Post" in body
    assert "https://example.com/1" in body
    assert "Mon, 01 Sep 2026 01:01:08 +0000" in body
    assert "Alice" in body
    assert "[2 items;" in body                                 # B12
    assert "<item>" not in body and "<rss" not in body          # rendered, not dumped


def test_atom_feed_renders_items(monkeypatch):
    """B11."""
    f = _fetched(monkeypatch, content=_ATOM_FEED, content_type="application/atom+xml")
    body, title = web_fetch._extract_content(f, "markdown")
    assert title == "Example Atom Title"
    assert "1. Atom Entry One" in body
    assert "https://example.com/a1" in body
    assert "Bob" in body
    assert "Atom summary text." in body
    assert "[1 item;" in body


def test_feed_fields_match_what_an_rss_trigger_receives(monkeypatch):
    """The design property: the preview shows the SAME six fields the rss trigger
    source emits per event, parsed by the same library, so conditions written
    against a preview match at fire time. Compare against the real source."""
    from nymeria.triggers.sources.rss_source import RSSSource

    f = _fetched(monkeypatch, content=_RSS_FEED, content_type="application/rss+xml")
    body, title = web_fetch._extract_content(f, "markdown")

    # Pinned against the source's own declaration: if a field is added there, this
    # fails until the preview carries it too, rather than drifting apart quietly.
    assert set(RSSSource.template_variables) == {
        "title", "link", "summary", "author", "published", "feed_title"
    }
    assert "First Post About Agents" in body          # title
    assert "https://example.com/1" in body            # link
    assert "Body with markup inside." in body         # summary (HTML stripped)
    assert "Alice" in body                            # author
    assert "Mon, 01 Sep 2026 01:01:08 +0000" in body  # published
    assert title == "Example Feed Title"              # feed_title


def test_feed_text_mode_returns_raw_xml(monkeypatch):
    """B13: the escape hatch."""
    f = _fetched(monkeypatch, content=_RSS_FEED, content_type="application/rss+xml")
    body, _title = web_fetch._extract_content(f, "text")
    assert body.startswith("<?xml")
    assert "<item>" in body
    assert "1. First Post" not in body


def test_feed_summary_is_html_stripped_and_capped(monkeypatch):
    """B14: feed descriptions are routinely HTML, and some are enormous."""
    long_tail = "word " * 200
    feed = _RSS_FEED.replace(
        b"&lt;p&gt;Body &lt;b&gt;with markup&lt;/b&gt; inside.&lt;/p&gt;",
        ("&lt;p&gt;Lead sentence. " + long_tail + "&lt;/p&gt;").encode(),
    )
    f = _fetched(monkeypatch, content=feed, content_type="application/rss+xml")
    body, _title = web_fetch._extract_content(f, "markdown")
    summary_line = next(ln for ln in body.splitlines() if "summary:" in ln)
    assert "<p>" not in summary_line and "&lt;" not in summary_line
    assert "Lead sentence." in summary_line
    assert summary_line.rstrip().endswith("...")
    assert len(summary_line) < 400  # capped, not the full 1000+ char description


def test_empty_but_valid_feed_says_so(monkeypatch):
    """B18: the URL from the bug report currently returns zero items. Dumping raw
    XML there hides the one fact a trigger author needs, so an empty feed is
    reported as empty. The discriminator is feedparser's detected FORMAT."""
    f = _fetched(monkeypatch, content=_EMPTY_RSS, content_type="application/xml")
    body, title = web_fetch._extract_content(f, "markdown")
    assert "no entries" in body
    assert "rss20" in body
    assert title == "Quiet Feed"
    assert "<rss" not in body


def test_non_feed_xml_is_not_rendered_as_a_feed(monkeypatch):
    """B15: a sitemap is XML with no feed root, so it falls through to raw text.

    This is held by the root-element sniff, not the format gate (a sitemap never
    reaches the renderer at all); `test_feed_type_over_a_non_feed_body_shows_the_body`
    is what covers the gate.
    """
    f = _fetched(monkeypatch, content=_SITEMAP_XML, content_type="application/xml")
    body, title = web_fetch._extract_content(f, "markdown")
    assert "<urlset" in body            # raw text, unrendered
    assert "<loc>" in body
    assert "no entries" not in body     # and NOT mistaken for an empty feed
    assert "returns the raw feed XML" not in body
    assert title == ""


def test_feed_type_over_a_non_feed_body_shows_the_body(monkeypatch):
    """The format gate, tested where it actually bites. A misconfigured server can
    send Content-Type: application/rss+xml for an HTML error page. Reporting that
    as "a feed with no entries" would send someone debugging a silent trigger off
    hunting an empty feed, when the real answer is on the page: it 404s.

    The declared type has disproven itself here, so the body decides: this comes
    back EXTRACTED, not as a dump of raw HTML source.
    """
    error_page = (
        b"<html><head><title>404 Not Found</title></head><body><h1>Not Found</h1>"
        b"<p>The feed you requested does not exist on this server, and has not for "
        b"some time. Please check the address you used and try again later.</p>"
        b"</body></html>"
    )
    f = _fetched(monkeypatch, content=error_page, content_type="application/rss+xml")
    body, title = web_fetch._extract_content(f, "markdown")
    assert "no entries" not in body
    assert "does not exist on this server" in body
    assert "<html>" not in body and "<h1>" not in body   # extracted, not dumped
    assert title == "404 Not Found"


def test_feed_behind_a_stylesheet_processing_instruction_is_detected(monkeypatch):
    """Feeds routinely carry an <?xml-stylesheet?> PI (often a long href) before
    the root element. Sniffing too small a head misses the root and dumps the
    markup instead, on exactly the generic-content-type feeds the sniff exists
    for."""
    pi = b'<?xml-stylesheet type="text/xsl" href="/static/' + b"p" * 1200 + b'.xsl"?>\n'
    feed = _RSS_FEED.replace(b"<rss version", pi + b"<rss version", 1)
    f = _fetched(monkeypatch, content=feed, content_type="application/xml")
    body, title = web_fetch._extract_content(f, "markdown")
    assert "1. First Post About Agents" in body
    assert title == "Example Feed Title"


def test_feed_renders_end_to_end_with_its_title_as_the_header(monkeypatch):
    """B12 through the whole tool, not just _extract_content: the feed's title
    has to survive _fetch_and_render's header assembly to be seen at all."""
    _patch_fetch(monkeypatch, FakeResponse(
        content=_RSS_FEED, content_type="application/xml", url="https://example.com/feed.xml"))
    out = web_fetch.fetch_url_nymeria.func(url="https://example.com/feed.xml")
    lines = out.splitlines()
    assert lines[0] == "# Example Feed Title"
    assert lines[1] == "Source: https://example.com/feed.xml"
    assert "1. First Post About Agents" in out
    assert "[2 items;" in out


def test_feed_parsing_resolves_no_external_entities(monkeypatch, tmp_path):
    """B16: the body is attacker-controlled and the module's premise is SSRF
    safety, so a feed must never become a local-file read primitive."""
    canary = tmp_path / "canary.txt"
    canary.write_text("SECRET_CANARY_VALUE")
    xxe = (
        '<?xml version="1.0"?>\n'
        f'<!DOCTYPE rss [<!ENTITY xxe SYSTEM "file://{canary}">]>\n'
        '<rss version="2.0"><channel><title>T</title>'
        '<item><title>&xxe;</title><link>https://example.com/1</link></item>'
        '</channel></rss>'
    ).encode()
    f = _fetched(monkeypatch, content=xxe, content_type="application/rss+xml")
    body, _title = web_fetch._extract_content(f, "markdown")
    assert "SECRET_CANARY_VALUE" not in body
    assert str(canary) not in body


def test_atom_without_published_shows_no_date(monkeypatch):
    """Deliberate strict fidelity: feedparser does not alias <updated> onto
    `published`, and neither does the rss trigger source, so the preview shows
    the same blank a trigger's {published} variable would get rather than a
    friendlier date the trigger will not have. Filed as a trigger-side gap."""
    atom = _ATOM_FEED.replace(b"<published>", b"<updated>").replace(b"</published>", b"</updated>")
    f = _fetched(monkeypatch, content=atom, content_type="application/atom+xml")
    body, _title = web_fetch._extract_content(f, "markdown")
    assert "Atom Entry One" in body
    assert "published:" not in body
    assert "2026-08-31" not in body


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
