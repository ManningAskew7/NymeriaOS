"""Web page-fetch tools for Nymeria.

`fetch_url_nymeria` is the first member of an opt-in fetch family that mirrors the
web_search_* family: a free, in-process, SSRF-safe fetcher that pulls a URL,
extracts the readable content as markdown or text, and optionally hands it to a
secondary model that reads the page and returns only what an `extraction_prompt`
asks for. Hosted members (fetch_url_firecrawl, ...) land later as separate
opt-in tools for the cases free extraction cannot match.

Content-kind dispatch lives in `_extract_content` (PDF, HTML, feed, text, binary)
and is deliberately body-first: a declared media family is the only hard refusal,
everything else earns a sniff. RSS/Atom bodies render through feedparser into the
same six fields `triggers/sources/rss_source.py` emits per trigger event, so a
feed previewed with this tool shows what a trigger watching it will receive. That
parity is enforced by SHARED CODE (`core/feed_fields.py`), not by two readers
agreeing: it used to be the latter, and the two quietly disagreed about dates on
any Atom feed (#309).
feedparser was measured safe on hostile bodies before being used here (no
external-entity resolution, entity-expansion bombs bounce); the dispatch order
and that measurement are documented in
`docs/agent-systems/tools.md` under `fetch_url_nymeria`.

The fetch is gated by the shared HTTP egress policy (core/http_policy.py): the
agent supplies an arbitrary URL, so every request and redirect hop is validated
against the SSRF rules (no loopback / private / link-local / metadata targets)
with DNS pinning. This is the same path rss_source and the credential probes use.
"""

from __future__ import annotations
from .registry import ToolGroup, register_tool_group

import hashlib
import html as html_mod
import io
import logging
import re
from typing import Annotated, Optional
from urllib.parse import urlparse

from langchain_core.runnables import RunnableConfig
from langchain_core.tools import InjectedToolArg, tool

from ..core.feed_fields import feed_entry_field, feed_entry_published
from ..core.http_policy import (
    HTTPPolicyRedirectLimit,
    HTTPPolicyViolation,
    requests_get_with_policy,
)
from .llm_extract import extraction_attribution, run_extraction
from .web_batch import run_batched

logger = logging.getLogger(__name__)

_MAX_BATCH_URLS = 10
_MAX_FETCH_BYTES = 10 * 1024 * 1024  # 10 MB hard cap, enforced while streaming
_THIN_CONTENT_CHARS = 200  # below this, try the readability fallback
_MIN_MAX_LENGTH = 500
_MAX_MAX_LENGTH = 50_000
_DEFAULT_MAX_LENGTH = 8_000
_USER_AGENT = "Nymeria/1.0 (web fetch; autonomous personal assistant)"

_EXTRACT_FORMATS = {"markdown", "text"}

# Content classification. Acceptance is STRUCTURAL rather than an allowlist of
# exact types: servers invent types freely, and a feed refused for its spelling
# is worse than a body sniff. Declared media families are the only hard "no".
_TEXT_TYPES = {"application/xml", "application/json", "application/javascript"}
_BINARY_FAMILIES = ("image/", "audio/", "video/", "font/", "model/")
_FEED_ROOT_RE = re.compile(rb"<\s*(rss|feed|rdf:RDF)[\s>]", re.IGNORECASE)
# Generous enough to clear an XML declaration plus a stylesheet processing
# instruction or licence comment, which routinely precede a feed's root element.
_FEED_HEAD_BYTES = 4096
_FEED_SUMMARY_CHARS = 300
_TEXT_SNIFF_BYTES = 4096
_TEXT_PRINTABLE_RATIO = 0.90


class _ExtractionError(Exception):
    """An extraction failure, rendered as the tool's ``[Error]:`` string.

    Signalled by exception rather than by returning a magic string prefix: the
    tool now returns raw document bodies for many more content types, and a
    fetched document that happens to BEGIN with "[Error]:" would otherwise be
    mistaken for a failure. Caught at the `_fetch_and_render` boundary so the
    tool keeps its contract of returning failures rather than raising them.
    """


# --- fetch --------------------------------------------------------------------


