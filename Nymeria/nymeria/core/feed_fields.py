"""One reader for feedparser entry fields, shared by every feed surface.

Three places in this codebase read entries out of a parsed RSS/Atom feed:
``triggers/sources/rss_source.py`` (what an ``rss`` trigger DELIVERS),
``tools/web_fetch.py`` (what ``fetch_url_nymeria`` PREVIEWS, whose docstring
promises the preview shows exactly what a trigger watching that feed will
receive), and ``tools/public_info_integrations.py`` (the ``fetch_rss_feed``
tool). They had three subtly different readers and two different answers about
dates, so the preview's promise was prose rather than a property of the code.
This module is the shared implementation that makes it a property (backlog
#309).

Why a ``core`` module rather than importing ``rss_source`` from the tools:
``triggers/sources/__init__.py`` auto-loads EVERY source plugin on import
(outlook, teams, slack and their dependencies), so reaching into that package
from a tool would drag the whole trigger source surface into the tool import
path. This module depends on nothing.
"""

from typing import Any

# In priority order. Atom REQUIRES <updated> and makes <published> optional, and
# feedparser does not alias one onto the other, so a feed that carries only
# <updated> (measured: every GitHub releases/commits/tags feed) yielded an empty
# `published` on every delivered event, and a {published} template rendered a
# blank with nothing erroring. `created` is Atom 0.3's spelling. RSS 2.0's
# <pubDate> is already mapped onto `published` by feedparser, so it keeps
# winning and this order is a pure widening.
_PUBLISHED_KEYS = ("published", "updated", "created")


def feed_entry_field(entry: Any, key: str, default: str = "") -> str:
    """Read one field off a feedparser entry as a string.

    feedparser entries are dict-like AND attribute-style, and callers hold them
    both ways, so both access shapes are supported. A missing key and a
    present-but-``None`` key both yield ``default``: an earlier reader used
    ``str(entry.get(key, default))``, which turned an explicit ``None`` into the
    literal string ``"None"`` and put that in a delivered trigger event.
    """
    if hasattr(entry, "get"):
        value = entry.get(key, default)
    else:
        value = getattr(entry, key, default)
    if value is None:
        value = default
    return str(value or default)


def feed_entry_published(entry: Any) -> str:
    """Return an entry's publication date, falling back across feed dialects.

    Tries ``published``, then ``updated``, then ``created``, and returns the
    first non-empty one. The value is whatever string the feed carried:
    nothing here parses or reformats a date, because every consumer of this
    passes it through to a template or a preview line verbatim.
    """
    for key in _PUBLISHED_KEYS:
        value = feed_entry_field(entry, key)
        if value:
            return value
    return ""
