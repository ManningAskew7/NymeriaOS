"""Twitch clip window helpers shared by the ``twitch_clip`` tool and the bot.

Kept dependency-free on purpose: the Twitch bot is a thin client that must
not import the LangChain-backed tools module, and the tool must not import
the TwitchIO-backed bot.

Helix Create Clip publishes the last ``duration`` seconds of the stream
ending at the call (5 to 60 s, 0.1 s precision, default 30) out of a capture
buffer that reaches about 85 s behind it; ``has_delay`` was removed by Twitch
and has no effect. The ``edit_url`` a creation returns lets a human re-trim
any 5 to 60 s span of that buffer for 24 hours.
"""

from __future__ import annotations

import re
from typing import Any, Tuple

CLIP_URL_BASE = "https://clips.twitch.tv"

#: Helix Create Clip ``duration`` bounds (seconds, 0.1 precision).
CLIP_DURATION_MIN = 5.0
CLIP_DURATION_MAX = 60.0
#: The agent is slow to the moment (pulse batching), so it defaults to the
#: longest window Twitch allows.
AGENT_CLIP_DEFAULT_SECONDS = CLIP_DURATION_MAX
#: A chatter typing !clip reacts within seconds, so the default is shorter;
#: a long clip can be trimmed via the edit link, a short one cannot grow.
CHAT_CLIP_DEFAULT_SECONDS = 45.0
CLIP_TITLE_MAX = 100

_LEADING_SECONDS = re.compile(r"^\s*(\d{1,3}(?:\.\d+)?)\b\s*(.*)$", re.DOTALL)
#: ``@bot !clip ...``: on a reply thread Twitch auto-inserts the mention and
#: TwitchIO hands the command the ORIGINAL line, so the prefix must be
#: skipped or it lands in the clip's public title.
_COMMAND_PREFIX = re.compile(r"^\s*(?:@\S+[,:]?\s+)?!clip\b", re.IGNORECASE)


def clip_duration(value: Any, default: float = AGENT_CLIP_DEFAULT_SECONDS) -> float:
    """Coerce a requested clip length into Helix's 5..60 s, 0.1 s grid."""
    try:
        seconds = float(value)
    except (TypeError, ValueError):
        seconds = default
    if seconds != seconds:  # NaN
        seconds = default
    return round(min(CLIP_DURATION_MAX, max(CLIP_DURATION_MIN, seconds)), 1)


def clean_clip_title(value: Any) -> str:
    """Whitespace-normalised title cut to Twitch's limit; empty means default."""
    return " ".join(str(value or "").split())[:CLIP_TITLE_MAX]


def parse_clip_args(text: str, default: float = CHAT_CLIP_DEFAULT_SECONDS) -> Tuple[int, str]:
    """``!clip [seconds] [title]`` -> ``(whole seconds, title)``.

    A leading number is the length in seconds (clamped to Helix bounds); the
    rest, if any, is the title. ``!clip nice one`` is a 45 s clip titled
    "nice one"; ``!clip 60`` is a 60 s clip with Twitch's default title.

    Whole seconds on purpose: the bot sends this through TwitchIO, whose
    3.3.2 ``Route.build_url`` only serialises ``str``/``int`` query values
    and iterates anything else, so the ``float`` its own ``create_clip``
    accepts raises ``TypeError: 'float' object is not iterable`` before the
    request leaves (measured live 2026-09-11). Sub-second precision is
    worthless for a typed command anyway.
    """
    body = _COMMAND_PREFIX.sub("", text, count=1)
    match = _LEADING_SECONDS.match(body)
    if match:
        seconds, title = clip_duration(match.group(1), default=default), clean_clip_title(match.group(2))
    else:
        seconds, title = clip_duration(default, default=default), clean_clip_title(body)
    return int(round(seconds)), title