def _redact_url(url: str) -> str:
    """Strip userinfo (user:pass@) so credentials never reach the agent or logs."""
    try:
        parsed = urlparse(url)
        if parsed.username or parsed.password:
            netloc = parsed.hostname or ""
            if parsed.port:
                netloc = f"{netloc}:{parsed.port}"
            parsed = parsed._replace(netloc=netloc)
        return parsed.geturl()
    except Exception:  # noqa: BLE001
        return "the requested URL"


class _Fetched:
    """Lightweight, client-agnostic holder for a fetched (already size-capped) body."""

    __slots__ = ("headers", "content", "url", "encoding")

    def __init__(self, headers: dict, content: bytes, url: str, encoding):
        self.headers = headers
        self.content = content
        self.url = url
        self.encoding = encoding


def _fetch_one(url: str, *, timeout: float = 25.0):
    """Fetch a URL through the SSRF egress policy, streaming with a hard byte cap.

    Returns ``(fetched, error, final_url)``. On failure ``fetched`` is None and
    ``error`` is a ``[Error]: ...`` string; on success ``error`` is None.
    """
    import requests

    response = None
    try:
        response, _chain, _decision = requests_get_with_policy(
            url,
            headers={
                "User-Agent": _USER_AGENT,
                "Accept": "text/html,application/xhtml+xml,application/pdf,text/plain,*/*",
                "Accept-Language": "en",
            },
            timeout=timeout,        # applied to both connect and read phases
            stream=True,            # defer the body so we can cap it before reading
            follow_redirects=True,
        )
        final_url = str(response.url)
        try:
            response.raise_for_status()
        except requests.exceptions.HTTPError:
            return None, f"[Error]: HTTP {response.status_code} for {_redact_url(url)}", final_url

        # Reject early when the server honestly advertises an over-cap body.
        declared = response.headers.get("content-length")
        if declared and declared.isdigit() and int(declared) > _MAX_FETCH_BYTES:
            return None, (
                f"[Error]: Response too large ({int(declared)} bytes; limit "
                f"{_MAX_FETCH_BYTES}). Try a more specific URL."
            ), final_url

        # Stream and enforce the cap even when Content-Length is absent or lies.
        chunks: list[bytes] = []
        total = 0
        for chunk in response.iter_content(chunk_size=65536):
            if not chunk:
                continue
            total += len(chunk)
            if total > _MAX_FETCH_BYTES:
                return None, (
                    f"[Error]: Response exceeded the {_MAX_FETCH_BYTES} byte limit. "
                    f"Try a more specific URL."
                ), final_url
            chunks.append(chunk)

        # requests defaults charset-less text/* responses to ISO-8859-1 (RFC 2616),
        # which mojibakes UTF-8 pages that declare their charset only via <meta>.
        # Honor an explicit header charset; otherwise default to utf-8 (as the
        # prior httpx path did) and let errors="replace" cover the rare exception.
        ctype = response.headers.get("content-type", "")
        encoding = response.encoding if "charset=" in ctype.lower() else None
        # Lowercase the header keys: copying requests' CaseInsensitiveDict into a
        # plain dict PRESERVES the server's casing, so downstream lookups of
        # "content-type" silently miss (httpx, which this module first used,
        # normalized instead). This is the one place a client response becomes a
        # _Fetched, so it is the only place that needs to know.
        fetched = _Fetched(
            headers={k.lower(): v for k, v in response.headers.items()},
            content=b"".join(chunks),
            url=final_url,
            encoding=encoding,
        )
        return fetched, None, final_url
    except (HTTPPolicyViolation, HTTPPolicyRedirectLimit) as e:
        return None, f"[Error]: Blocked by egress policy: {e}", url
    except requests.exceptions.Timeout:
        return None, f"[Error]: Timed out fetching {_redact_url(url)}", url
    except Exception as e:  # noqa: BLE001
        logger.error("fetch_url_nymeria fetch failed: %s", type(e).__name__, exc_info=True)
        return None, f"[Error]: Fetch failed for {_redact_url(url)}: {type(e).__name__}", url
    finally:
        if response is not None:
            try:
                response.close()
            except Exception:  # noqa: BLE001
                pass


