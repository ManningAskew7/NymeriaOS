"""Browser sub-agent for web automation tasks.

Uses Gemini 3 Flash Preview for vision and decision-making,
with native Playwright browser tools for execution.
"""

from . import register_agent
from ..tools.browser import (
    browser_navigate,
    browser_click,
    browser_type,
    browser_get_content,
    browser_screenshot,
    browser_scroll,
    browser_close,
    browser_press_key,
    browser_status,
)

BROWSER_AGENT_PROMPT = """You are a browser automation agent. You control a web browser to complete tasks.

## Available Tools
- browser_navigate(url): Go to a URL
- browser_click(selector): Click element (CSS selector or "text=Button Text")
- browser_type(selector, text): Type into input field
- browser_get_content(): Get page text and links
- browser_screenshot(): Take a screenshot of the current page
- browser_scroll(direction, amount): Scroll up/down
- browser_press_key(key): Press keyboard key (Enter, Tab, Escape, etc.)
- browser_close(): Close browser when done
- browser_status(): Check browser/Playwright availability (use if having issues)

## Instructions
1. Break tasks into small steps
2. After each action, check the result before proceeding
3. Use browser_get_content() to understand the page state
4. For clicking, try CSS selectors first, then text= selectors
5. If an action fails, try an alternative approach
6. If you encounter errors, use browser_status() to diagnose
7. Report what you found/accomplished when done

## Selector Tips
- Buttons: "button", "text=Submit", "[type='submit']"
- Links: "a", "text=Click Here", "a[href*='login']"
- Inputs: "input[name='email']", "#search", "[placeholder='Search']"
- By text: "text=Exact Text" or "text=/partial/i" (regex)

## Error Handling
- If navigation times out, the page may still have loaded - try browser_get_content()
- If Playwright is unavailable, browser_navigate will fall back to simple HTTP fetch
- Use browser_status() to check if Playwright and Chromium are properly installed

Always be methodical and verify each step succeeded."""

# All browser tools - passed directly (not via allowed_tools)
# because they are removed from ALL_TOOLS
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

# Register the browser agent
register_agent(
    "BrowserAgent",
    {
        "name": "BrowserAgent",
        "description": "Autonomous browser control - navigate, click, type, extract data from websites",
        "system_prompt": BROWSER_AGENT_PROMPT,
        "context_turns": 10,  # Remember recent actions
        "tools": BROWSER_TOOLS,  # Direct tools (not from ALL_TOOLS)
        "allowed_tools": [],  # No additional tools from ALL_TOOLS needed
        # Use Gemini via OpenRouter for vision capabilities
        "llm_provider": "openrouter",
        "llm_model": "google/gemini-3-flash-preview",
        "llm_temperature": 0.3,
        "required_env_vars": ["OPENROUTER_API_KEY"],
    }
)
