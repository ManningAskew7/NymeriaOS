"""Native Playwright browser tools for Nymeria.

Uses a dedicated browser thread to avoid Playwright's threading restrictions.
All browser operations are queued and executed on a single persistent thread.
"""

import base64
import logging
import os
import queue
import threading
from typing import Any, Optional, Tuple

from langchain_core.tools import tool

logger = logging.getLogger(__name__)

# Set Playwright browsers path based on environment
def _setup_playwright_path():
    """Configure Playwright browsers path for the current environment."""
    # Check if already set in environment
    if os.environ.get("PLAYWRIGHT_BROWSERS_PATH"):
        logger.info(f"Using PLAYWRIGHT_BROWSERS_PATH from env: {os.environ['PLAYWRIGHT_BROWSERS_PATH']}")
        return

    # Windows path (local development)
    windows_path = r"C:\Users\user\AppData\Local\ms-playwright"
    if os.path.exists(windows_path):
        os.environ["PLAYWRIGHT_BROWSERS_PATH"] = windows_path
        logger.info(f"Set PLAYWRIGHT_BROWSERS_PATH to {windows_path}")
        return

    # Linux paths (Docker)
    linux_paths = [
        "/root/.cache/ms-playwright",
        "/home/nymeria/.cache/ms-playwright",
        "/ms-playwright",
    ]
    for path in linux_paths:
        if os.path.exists(path):
            os.environ["PLAYWRIGHT_BROWSERS_PATH"] = path
            logger.info(f"Set PLAYWRIGHT_BROWSERS_PATH to {path}")
            return

    # Let Playwright use its default
    logger.info("Using default Playwright browsers path")

_setup_playwright_path()

# Determine if we should run headless (Docker/Linux without display)
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


class BrowserThread:
    """Dedicated thread for Playwright operations."""

    def __init__(self):
        self._command_queue: queue.Queue = queue.Queue()
        self._result_queue: queue.Queue = queue.Queue()
        self._thread: Optional[threading.Thread] = None
        self._running = False
        self._playwright = None
        self._browser = None
        self._page = None

    def start(self):
        """Start the browser thread."""
        if self._thread is not None and self._thread.is_alive():
            return

        self._running = True
        self._thread = threading.Thread(target=self._run, daemon=True, name="BrowserThread")
        self._thread.start()

    def stop(self):
        """Stop the browser thread."""
        self._running = False
        self._command_queue.put(("STOP", None))
        if self._thread:
            self._thread.join(timeout=10)

    def execute(self, command: str, args: dict) -> Tuple[bool, Any]:
        """Execute a command on the browser thread."""
        self.start()  # Ensure thread is running

        self._command_queue.put((command, args))

        try:
            success, result = self._result_queue.get(timeout=60)
            return success, result
        except queue.Empty:
            return False, "Browser operation timed out"

    def _run(self):
        """Main browser thread loop."""
        from playwright.sync_api import sync_playwright

        try:
            self._playwright = sync_playwright().start()
            logger.info("Playwright started on browser thread")

            while self._running:
                try:
                    command, args = self._command_queue.get(timeout=1)

                    if command == "STOP":
                        break

                    try:
                        result = self._execute_command(command, args)
                        self._result_queue.put((True, result))
                    except Exception as e:
                        logger.error(f"Browser command failed: {e}")
                        self._result_queue.put((False, str(e)))

                except queue.Empty:
                    continue

        except Exception as e:
            logger.error(f"Browser thread error: {e}")
        finally:
            self._cleanup()

    def _execute_command(self, command: str, args: dict) -> Any:
        """Execute a browser command."""
        # Ensure browser and page exist
        if command != "close" and self._page is None:
            self._ensure_page()

        if command == "navigate":
            self._page.goto(args["url"], wait_until='domcontentloaded', timeout=30000)
            return {"title": self._page.title(), "url": self._page.url}

        elif command == "click":
            selector = args["selector"]
            if selector.startswith('text='):
                self._page.click(selector, timeout=10000)
            else:
                try:
                    self._page.click(selector, timeout=5000)
                except Exception:
                    self._page.click(f'text="{selector}"', timeout=5000)
            self._page.wait_for_load_state('domcontentloaded', timeout=10000)
            return "clicked"

        elif command == "type":
            self._page.fill(args["selector"], args["text"], timeout=10000)
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

        elif command == "close":
            self._cleanup()
            return "closed"

        else:
            raise ValueError(f"Unknown command: {command}")

    def _ensure_page(self):
        """Ensure browser and page are ready."""
        if self._browser is None or not self._browser.is_connected():
            headless = _should_run_headless()
            logger.info(f"Launching browser in {'headless' if headless else 'visible'} mode")

            launch_args = ['--disable-blink-features=AutomationControlled']
            if headless:
                # Additional args for headless Docker environment
                launch_args.extend([
                    '--no-sandbox',
                    '--disable-dev-shm-usage',
                    '--disable-gpu',
                ])
            else:
                launch_args.append('--start-maximized')

            self._browser = self._playwright.chromium.launch(
                headless=headless,
                args=launch_args
            )

        if self._page is None or self._page.is_closed():
            context = self._browser.new_context(
                viewport={'width': 1920, 'height': 1080},
                user_agent='Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36'
            )
            self._page = context.new_page()

    def _cleanup(self):
        """Cleanup browser resources."""
        try:
            if self._page:
                self._page.close()
        except Exception:
            pass
        self._page = None

        try:
            if self._browser:
                self._browser.close()
        except Exception:
            pass
        self._browser = None

        try:
            if self._playwright:
                self._playwright.stop()
        except Exception:
            pass
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


@tool
def browser_navigate(url: str) -> str:
    """
    Navigate browser to a URL. Opens browser if not already open.

    Args:
        url: URL to navigate to (e.g., "https://google.com")
    """
    logger.info(f"browser_navigate: {url}")
    browser = _get_browser()
    success, result = browser.execute("navigate", {"url": url})

    if success:
        return f"[Success]: Navigated to {url}\nPage title: {result['title']}"
    else:
        return f"[Error]: Failed to navigate - {result}"


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
]