# --- extraction ---------------------------------------------------------------


def _strip_tags(html: str) -> str:
    """Minimal HTML to plain text for the readability text-mode fallback."""
    text = re.sub(r"(?is)<(script|style|noscript|head)[^>]*>.*?</\1>", " ", html)
    text = re.sub(r"(?i)<br\s*/?>", "\n", text)
    text = re.sub(r"(?i)</(p|div|li|tr|h[1-6]|blockquote)>", "\n", text)
    text = re.sub(r"<[^>]+>", "", text)
    text = html_mod.unescape(text)
    lines = [line.strip() for line in text.splitlines()]
    return "\n".join(line for line in lines if line)


def _extract_title(html: str) -> str:
    match = re.search(r"<title[^>]*>(.*?)</title>", html, re.IGNORECASE | re.DOTALL)
    if not match:
        return ""
    return html_mod.unescape(re.sub(r"\s+", " ", match.group(1))).strip()[:200]


def _extract_pdf(raw: bytes) -> str:
    from pypdf import PdfReader

    reader = PdfReader(io.BytesIO(raw))
    chunks: list[str] = []
    for i, page in enumerate(reader.pages):
        try:
            text = page.extract_text() or ""
        except Exception as e:  # noqa: BLE001 - one bad page should not fail the doc
            logger.debug("PDF page %d extract failed: %s", i, e)
            text = ""
        chunks.append(f"--- Page {i + 1} ---\n{text.strip()}\n")
    return "\n".join(chunks).strip()


def _extract_html(html: str, extract: str) -> str:
    """Trafilatura primary, readability-lxml fallback. Returns "" if nothing."""
    import trafilatura

    fmt = "markdown" if extract == "markdown" else "txt"
    primary = ""
    try:
        out = trafilatura.extract(
            html,
            output_format=fmt,
            favor_recall=True,
            include_links=(extract == "markdown"),
            include_tables=True,
        )
        primary = (out or "").strip()
    except Exception as e:  # noqa: BLE001
        logger.debug("trafilatura extract failed: %s", e)

    if len(primary) >= _THIN_CONTENT_CHARS:
        return primary

    # Fallback: readability isolates the main article subtree.
    try:
        from readability import Document

        summary_html = Document(html).summary(html_partial=True)
        if extract == "markdown":
            from markdownify import markdownify as _md

            fallback = _md(summary_html, heading_style="ATX").strip()
        else:
            fallback = _strip_tags(summary_html).strip()
        if fallback:
            return fallback
    except Exception as e:  # noqa: BLE001
        logger.debug("readability fallback failed: %s", e)

    # Last resort: return the thin primary result rather than nothing.
    return primary


def _is_textual_type(content_type: str) -> bool:
    """True for text/*, the RFC 6839 structured suffixes, and known text types.

    The ``+xml``/``+json`` suffixes are what make this structural: they cover
    application/rss+xml, application/atom+xml, application/ld+json and every
    vendor spelling nobody thought to enumerate.
    """
    return (
        content_type.startswith("text/")
        or content_type.endswith(("+xml", "+json"))
        or content_type in _TEXT_TYPES
    )


def _decode(raw: bytes, encoding=None) -> str:
    """Decode a body, tolerating a charset label Python does not know.

    requests copies the header's charset VERBATIM, so a server sending
    `charset=foobar` hands us an unusable encoding name. Without this guard the
    decode raises LookupError straight out of the tool, breaking its contract of
    returning failures rather than raising them.
    """
    try:
        return raw.decode(encoding or "utf-8", errors="replace")
    except (LookupError, TypeError):
        return raw.decode("utf-8", errors="replace")


