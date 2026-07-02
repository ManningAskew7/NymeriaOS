"""Discord bot Cog modules for slash command organization."""

from .chat import ChatCog
from .todos import TodosCog
from .config import ConfigCog
from .tools import ToolsCog
from .memory import MemoryCog
from .info import InfoCog
from .hooks import HooksCog

ALL_COGS = [ChatCog, TodosCog, ConfigCog, ToolsCog, MemoryCog, InfoCog, HooksCog]
