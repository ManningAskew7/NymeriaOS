"""Native Playwright browser tools for Nymeria.

Uses a dedicated browser thread to avoid Playwright's threading restrictions.
All browser operations are queued and executed on a single persistent thread.

Features:
- Auto-detects Playwright browser installation path
- Configurable timeouts for different operations
- Graceful error handling with detailed error messages
- Fallback to requests+BeautifulSoup if Playwright unavailable
"""

import base64
from contextlib import contextmanager
import importlib.util
import logging
import os
import queue
import threading
import warnings
from typing import Any, Optional, Tuple
from urllib.parse import urlparse

from langchain_core.tools import tool

from nymeria.core.http_policy import (
    requests_get_with_policy,
    validate_http_egress_url,
)

logger = logging.getLogger(__name__)

# Timeout configuration (in seconds)
BROWSER_LAUNCH_TIMEOUT = 120  # Initial browser launch can be slow
NAVIGATION_TIMEOUT = 60  # Page navigation timeout
DEFAULT_OPERATION_TIMEOUT = 30  # Default for other operations
QUEUE_TIMEOUT = 90  # How long to wait for result from browser thread
ALLOWED_BROWSER_URL_SCHEMES = {"http", "https"}
_FALSE_ENV_VALUES = {"0", "false", "no", "off"}


def _validate_browser_url(url: str) -> Tuple[bool, str]:
    """Return a stripped HTTP(S) URL or a user-facing error string."""
    if not isinstance(url, str) or not url.strip():
        return (
            False,
            "[Error]: Invalid URL. browser_navigate requires an absolute "
            "http:// or https:// URL.",
        )

    candidate = url.strip()
    parsed = urlparse(candidate)
    scheme = parsed.scheme.lower()

    if scheme not in ALLOWED_BROWSER_URL_SCHEMES:
        display_scheme = scheme or "(none)"
        return (
            False,
            f"[Error]: Unsupported URL scheme '{display_scheme}'. "
            "browser_navigate only supports http:// and https:// URLs.",
        )

    if not parsed.netloc:
        return (
            False,
            "[Error]: Invalid URL. browser_navigate requires an absolute "
            "http:// or https:// URL with a host.",
        )

    try:
        candidate = validate_http_egress_url(candidate, label="browser URL")
    except ValueError as exc:
        return False, f"[Error]: {exc}"

    return True, candidate


def _browser_verify_ssl() -> bool:
    """Return whether browser fallback requests should verify TLS certificates."""
    raw_value = os.environ.get("BROWSER_VERIFY_SSL")
    if raw_value and raw_value.strip().lower() in _FALSE_ENV_VALUES:
        logger.warning("Ignoring insecure BROWSER_VERIFY_SSL=%r; TLS verification is enforced", raw_value)
    return True


@contextmanager
def _maybe_suppress_insecure_request_warning(verify_ssl: bool):
    """Suppress urllib3's warning only when fallback TLS verification is disabled."""
    if verify_ssl:
        yield
        return

    try:
        from urllib3.exceptions import InsecureRequestWarning
    except Exception:
        yield
        return

    with warnings.catch_warnings():
        warnings.simplefilter("ignore", InsecureRequestWarning)
        yield


def _requests_get(url: str, headers: dict[str, str]):
    """Issue a fallback GET request with configured TLS verification."""
    verify_ssl = _browser_verify_ssl()
    with _maybe_suppress_insecure_request_warning(verify_ssl):
        response, _redirect_chain, _policy = requests_get_with_policy(
            url,
            headers=headers,
            timeout=30,
            verify=verify_ssl,
        )
        return response