def _looks_like_text(raw: bytes, encoding=None) -> bool:
    """Sniff a body whose declared type says nothing useful.

    Text when it carries no NULs and decodes as mostly printable. The server's
    declared charset is honoured when it gave one, so a legacy-encoded page
    (latin-1, Shift-JIS) is not written off as binary just for failing to be
    UTF-8. Replacement characters count AGAINST the body, so binary holding a
    little ASCII does not pass, while one from a multibyte character straddling
    the sample edge is lost in the ratio. Unicode whitespace counts as printable
    (NBSP and friends are not `isprintable()`, and real prose is full of them).

    Residual: an undeclared legacy encoding still reads as binary. Rare enough
    to prefer over guessing, since guessing risks dumping real binary as text.
    """
    sample = raw[:_TEXT_SNIFF_BYTES]
    if not sample:
        return True  # an empty body is not binary
    if b"\x00" in sample:
        return False
    decoded = _decode(sample, encoding)
    printable = sum(1 for ch in decoded if ch.isprintable() or ch.isspace())
    undecodable = decoded.count("�")
    return (printable - undecodable) / len(decoded) >= _TEXT_PRINTABLE_RATIO


def _looks_like_feed(raw: bytes) -> bool:
    """True when the document's ROOT element is a feed root (rss/feed/rdf:RDF).

    Scans only the head, and only ahead of any ``<html``, so an HTML page that
    merely links or discusses a feed cannot match.
    """
    head = raw[:_FEED_HEAD_BYTES]
    match = _FEED_ROOT_RE.search(head)
    if not match:
        return False
    html_at = head.lower().find(b"<html")
    return html_at == -1 or html_at > match.start()


def _entry_field(entry, key: str) -> str:
    """Read one feedparser entry field, through the shared feed reader.

    Shared with ``triggers/sources/rss_source.py`` via
    ``core/feed_fields.py`` so the preview and the trigger cannot drift: the
    parity this module's docstring promises is now a property of the code
    rather than an agreement between two independent implementations (#309).
    """
    return feed_entry_field(entry, key)


def _collapse(value: str) -> str:
    return re.sub(r"\s+", " ", value).strip()


def _render_feed(raw: bytes) -> tuple[str, str]:
    """Render an RSS/Atom body as a compact item list. Returns ``(body, feed_title)``.

    Emits the same six fields ``triggers/sources/rss_source.py`` puts in a trigger
    event (title, link, summary, author, published, feed_title) via the same
    parser AND the same field reader (``core/feed_fields.py``), so a feed
    previewed here shows exactly what an ``rss`` trigger's conditions will match
    against, rather than something merely similar. ``published`` therefore falls
    back to Atom's ``<updated>`` on both sides (#309); before that was shared,
    each side read the field its own way and an Atom feed previewed blank while
    the trigger delivered blank for a different reason.

    Returns ``("", "")`` when the body is not a feed at all, so the caller falls
    through to the raw-text path. The test is feedparser's ``version`` (the
    detected feed FORMAT), never the entry count: a sitemap and arbitrary XML
    both report ``""``, while a valid but currently empty feed still reports
    ``rss20``/``atom10`` and is worth saying so about. An empty feed is a real
    answer for someone about to point a trigger at it, and burying that under
    raw markup is how a trigger ends up watching a feed that never fires.

    feedparser is handed the raw bytes so the feed's own XML declaration governs
    its decoding.
    """
    import feedparser

    parsed = feedparser.parse(raw)
    version = str(getattr(parsed, "version", "") or "")
    if not version:
        return "", ""

    meta = parsed.feed if isinstance(parsed.feed, dict) else {}
    feed_title = _collapse(str(meta.get("title", "") or ""))
    entries = getattr(parsed, "entries", None) or []
    if not entries:
        return (
            f"[This {version} feed parsed correctly but currently has no entries. "
            f'extract="text" returns the raw feed XML]'
        ), feed_title

    lines: list[str] = []
    for i, entry in enumerate(entries, 1):
        lines.append(f"{i}. {_collapse(_entry_field(entry, 'title')) or '(untitled)'}")
        for key in ("link", "published", "author"):
            field = (
                feed_entry_published(entry)
                if key == "published"
                else _entry_field(entry, key)
            )
            value = _collapse(field)
            if value:
                lines.append(f"   {key + ':':<11}{value}")
        # Feed summaries are routinely HTML; strip it so the preview stays readable.
        summary = _collapse(_strip_tags(_entry_field(entry, "summary")))
        if len(summary) > _FEED_SUMMARY_CHARS:
            summary = summary[:_FEED_SUMMARY_CHARS].rstrip() + "..."
        if summary:
            lines.append(f"   {'summary:':<11}{summary}")
        lines.append("")

    count = len(entries)
    lines.append(
        f"[{count} item{'s' if count != 1 else ''}; "
        f'extract="text" returns the raw feed XML]'
    )
    return "\n".join(lines).strip(), feed_title


