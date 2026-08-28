"""Web page-fetch tools for Nymeria.

`fetch_url_nymeria` is the first member of an opt-in fetch family that mirrors the
web_search_* family: a free, in-process, SSRF-safe fetcher that pulls a URL,
extracts the readable content as markdown or text, and optionally hands it to a
secondary model that reads the page and returns only what an `extraction_prompt`
asks for. Hosted members (fetch_url_firecrawl, ...) land later as separate
opt-in tools for the cases free extraction cannot match.

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

    @property
    def text(self) -> str:
        return self.content.decode(self.encoding or "utf-8", errors="replace")


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
        fetched = _Fetched(
            headers=dict(response.headers),
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


def _extract_content(response, extract: str) -> str:
    """Dispatch on Content-Type. Returns extracted text or a ``[Error]:`` string."""
    content_type = (response.headers.get("content-type") or "").split(";")[0].strip().lower()
    raw = response.content[:_MAX_FETCH_BYTES]

    # Detect PDFs by content-type, magic bytes, or .pdf extension. Many servers
    # send PDFs as application/octet-stream or binary/octet-stream, so the
    # content-type alone is not reliable; the "%PDF-" header is definitive.
    url_path = str(response.url).split("?", 1)[0].lower()
    is_pdf = (
        "pdf" in content_type
        or b"%PDF-" in raw[:1024]
        or url_path.endswith(".pdf")
    )
    if is_pdf:
        try:
            text = _extract_pdf(raw)
        except Exception as e:  # noqa: BLE001
            logger.error("PDF extraction failed: %s", e, exc_info=True)
            return f"[Error]: Could not read PDF: {e}"
        return text or "[Error]: PDF contained no extractable text (it may be scanned images)."

    is_html = "html" in content_type or (
        not content_type and b"<html" in raw[:2000].lower()
    )
    if is_html:
        try:
            html_text = raw.decode(response.encoding or "utf-8", errors="replace")
        except (LookupError, TypeError):
            html_text = raw.decode("utf-8", errors="replace")
        body = _extract_html(html_text, extract)
        if not body:
            return (
                "[Error]: Could not extract readable content (the page may require "
                "JavaScript; try a hosted fetch provider)."
            )
        return body

    if content_type.startswith("text/") or content_type in {
        "application/json",
        "application/xml",
        "application/atom+xml",
        "application/rss+xml",
    }:
        return raw.decode(response.encoding or "utf-8", errors="replace").strip()

    return f"[Error]: Unsupported content type '{content_type or 'unknown'}' for {response.url}"


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

    body = _extract_content(response, extract)
    if body.startswith("[Error]:"):
        return body

    title = ""
    content_type = (response.headers.get("content-type") or "").lower()
    if "html" in content_type:
        try:
            title = _extract_title(response.text)
        except Exception:  # noqa: BLE001
            title = ""

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
    Fetch a web page or PDF by URL and return its readable content.

    Free, in-process fetch with boilerplate removal. Best for static and
    server-rendered pages, articles, docs, and PDFs. JavaScript-only pages may
    return little content (a hosted fetch provider handles those). Use a
    web_search_* tool to find URLs, then this tool to read them.

    Args:
        url: Single URL to fetch.
        urls: Multiple URLs separated by " | " (pipe), or by commas between full
              http(s) URLs. Takes precedence over url. Each is fetched
              independently. Max 10 per call.
        extract: "markdown" (default, preserves structure) or "text" (plain prose).
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
        HTTP code, timeout, unsupported type, or could-not-extract), never raised.
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
