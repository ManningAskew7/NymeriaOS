"""Discord bot Cog modules for slash command organization."""

from .chat import ChatCog
from .config import ConfigCog
from .tools import ToolsCog
from .memory import MemoryCog
from .info import InfoCog
from .hooks import HooksCog
from .fallback import FallbackCog
from .generated_cogs import GeneratedCommandsCog

ALL_COGS = [
    ChatCog,
    ConfigCog,
    ToolsCog,
    MemoryCog,
    InfoCog,
    HooksCog,
    FallbackCog,
    # Registry-derived commands (scripts/generate_discord_cogs.py). Loaded
    # alongside the hand cogs; the generator's exclusion sets are what keep
    # the two halves from claiming the same Discord name.
    GeneratedCommandsCog,
]