def _render_html(raw: bytes, response, extract: str) -> tuple[str, str]:
    """Extract an HTML body and its ``<title>``. Returns ``(body, title)``."""
    html_text = _decode(raw, response.encoding)
    body = _extract_html(html_text, extract)
    if not body:
        raise _ExtractionError(
            "[Error]: Could not extract readable content (the page may require "
            "JavaScript; try a hosted fetch provider)."
        )
    try:
        title = _extract_title(html_text)
    except Exception:  # noqa: BLE001 - a missing title must never fail the fetch
        title = ""
    return body, title


def _extract_content(response, extract: str) -> tuple[str, str]:
    """Dispatch on Content-Type, falling back to body sniffing.

    Returns ``(body, title)`` and raises `_ExtractionError` on failure. Header
    lookups here rely on the lowercased keys ``_fetch_one`` normalizes to.
    """
    content_type = (response.headers.get("content-type") or "").split(";")[0].strip().lower()
    raw = response.content[:_MAX_FETCH_BYTES]
    safe_url = _redact_url(str(response.url))
    if not raw:
        raise _ExtractionError(f"[Error]: Empty response body from {safe_url}.")

    # A declared media family is the only hard refusal; everything else earns a
    # look at the body rather than an "unsupported type" dead end.
    declared_binary = content_type.startswith(_BINARY_FAMILIES)
    sniffable = not declared_binary and not _is_textual_type(content_type)

    # Detect PDFs by content-type, magic bytes, or .pdf extension. Many servers
    # send PDFs as application/octet-stream or binary/octet-stream, so the
    # content-type alone is not reliable; the "%PDF-" header is definitive.
    url_path = str(response.url).split("?", 1)[0].lower()
    if "pdf" in content_type or b"%PDF-" in raw[:1024] or url_path.endswith(".pdf"):
        try:
            text = _extract_pdf(raw)
        except Exception as e:  # noqa: BLE001
            logger.error("PDF extraction failed: %s", e, exc_info=True)
            raise _ExtractionError(f"[Error]: Could not read PDF: {e}") from e
        if not text:
            raise _ExtractionError(
                "[Error]: PDF contained no extractable text (it may be scanned images)."
            )
        return text, ""

    # Declared HTML resolves before the feed check, so a real page is never
    # rendered as a feed; sniffed HTML resolves after it, because a feed is XML.
    if "html" in content_type:
        return _render_html(raw, response, extract)

    # Feeds. The declared type is not enough on its own: hnrss and many others
    # serve RSS as a generic application/xml, so anything not declared binary
    # also gets the root-element sniff.
    feed_claimed = "rss" in content_type or "atom" in content_type
    if feed_claimed or (not declared_binary and _looks_like_feed(raw)):
        if extract == "text":
            return _decode(raw, response.encoding).strip(), ""   # raw-feed escape hatch
        try:
            feed_body, feed_title = _render_feed(raw)
        except Exception as e:  # noqa: BLE001 - a parser failure is not fatal
            logger.debug("feed render failed for %s: %s", safe_url, e)
            feed_body, feed_title = "", ""
        if feed_body:
            return feed_body, feed_title
        # Not a feed after all. A type that CLAIMED to be one has just disproven
        # itself (a 404 page served as application/rss+xml is the common case),
        # so stop trusting it and let the body decide what this is.
        if feed_claimed:
            sniffable = True

    if sniffable and b"<html" in raw[:2000].lower():
        return _render_html(raw, response, extract)

    if _is_textual_type(content_type) or (sniffable and _looks_like_text(raw, response.encoding)):
        return _decode(raw, response.encoding).strip(), ""

    raise _ExtractionError(
        f"[Error]: Binary content ('{content_type or 'no content-type'}', "
        f"{len(raw)} bytes) at {safe_url}; nothing readable to extract."
    )