def _find_playwright_browsers_path() -> Optional[str]:
    """Auto-detect Playwright browsers installation path."""
    # Check if already set in environment
    if os.environ.get("PLAYWRIGHT_BROWSERS_PATH"):
        path = os.environ["PLAYWRIGHT_BROWSERS_PATH"]
        if os.path.exists(path):
            return path
    
    # Windows paths - check common locations
    if os.name == 'nt':
        # Get current user's home directory
        user_home = os.path.expanduser("~")
        windows_paths = [
            os.path.join(user_home, "AppData", "Local", "ms-playwright"),
            r"C:\Users\user\AppData\Local\ms-playwright",
            r"C:\ms-playwright",
        ]
        for path in windows_paths:
            if os.path.exists(path):
                return path
    
    # Linux paths (Docker and native)
    linux_paths = [
        os.path.expanduser("~/.cache/ms-playwright"),
        "/root/.cache/ms-playwright",
        "/home/nymeria/.cache/ms-playwright",
        "/ms-playwright",
    ]
    for path in linux_paths:
        if os.path.exists(path):
            return path
    
    return None


def _setup_playwright_path():
    """Configure Playwright browsers path for the current environment."""
    path = _find_playwright_browsers_path()
    if path:
        os.environ["PLAYWRIGHT_BROWSERS_PATH"] = path
        logger.info(f"Set PLAYWRIGHT_BROWSERS_PATH to {path}")
    else:
        logger.warning("Could not find Playwright browsers path - will use default")


# Setup path on module load
_setup_playwright_path()


def _use_fallback_mode() -> bool:
    """Check if we should skip Playwright entirely and use requests fallback.

    Set BROWSER_FORCE_FALLBACK=true in .env to enable this.
    Useful when Playwright hangs (e.g. SSL interception, Python 3.14 incompatibility).
    """
    return os.environ.get("BROWSER_FORCE_FALLBACK", "").lower() == "true"


# Cache for fallback mode — tracks last fetched URL + content so browser_get_content
# can work without a live browser session.
_fallback_cache: dict = {"url": None, "title": None, "text": None, "links": []}


def _should_run_headless() -> bool:
    """Determine if browser should run in headless mode."""
    # Explicit override from environment
    headless_env = os.environ.get("BROWSER_HEADLESS", "").lower()
    if headless_env == "true":
        return True
    if headless_env == "false":
        return False

    # Auto-detect: headless in Docker or Linux without DISPLAY
    if os.path.exists("/.dockerenv"):
        return True
    if os.name != "nt" and not os.environ.get("DISPLAY"):
        return True

    return False


def _check_playwright_available() -> Tuple[bool, str]:
    """Check if Playwright is installed and browsers are available."""
    if importlib.util.find_spec("playwright.sync_api") is None:
        return False, "Playwright not installed. Run: pip install playwright && playwright install chromium"
    
    # Check if browsers are installed
    browsers_path = _find_playwright_browsers_path()
    if not browsers_path:
        return False, "Playwright browsers not installed. Run: playwright install chromium"
    
    # Check for chromium specifically
    chromium_dirs = [d for d in os.listdir(browsers_path) if d.startswith('chromium')]
    if not chromium_dirs:
        return False, f"Chromium not found in {browsers_path}. Run: playwright install chromium"
    
    return True, "Playwright ready"


