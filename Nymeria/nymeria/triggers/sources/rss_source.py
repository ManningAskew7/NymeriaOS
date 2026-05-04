"""RSS/Atom feed trigger source -- fires when new entries appear in a feed.

Polls any RSS or Atom feed URL and returns events for new entries.
Uses feedparser for robust feed parsing. Falls back to raw httpx if
feedparser is not installed.
"""

import logging
from typing import Any, Dict, List

from .base import BaseTriggerSource
from . import register_source

logger = logging.getLogger(__name__)


class RSSSource(BaseTriggerSource):
    """Poll-based RSS/Atom feed monitor."""

    name = "rss"
    description = "Fires when new entries appear in an RSS or Atom feed"
    category = "monitoring"
    icon = "fileText"
    setup_guide = (
        "Monitor any RSS or Atom feed for new entries.\n\n"
        "**Examples of feeds you can monitor:**\n"
        "- Blog posts: `https://example.com/feed.xml`\n"
        "- Reddit: `https://www.reddit.com/r/python/.rss`\n"
        "- YouTube: `https://www.youtube.com/feeds/videos.xml?channel_id=...`\n"
        "- GitHub releases: `https://github.com/owner/repo/releases.atom`\n\n"
        "The source checks for new entries every ~30 seconds and deduplicates "
        "using entry IDs."
    )
    template_variables = ["title", "link", "summary", "author", "published", "feed_title"]
    example_config = {"url": "https://example.com/feed.xml", "max_items": 5}

    config_schema: Dict[str, Any] = {
        "url": {
            "type": "string",
            "description": "RSS or Atom feed URL",
            "required": True,
            "placeholder": "https://example.com/feed.xml",
            "order": 1,
        },
        "max_items": {
            "type": "integer",
            "description": "Maximum new items per poll cycle",
            "required": False,
            "default": 5,
            "order": 2,
        },
    }

    def check(self, config: dict, state: dict) -> List[dict]:
        url = config["url"]
        max_items = config.get("max_items", 5)

        import httpx

        try:
            resp = httpx.get(url, timeout=15, follow_redirects=True)
            resp.raise_for_status()
        except Exception as e:
            logger.error(f"rss source: failed to fetch {url}: {e}")
            raise

        try:
            import feedparser
            feed = feedparser.parse(resp.text)
            entries = feed.entries
            feed_title = feed.feed.get("title", "")
        except ImportError:
            logger.warning("feedparser not installed, using basic XML parsing")
            entries, feed_title = self._basic_parse(resp.text)

        seen_ids = set(state.get("seen_ids", []))

        # First poll: baseline without flooding
        if not state.get("initialized"):
            new_seen = []
            for entry in entries:
                entry_id = self._entry_id(entry)
                new_seen.append(entry_id)
            state["seen_ids"] = new_seen[-500:]
            state["initialized"] = True
            logger.info(f"rss source: initialized with {len(new_seen)} entries from {url}")
            return []

        events = []
        new_seen = list(state.get("seen_ids", []))

        for entry in entries:
            entry_id = self._entry_id(entry)
            if entry_id in seen_ids:
                continue

            new_seen.append(entry_id)
            events.append({
                "title": self._get(entry, "title", ""),
                "link": self._get(entry, "link", ""),
                "summary": self._get(entry, "summary", "")[:500],
                "author": self._get(entry, "author", ""),
                "published": self._get(entry, "published", ""),
                "feed_title": feed_title,
            })

            if len(events) >= max_items:
                break

        if len(new_seen) > 500:
            new_seen = new_seen[-500:]
        state["seen_ids"] = new_seen

        if events:
            logger.info(f"rss source: {len(events)} new entry/entries from {url}")
        return events

    @staticmethod
    def _entry_id(entry) -> str:
        if hasattr(entry, "get"):
            return entry.get("id") or entry.get("link") or entry.get("title", "unknown")
        return getattr(entry, "id", None) or getattr(entry, "link", None) or getattr(entry, "title", "unknown")

    @staticmethod
    def _get(entry, key: str, default: str = "") -> str:
        if hasattr(entry, "get"):
            return str(entry.get(key, default))
        return str(getattr(entry, key, default))

    @staticmethod
    def _basic_parse(text: str):
        """Minimal fallback when feedparser is not available."""
        import re
        entries = []
        feed_title = ""

        title_match = re.search(r"<title[^>]*>(.*?)</title>", text, re.DOTALL)
        if title_match:
            feed_title = title_match.group(1).strip()

        for item_match in re.finditer(r"<(?:item|entry)[^>]*>(.*?)</(?:item|entry)>", text, re.DOTALL):
            item_text = item_match.group(1)
            entry = {}
            for tag in ("title", "link", "description", "author", "pubDate", "published", "id"):
                tag_match = re.search(rf"<{tag}[^>]*>(.*?)</{tag}>", item_text, re.DOTALL)
                if tag_match:
                    entry[tag] = tag_match.group(1).strip()
            if "description" in entry:
                entry["summary"] = entry.pop("description")
            if "pubDate" in entry:
                entry["published"] = entry.pop("pubDate")
            entries.append(entry)

        return entries, feed_title

    def get_sample_event(self, config: dict) -> dict:
        return {
            "title": "New Release: Project v2.0",
            "link": "https://example.com/blog/new-release-v2",
            "summary": "We're excited to announce version 2.0 with major improvements...",
            "author": "Jane Smith",
            "published": "2026-04-20T10:00:00Z",
            "feed_title": "Example Blog",
        }

    def validate_config(self, config: dict) -> tuple:
        ok, msg = super().validate_config(config)
        if not ok:
            return ok, msg
        max_items = config.get("max_items")
        if max_items is not None and (max_items < 1 or max_items > 50):
            return False, "max_items must be between 1 and 50"
        return True, "ok"


register_source("rss", RSSSource)