# --- rendering ----------------------------------------------------------------


def _format_header(title: str, requested_url: str, final_url: str) -> str:
    lines: list[str] = []
    if title:
        lines.append(f"# {title}")
    if final_url and final_url != requested_url:
        lines.append(f"Source: {final_url} (redirected from {requested_url})")
    else:
        lines.append(f"Source: {requested_url}")
    return "\n".join(lines)


def _spill_filename(url: str, extract: str) -> str:
    """Stable, filesystem-safe filename derived from the URL (re-fetch overwrites).

    Uses the hostname (not netloc) so any user:pass@ userinfo never lands in the
    filename, and a hash for uniqueness.
    """
    parsed = urlparse(url)
    host = (parsed.hostname or "page").lower()
    slug = re.sub(r"[^a-z0-9]+", "-", (host + parsed.path).lower()).strip("-")[:80] or "page"
    digest = hashlib.sha1(url.encode("utf-8", "replace")).hexdigest()[:8]
    ext = "md" if extract == "markdown" else "txt"
    return f"{slug}-{digest}.{ext}"


def _spill_to_file(body: str, url: str, extract: str, config) -> Optional[str]:
    """Write the full extracted content to the thread's fetch dir.

    Returns the absolute path, or None when there is no thread to key by or the
    write fails (best-effort: a spill failure must never fail the fetch).
    """
    from .utils import get_thread_id_or_none

    thread_id = get_thread_id_or_none(config)
    if not thread_id:
        return None
    try:
        from ..core.attachment_sandbox import get_thread_fetch_dir

        # Write the FULL extracted text (already bounded by the 10 MB fetch cap).
        # The agent pages through it with file_read or bash_execute (grep/sed).
        target = get_thread_fetch_dir(thread_id) / _spill_filename(url, extract)
        target.write_text(body, encoding="utf-8")
        try:
            target.chmod(0o600)
        except (PermissionError, OSError):
            pass  # best-effort tightening; the spill already succeeded
        return str(target)
    except Exception as e:  # noqa: BLE001 - spill is best-effort
        logger.debug("fetch spill failed for %s: %s", url, e)
        return None


def _fetch_and_render(
    url: str,
    *,
    extract: str,
    extraction_prompt: str,
    max_length: int,
    config=None,
) -> str:
    response, error, final_url = _fetch_one(url)
    if error is not None or response is None:
        return error or f"[Error]: Fetch failed for {url}"

    # _extract_content owns content-kind dispatch and produces the title with the
    # body, so there is exactly one place that decides what a response IS. This
    # is also the boundary where extraction failures become the tool's returned
    # [Error]: string, so nothing from extraction can escape as a raised error.
    try:
        body, title = _extract_content(response, extract)
    except _ExtractionError as e:
        return str(e)
    except Exception as e:  # noqa: BLE001 - the tool contract is to RETURN failures
        logger.error("content extraction failed for %s: %s", _redact_url(url), e, exc_info=True)
        return f"[Error]: Could not extract content from {_redact_url(url)}: {type(e).__name__}"

    if extraction_prompt.strip():
        extracted, model, cut = run_extraction(body, extraction_prompt)
        if extracted.startswith("[Error]:"):
            return extracted
        body = f"{extracted}\n\n{extraction_attribution(model, cut)}"
    elif len(body) > max_length:
        full_len = len(body)
        preview = body[:max_length].rstrip()
        spill_path = _spill_to_file(body, url, extract, config)
        if spill_path:
            body = (
                f"{preview}\n\n[Content truncated to {max_length} of {full_len} characters. "
                f"Full text saved to {spill_path}. Read it with file_read, or use "
                f"bash_execute (grep/sed/head) to search it or read specific sections.]"
            )
        else:
            body = f"{preview}\n\n[Content truncated to {max_length} of {full_len} characters]"

    header = _format_header(title, url, final_url)
    return f"{header}\n\n{body}"


# --- tool ---------------------------------------------------------------------


