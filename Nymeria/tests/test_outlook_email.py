"""Unit tests for Outlook email helper functions.

Covers the small extractions from optimization slice 16: the KQL suffix
builder (F5), the hoisted recipient parser (F7), the shared inline-image skip
predicate (F9), and the symmetric body-truncation marker (F14).
"""

from nymeria.tools import outlook_email as oe


def test_build_kql_suffix_combines_structured_filters():
    suffix = oe._build_kql_suffix(
        sender="a@x.com", recipient="b@y.com", subject="Hello", has_attachments=True
    )
    assert suffix == "from:a@x.com to:b@y.com subject:Hello hasattachment:true"


def test_build_kql_suffix_empty_when_no_filters():
    assert oe._build_kql_suffix() == ""


def test_build_kql_suffix_partial_filters():
    assert oe._build_kql_suffix(sender="a@x.com") == "from:a@x.com"
    assert oe._build_kql_suffix(has_attachments=True) == "hasattachment:true"


def test_parse_recipients_splits_and_wraps():
    assert oe._parse_recipients("a@x.com, b@y.com") == [
        {"emailAddress": {"address": "a@x.com"}},
        {"emailAddress": {"address": "b@y.com"}},
    ]


def test_parse_recipients_ignores_blanks_and_empty():
    assert oe._parse_recipients(" , a@x.com ,, ") == [{"emailAddress": {"address": "a@x.com"}}]
    assert oe._parse_recipients("") == []


def test_is_inline_signature_image_predicate():
    # Small inline image -> skipped.
    assert oe._is_inline_signature_image(is_inline=True, mime="image/png", size=1000) is True
    # Above the byte threshold -> kept.
    assert oe._is_inline_signature_image(is_inline=True, mime="image/png", size=60000) is False
    # Not inline -> kept.
    assert oe._is_inline_signature_image(is_inline=False, mime="image/png", size=1000) is False
    # Not an image -> kept.
    assert oe._is_inline_signature_image(is_inline=True, mime="application/pdf", size=1000) is False


def test_truncate_body_returns_short_text_unchanged():
    short = "hello world"
    assert oe._truncate_body(short) is short


def test_truncate_body_marks_when_trimmed():
    long = "x" * (oe._BODY_PREVIEW_CHARS + 500)
    out = oe._truncate_body(long)
    assert out.startswith("x" * oe._BODY_PREVIEW_CHARS)
    assert out.endswith("...[truncated 500 chars]")


def _email(content_type, content):
    return {
        "subject": "Subject",
        "from": {"emailAddress": {"address": "a@x.com", "name": "Alice"}},
        "receivedDateTime": "2026-06-21T10:00:00Z",
        "toRecipients": [{"emailAddress": {"address": "b@y.com"}}],
        "body": {"contentType": content_type, "content": content},
    }


def test_format_single_email_truncates_long_plain_text():
    # Plain-text bodies were previously not truncated at all (F14).
    long_body = "y" * (oe._BODY_PREVIEW_CHARS + 100)
    out = oe._format_single_email(_email("text", long_body))
    assert "...[truncated 100 chars]" in out


def test_format_single_email_caps_html_after_conversion():
    long_html = "<p>" + ("z" * (oe._BODY_PREVIEW_CHARS + 50)) + "</p>"
    out = oe._format_single_email(_email("html", long_html))
    assert "...[truncated" in out


def test_format_single_email_keeps_short_body_intact():
    out = oe._format_single_email(_email("text", "just a short note"))
    assert "just a short note" in out
    assert "[truncated" not in out