class BrowserThread:
    """Dedicated thread for Playwright operations."""

    def __init__(self):
        self._command_queue: queue.Queue = queue.Queue()
        self._result_queue: queue.Queue = queue.Queue()
        self._thread: Optional[threading.Thread] = None
        self._running = False
        self._ready = threading.Event()
        self._error: Optional[str] = None
        self._playwright = None
        self._browser = None
        self._page = None
        self._last_policy_block: Optional[str] = None

    def start(self) -> Tuple[bool, str]:
        """Start the browser thread. Returns (success, message)."""
        if self._thread is not None and self._thread.is_alive():
            if self._ready.is_set():
                return True, "Browser thread already running"
            # Wait for it to become ready
            if self._ready.wait(timeout=BROWSER_LAUNCH_TIMEOUT):
                if self._error:
                    return False, self._error
                return True, "Browser thread ready"
            return False, "Browser thread startup timed out"

        # Check Playwright availability before starting
        available, msg = _check_playwright_available()
        if not available:
            return False, msg

        self._running = True
        self._ready.clear()
        self._error = None
        self._thread = threading.Thread(target=self._run, daemon=True, name="BrowserThread")
        self._thread.start()
        
        # Wait for thread to signal ready or error
        if self._ready.wait(timeout=BROWSER_LAUNCH_TIMEOUT):
            if self._error:
                return False, self._error
            return True, "Browser thread started"
        
        return False, "Browser thread startup timed out"

    def stop(self):
        """Stop the browser thread."""
        self._running = False
        self._command_queue.put(("STOP", None))
        if self._thread:
            self._thread.join(timeout=10)
        self._ready.clear()

    def execute(self, command: str, args: dict, timeout: int = QUEUE_TIMEOUT) -> Tuple[bool, Any]:
        """Execute a command on the browser thread."""
        # Ensure thread is running
        success, msg = self.start()
        if not success:
            return False, msg

        self._command_queue.put((command, args))

        try:
            success, result = self._result_queue.get(timeout=timeout)
            return success, result
        except queue.Empty:
            return False, f"Browser operation timed out after {timeout}s. The browser may be unresponsive."

    def _run(self):
        """Main browser thread loop."""
        try:
            from playwright.sync_api import sync_playwright
        except ImportError as e:
            self._error = f"Failed to import Playwright: {e}"
            self._ready.set()
            return

        try:
            logger.info("Starting Playwright...")
            self._playwright = sync_playwright().start()
            logger.info("Playwright started successfully")
            self._ready.set()

            while self._running:
                try:
                    command, args = self._command_queue.get(timeout=1)

                    if command == "STOP":
                        break

                    try:
                        result = self._execute_command(command, args)
                        self._result_queue.put((True, result))
                    except Exception as e:
                        error_msg = str(e)
                        logger.error(f"Browser command '{command}' failed: {error_msg}")
                        self._result_queue.put((False, error_msg))

                except queue.Empty:
                    continue

        except Exception as e:
            error_msg = f"Browser thread error: {e}"
            logger.error(error_msg)
            self._error = error_msg
            self._ready.set()
        finally:
            self._cleanup()

    def _execute_command(self, command: str, args: dict) -> Any:
        """Execute a browser command."""
        # Ensure browser and page exist (except for close)
        if command != "close" and self._page is None:
            self._ensure_page()

        if command == "close":
            self._cleanup()
            return "closed"

        assert self._page is not None  # guaranteed by _ensure_page()

        if command == "navigate":
            url = args["url"]
            logger.info(f"Navigating to {url}")
            self._last_policy_block = None
            try:
                self._page.goto(
                    url,
                    wait_until='domcontentloaded',
                    timeout=NAVIGATION_TIMEOUT * 1000,
                )
            except Exception as exc:
                if self._last_policy_block:
                    raise RuntimeError(self._last_policy_block) from exc
                raise
            return {"title": self._page.title(), "url": self._page.url}

        elif command == "click":
            selector = args["selector"]
            timeout_ms = DEFAULT_OPERATION_TIMEOUT * 1000
            if selector.startswith('text='):
                self._page.click(selector, timeout=timeout_ms)
            else:
                try:
                    self._page.click(selector, timeout=timeout_ms // 2)
                except Exception:
                    # Fallback to text selector
                    self._page.click(f'text="{selector}"', timeout=timeout_ms // 2)
            self._page.wait_for_load_state('domcontentloaded', timeout=timeout_ms)
            return "clicked"

        elif command == "type":
            self._page.fill(args["selector"], args["text"], timeout=DEFAULT_OPERATION_TIMEOUT * 1000)
            return "typed"

        elif command == "get_content":
            url = self._page.url
            title = self._page.title()
            text = self._page.evaluate('''() => {
                const scripts = document.querySelectorAll('script, style, noscript');
                scripts.forEach(s => s.remove());
                return document.body.innerText;
            }''')
            links = self._page.evaluate('''() => {
                const links = Array.from(document.querySelectorAll('a[href]'));
                return links.slice(0, 20).map(a => ({
                    text: a.innerText.trim().substring(0, 50),
                    href: a.href
                })).filter(l => l.text && l.href.startsWith('http'));
            }''')
            return {"url": url, "title": title, "text": text, "links": links}

        elif command == "screenshot":
            screenshot_bytes = self._page.screenshot(full_page=False)
            return base64.b64encode(screenshot_bytes).decode('utf-8')

        elif command == "scroll":
            direction = args.get("direction", "down")
            amount = args.get("amount", 500)
            amount_val = -abs(amount) if direction.lower() == "up" else abs(amount)
            self._page.evaluate(f'window.scrollBy(0, {amount_val})')
            return "scrolled"

        elif command == "press_key":
            self._page.keyboard.press(args["key"])
            return "pressed"

        else:
            raise ValueError(f"Unknown command: {command}")

    def _ensure_page(self):
        """Ensure browser and page are ready."""
        assert self._playwright is not None  # set by _run() before commands execute
        if self._browser is None or not self._browser.is_connected():
            headless = _should_run_headless()
            logger.info(f"Launching Chromium in {'headless' if headless else 'visible'} mode")

            launch_args = ['--disable-blink-features=AutomationControlled']
            if headless:
                # Additional args for headless/Docker environment
                launch_args.extend([
                    '--no-sandbox',
                    '--disable-dev-shm-usage',
                    '--disable-gpu',
                ])
            else:
                launch_args.append('--start-maximized')

            self._browser = self._playwright.chromium.launch(
                headless=headless,
                args=launch_args,
                timeout=BROWSER_LAUNCH_TIMEOUT * 1000
            )
            logger.info("Browser launched successfully")

        if self._page is None or self._page.is_closed():
            context = self._browser.new_context(
                viewport={'width': 1920, 'height': 1080},
                user_agent='Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36'
            )
            context.route("**/*", self._route_with_policy)
            self._page = context.new_page()
            logger.info("Browser page created")

    def _route_with_policy(self, route):
        request_url = route.request.url
        scheme = urlparse(request_url).scheme.lower()
        if scheme in ALLOWED_BROWSER_URL_SCHEMES:
            try:
                validate_http_egress_url(request_url, label="browser request")
            except ValueError as exc:
                self._last_policy_block = str(exc)
                logger.warning("Blocked browser request by HTTP policy: %s", exc)
                route.abort()
                return
        route.continue_()

    def _cleanup(self):
        """Cleanup browser resources."""
        logger.info("Cleaning up browser resources")
        try:
            if self._page:
                self._page.close()
        except Exception as e:
            logger.debug(f"Error closing page: {e}")
        self._page = None

        try:
            if self._browser:
                self._browser.close()
        except Exception as e:
            logger.debug(f"Error closing browser: {e}")
        self._browser = None

        try:
            if self._playwright:
                self._playwright.stop()
        except Exception as e:
            logger.debug(f"Error stopping playwright: {e}")
        self._playwright = None


# Global browser thread instance
_browser_thread: Optional[BrowserThread] = None
_browser_lock = threading.Lock()


def _get_browser() -> BrowserThread:
    """Get or create the browser thread."""
    global _browser_thread
    with _browser_lock:
        if _browser_thread is None:
            _browser_thread = BrowserThread()
        return _browser_thread


def _reset_browser():
    """Reset the browser thread (useful after errors)."""
    global _browser_thread
    with _browser_lock:
        if _browser_thread is not None:
            _browser_thread.stop()
            _browser_thread = None


# ============================================================================
# Fallback implementation using requests + BeautifulSoup
# ============================================================================

def _fallback_navigate(url: str) -> str:
    """Fallback navigation using requests + BeautifulSoup."""
    valid, validated_url_or_error = _validate_browser_url(url)
    if not valid:
        return validated_url_or_error
    url = validated_url_or_error

    try:
        from bs4 import BeautifulSoup
    except ImportError:
        return "[Error]: Fallback requires 'requests' and 'beautifulsoup4'. Install with: pip install requests beautifulsoup4"

    try:
        headers = {
            'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36'
        }
        response = _requests_get(url, headers=headers)
        response.raise_for_status()

        soup = BeautifulSoup(response.text, 'html.parser')
        title = soup.title.string if soup.title else "No title"

        # Remove scripts/styles for clean text
        for element in soup(['script', 'style', 'noscript']):
            element.decompose()
        text = soup.get_text(separator='\n', strip=True)
        if len(text) > 8000:
            text = text[:8000] + "\n...[truncated]"

        links = []
        for a in soup.find_all('a', href=True)[:20]:
            href = a['href']
            if href.startswith('http'):
                link_text = a.get_text(strip=True)[:50]
                if link_text:
                    links.append({"text": link_text, "href": href})

        # Populate cache for browser_get_content
        _fallback_cache["url"] = url
        _fallback_cache["title"] = title
        _fallback_cache["text"] = text
        _fallback_cache["links"] = links

        return f"[Success - Fallback Mode]: Fetched {url}\nPage title: {title}"
    except Exception as e:
        return f"[Error]: Fallback navigation failed - {e}"


def _fallback_get_content(url: str) -> str:
    """Fallback content extraction using requests + BeautifulSoup."""
    valid, validated_url_or_error = _validate_browser_url(url)
    if not valid:
        return validated_url_or_error
    url = validated_url_or_error

    try:
        from bs4 import BeautifulSoup
    except ImportError:
        return "[Error]: Fallback requires 'requests' and 'beautifulsoup4'"

    try:
        headers = {
            'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36'
        }
        response = _requests_get(url, headers=headers)
        response.raise_for_status()
        
        soup = BeautifulSoup(response.text, 'html.parser')
        
        # Remove script and style elements
        for element in soup(['script', 'style', 'noscript']):
            element.decompose()
        
        title = soup.title.string if soup.title else "No title"
        text = soup.get_text(separator='\n', strip=True)
        
        # Truncate if too long
        if len(text) > 8000:
            text = text[:8000] + "\n...[truncated]"
        
        # Get links
        links = []
        for a in soup.find_all('a', href=True)[:20]:
            href = a['href']
            if href.startswith('http'):
                link_text = a.get_text(strip=True)[:50]
                if link_text:
                    links.append(f"- [{link_text}]({href})")
        
        output = f"URL: {url}\nTitle: {title}\n\nContent:\n{text}"
        if links:
            output += "\n\nLinks:\n" + "\n".join(links)
        
        return output
    except Exception as e:
        return f"[Error]: Fallback content extraction failed - {e}"


# ============================================================================
# Tool definitions
# ============================================================================

@tool
def browser_navigate(url: str) -> str:
    """
    Navigate browser to a URL. Opens browser if not already open.

    Args:
        url: URL to navigate to (e.g., "https://google.com")
    """
    logger.info(f"browser_navigate: {url}")

    valid, validated_url_or_error = _validate_browser_url(url)
    if not valid:
        return validated_url_or_error
    url = validated_url_or_error

    if _use_fallback_mode():
        logger.info("BROWSER_FORCE_FALLBACK=true; skipping Playwright, using requests fallback")
        return _fallback_navigate(url)

    browser = _get_browser()
    success, result = browser.execute("navigate", {"url": url})

    if success:
        return f"[Success]: Navigated to {url}\nPage title: {result['title']}"
    else:
        # Try fallback if Playwright failed
        logger.warning(f"Playwright navigation failed: {result}. Trying fallback...")
        fallback_result = _fallback_navigate(url)
        if "[Success" in fallback_result:
            return fallback_result
        return f"[Error]: Failed to navigate - {result}\n\nFallback also failed: {fallback_result}"


@tool
def browser_click(selector: str) -> str:
    """
    Click an element on the page.

    Args:
        selector: CSS selector or text to click (e.g., "button.submit", "text=Login")
    """
    logger.info(f"browser_click: {selector}")
    browser = _get_browser()
    success, result = browser.execute("click", {"selector": selector})

    if success:
        return f"[Success]: Clicked '{selector}'"
    else:
        return f"[Error]: Could not click '{selector}' - {result}"


@tool
def browser_type(selector: str, text: str) -> str:
    """
    Type text into an input field.

    Args:
        selector: CSS selector for input field (e.g., "input[name='search']", "#email")
        text: Text to type
    """
    logger.info(f"browser_type: {selector}")
    browser = _get_browser()
    success, result = browser.execute("type", {"selector": selector, "text": text})

    if success:
        return f"[Success]: Typed into '{selector}'"
    else:
        return f"[Error]: Type failed - {result}"


@tool
def browser_get_content(include_links: bool = True) -> str:
    """
    Get the text content of the current page.

    Args:
        include_links: Whether to include link URLs (default True)
    """
    logger.info("browser_get_content")

    if _use_fallback_mode():
        if not _fallback_cache["url"]:
            return "[Error]: No page loaded. Use browser_navigate(url) first."
        output = f"URL: {_fallback_cache['url']}\nTitle: {_fallback_cache['title']}\n\nContent:\n{_fallback_cache['text']}"
        if include_links and _fallback_cache.get("links"):
            output += "\n\nLinks:\n"
            for link in _fallback_cache["links"]:
                output += f"- [{link['text']}]({link['href']})\n"
        return output

    browser = _get_browser()
    success, result = browser.execute("get_content", {})

    if success:
        text = result["text"]
        if len(text) > 8000:
            text = text[:8000] + "\n...[truncated]"

        output = f"URL: {result['url']}\nTitle: {result['title']}\n\nContent:\n{text}"

        if include_links and result.get("links"):
            output += "\n\nLinks:\n"
            for link in result["links"]:
                output += f"- [{link['text']}]({link['href']})\n"

        return output
    else:
        return f"[Error]: Get content failed - {result}"


@tool
def browser_screenshot() -> str:
    """
    Take a screenshot of the current page. Returns base64 encoded image.
    """
    logger.info("browser_screenshot")
    browser = _get_browser()
    success, result = browser.execute("screenshot", {})

    if success:
        return f"[Screenshot]: data:image/png;base64,{result[:100]}... ({len(result)} chars)"
    else:
        return f"[Error]: Screenshot failed - {result}"


@tool
def browser_scroll(direction: str = "down", amount: int = 500) -> str:
    """
    Scroll the page.

    Args:
        direction: "up" or "down"
        amount: Pixels to scroll (default 500)
    """
    logger.info(f"browser_scroll: {direction} {amount}px")
    browser = _get_browser()
    success, result = browser.execute("scroll", {"direction": direction, "amount": amount})

    if success:
        return f"[Success]: Scrolled {direction} {abs(amount)}px"
    else:
        return f"[Error]: Scroll failed - {result}"


@tool
def browser_close() -> str:
    """
    Close the browser.
    """
    logger.info("browser_close")
    browser = _get_browser()
    success, result = browser.execute("close", {})
    
    # Reset the browser thread so next operation creates a fresh instance
    _reset_browser()

    if success:
        return "[Success]: Browser closed"
    else:
        return f"[Error]: Close failed - {result}"


@tool
def browser_press_key(key: str) -> str:
    """
    Press a keyboard key.

    Args:
        key: Key to press (e.g., "Enter", "Tab", "Escape", "ArrowDown")
    """
    logger.info(f"browser_press_key: {key}")
    browser = _get_browser()
    success, result = browser.execute("press_key", {"key": key})

    if success:
        return f"[Success]: Pressed '{key}'"
    else:
        return f"[Error]: Key press failed - {result}"


@tool
def browser_status() -> str:
    """
    Check the browser status and Playwright availability.
    Useful for diagnosing issues.
    """
    fallback = _use_fallback_mode()
    available, msg = _check_playwright_available()

    status_lines = [
        f"Force fallback mode (BROWSER_FORCE_FALLBACK): {fallback}",
        f"Playwright available: {available}",
        f"Status: {msg}",
        f"Browsers path: {_find_playwright_browsers_path() or 'Not found'}",
        f"Headless mode: {_should_run_headless()}",
    ]
    if fallback:
        cached_url = _fallback_cache.get("url") or "none"
        status_lines.append(f"Fallback last URL: {cached_url}")

    return "\n".join(status_lines)


# Export browser tools
BROWSER_TOOLS = [
    browser_navigate,
    browser_click,
    browser_type,
    browser_get_content,
    browser_screenshot,
    browser_scroll,
    browser_close,
    browser_press_key,
    browser_status,
]