@tool
def fetch_url_nymeria(
    url: str = "",
    urls: str = "",
    extract: str = "markdown",
    extraction_prompt: str = "",
    max_length: int = _DEFAULT_MAX_LENGTH,
    config: Annotated[RunnableConfig, InjectedToolArg] = None,
) -> str:
    """
    Fetch a web page, PDF, RSS/Atom feed, or text document by URL.

    Returns the readable content. Free, in-process fetch with boilerplate removal. Best for static and
    server-rendered pages, articles, docs, PDFs, feeds, and text formats like
    JSON, XML, CSV and plain text. JavaScript-only pages may return little
    content (a hosted fetch provider handles those). Use a web_search_* tool to
    find URLs, then this tool to read them.

    RSS and Atom feeds are rendered as a numbered item list carrying each entry's
    title, link, published date, author and summary. Those are the same fields an
    "rss" trigger's conditions match against, so fetching a feed shows what a
    trigger watching it will actually receive: check a feed here before writing
    conditions against it, rather than guessing at the titles.

    Args:
        url: Single URL to fetch.
        urls: Multiple URLs separated by " | " (pipe), or by commas between full
              http(s) URLs. Takes precedence over url. Each is fetched
              independently. Max 10 per call.
        extract: "markdown" (default, preserves structure) or "text" (plain
                 prose; on an RSS/Atom feed this returns the raw feed XML instead
                 of the rendered item list).
        extraction_prompt: Leave empty to return the full readable page. Provide
                 a prompt (e.g. "pricing tiers and limits") and a secondary LLM
                 reads the page and returns only what the prompt asks for,
                 instead of the full text. Best for large pages where you want a
                 few specific facts; skip it for small pages (just read them).
                 The LLM sees the cleaned page up to ~30k tokens, so for very
                 large pages it can miss content past that; the result is tagged
                 with the model that produced it, and the tag says so when the
                 model was cut at its output limit mid-answer (the tail may be
                 missing; without that clause the extraction ran to its own
                 finish).
        max_length: Max characters of content returned (500-50000, default 8000).
                   Long pages are truncated when extraction_prompt is empty. When
                   truncated, the FULL extracted text is saved to a file in your
                   thread sandbox and the path is included so you can file_read or
                   grep it.

    Returns:
        A short header (title, source URL) followed by the content. Batch mode:
        sections separated by "=== URL N/M: <url> ===" headers. When a page
        exceeds max_length, the preview ends with the saved file path for the
        full text. Failures are returned as "[Error]: <reason>" strings (blocked,
        HTTP code, timeout, binary content, or could-not-extract), never raised.
    """
    if urls.strip():
        # Split on pipes, or on a comma only when the next token is a full http(s)
        # URL, so commas inside a single URL's query string do not fragment it.
        url_list = [
            u.strip()
            for u in re.split(r"\s*\|\s*|\s*,\s*(?=https?://)", urls)
            if u.strip()
        ]
        url_list = url_list[:_MAX_BATCH_URLS]
    elif url.strip():
        url_list = [url.strip()]
    else:
        return "[Error]: Provide a url or pipe/comma-separated urls."

    extract = extract.strip().lower()
    if extract not in _EXTRACT_FORMATS:
        extract = "markdown"
    max_length = max(_MIN_MAX_LENGTH, min(_MAX_MAX_LENGTH, max_length))

    logger.info(
        "fetch_url_nymeria: %d url(s) (extract=%s, extraction_prompt=%s)",
        len(url_list),
        extract,
        bool(extraction_prompt.strip()),
    )

    return run_batched(
        url_list,
        lambda u: _fetch_and_render(
            u,
            extract=extract,
            extraction_prompt=extraction_prompt,
            max_length=max_length,
            config=config,
        ),
        label="URL",
    )


# Opt-in page-fetch tool group. fetch_url_nymeria is the first member; hosted
# providers (fetch_url_firecrawl, fetch_url_jina, ...) land later as separate
# opt-in tools in the same Web group.
WEB_FETCH_TOOLS = [fetch_url_nymeria]


# Register this tool family for catalog auto-discovery (backlog #51).
register_tool_group(ToolGroup(name="web_fetch", tools=tuple(WEB_FETCH_TOOLS)))
