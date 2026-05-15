"""Welcome panel and status header for the CLI."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any, Literal

from rich import box
from rich.cells import cell_len
from rich.console import Group
from rich.panel import Panel
from rich.rule import Rule
from rich.text import Text

from ..header import CLIHeaderSnapshot
from ..theme import CLITheme, DEFAULT_CLI_THEME
from .markdown import truncate_cell_width

if TYPE_CHECKING:
    from ..state import CLIState


HEADER_BORDER_STYLE = "white"
HEADER_INNER_RULE_STYLE = "grey37"
HEADER_LABEL_STYLE = "bold white"
HEADER_MAX_WIDTH = 79
HEADER_VALUE_STYLE = "grey70"
HEADER_LABEL_WIDTH = 11

WolfArtStyle = Literal["transparent", "green"]
WOLF_ART_STYLE: WolfArtStyle = "transparent"

WOLF_ART_TRANSPARENT = (
    "⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⣠⣾⣇⠀⣠⣶⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀",
    "⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⢀⣼⠟⠀⣿⣼⣿⣿⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀",
    "⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠠⣤⣶⣾⣿⡟⣼⡄⠙⠙⠛⠿⣦⣄⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀",
    "⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⢀⣠⣴⣾⡛⠁⣿⡇⠛⠁⠀⠀⠀⠀⠈⠻⣧⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀",
    "⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⢉⣽⡿⠛⠁⠠⠋⠀⠀⠀⠀⠀⠀⠙⢷⣆⠻⣧⣄⠀⠀⠀⠀⠀⠀⠀⠀⠀",
    "⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠠⠾⣿⣿⠆⠀⠀⣠⠂⢀⣴⠏⠀⠀⠀⠀⠀⠀⠀⠀⠉⠻⣶⣄⠀⠀⠀⠀⠀⠀",
    "⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⢀⣼⡟⠁⠀⠀⢰⡇⠐⣹⣏⣀⡄⠀⠀⠀⠀⣀⠀⠀⠀⢈⣿⠏⠀⠀⠀⠀⠀⠀",
    "⠀⠀⠀⠀⠀⠀⠀⠀⠀⣠⡿⠋⠀⢠⠀⠀⢿⡇⠀⡿⢻⡟⠀⠀⢠⣾⠟⠛⠛⠛⠛⠋⠁⠀⠀⠀⠀⠀⠀⠀",
    "⠀⠀⠀⠀⠀⠀⢀⡠⠞⠋⠀⠀⠀⠘⣇⠀⠘⣿⡄⠀⠘⣧⠀⠀⣾⡇⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀",
    "⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠘⣷⡀⠈⢿⣦⠀⠘⡆⠀⢹⣷⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀",
    "⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠈⢻⣦⠀⢻⣧⠀⠙⠀⡈⣿⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀",
    "⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠹⣧⠀⢿⡇⠀⢠⣷⣿⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀",
    "⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⣿⠀⢸⡿⢀⣼⣿⠇⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀",
    "⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⣿⠀⣾⣧⡾⠃⠋⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀",
    "⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠃⣰⡿⠋⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀",
    "⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠘⠁⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀",
)

WOLF_ART_GREEN = (
    "⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⣠⣾⡇⠀⢀⣤⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀",
    "⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⢀⣴⠏⠀⣿⣴⣿⣿⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀",
    "⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠠⣤⣴⣾⣿⡏⣸⡀⠘⠉⠛⠿⣦⣀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀",
    "⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⣀⣴⣾⡋⠁⣿⡇⠋⠀⠀⠀⠀⠀⠀⠹⣦⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀",
    "⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⢈⣭⡿⠋⠁⠠⠋⠀⠀⠀⠀⠀⠀⠘⠶⡄⠹⢦⣀⠀⠀⠀⠀⠀⠀⠀⠀⠀",
    "⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠠⠶⣿⣿⠂⠀⠀⣠⠀⢀⣴⠊⠀⠀⠀⠀⠀⠀⠀⠀⠉⠛⣦⣄⠀⠀⠀⠀⠀⠀",
    "⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⢀⣴⠟⠁⠀⠀⢰⠇⠐⢹⡇⣀⡀⠀⠀⠀⠀⠀⠀⠀⠀⢀⣿⠋⠀⠀⠀⠀⠀⠀",
    "⠀⠀⠀⠀⠀⠀⠀⠀⠀⣠⡾⠋⠀⠀⠀⠀⢸⡄⠀⠿⢛⡟⠀⠀⢀⣾⠟⠛⠛⠛⠛⠋⠁⠀⠀⠀⠀⠀⠀⠀",
    "⠀⠀⠀⠀⠀⠀⠀⡠⠞⠋⠀⠀⠀⠀⣆⠀⠘⣷⡄⠀⠈⣧⠀⠀⣾⡇⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀",
    "⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠘⢧⡀⠈⢿⣆⠀⠘⡆⠀⢸⣧⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀",
    "⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠈⠻⣦⠀⠻⣧⠀⠀⠀⠀⣿⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀",
    "⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠹⣧⠀⢻⡇⠀⢀⣷⣿⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀",
    "⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⢿⠀⢸⡇⠀⣼⣿⠃⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀",
    "⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⡼⠀⣼⣇⡾⠃⠃⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀",
    "⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠁⣰⠿⠋⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀",
    "⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠈⠁⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀",
)

WOLF_ARTS: dict[WolfArtStyle, tuple[str, ...]] = {
    "transparent": WOLF_ART_TRANSPARENT,
    "green": WOLF_ART_GREEN,
}


def render_welcome(
    state: "CLIState",
    snapshot: CLIHeaderSnapshot | None = None,
    *,
    theme: CLITheme | None = None,
    capabilities: Any | None = None,
) -> None:
    """Print the startup welcome panel."""

    if snapshot is not None:
        _render_header_snapshot(
            state,
            snapshot,
            theme=theme,
            capabilities=capabilities,
        )
        return

    model = state.get_effective_model()
    model_display = model.split("/")[-1] if "/" in model else model

    thread_display = state.get_thread_title()

    width = _header_width(state, capabilities)
    body = Text()
    wolf = WOLF_ARTS.get(WOLF_ART_STYLE, WOLF_ART_TRANSPARENT)
    for line in wolf:
        body.append(_center(line, width - 4) + "\n", style="bold white")
    body.append(_center("N Y M E R I A", width - 4) + "\n", style="bold white")
    body.append("\n")
    body.append("  Model   ", style="dim")
    body.append(f"{model_display}\n", style="white")
    body.append("  Thread  ", style="dim")
    body.append(f"{thread_display}\n", style="white")
    body.append("\n")
    body.append("  /help", style="dim cyan")
    body.append(" for commands  ", style="dim")
    body.append("/threads", style="dim cyan")
    body.append(" to switch", style="dim")

    state.console.print()
    state.console.print(
        Panel(
            body,
            box=box.SQUARE,
            border_style=HEADER_BORDER_STYLE,
            padding=(0, 1),
            width=width,
        )
    )
    state.console.print()


def _render_header_snapshot(
    state: "CLIState",
    snapshot: CLIHeaderSnapshot,
    *,
    theme: CLITheme | None,
    capabilities: Any | None,
) -> None:
    selected_theme = theme or DEFAULT_CLI_THEME
    width = _header_width(state, capabilities)
    body_width = max(28, width - 4)

    wolf = WOLF_ARTS.get(WOLF_ART_STYLE, WOLF_ART_TRANSPARENT)
    parts: list[Text | Rule] = []

    wolf_text = Text()
    for line in wolf:
        wolf_text.append(_center(line, body_width) + "\n", style="bold white")
    wolf_text.append(_center("N Y M E R I A", body_width), style="bold white")
    parts.append(wolf_text)

    parts.append(Rule(style=HEADER_INNER_RULE_STYLE))

    info_lines = _snapshot_info_lines(snapshot, body_width)
    info_text = Text()
    for i, (label, value) in enumerate(info_lines):
        if i > 0:
            info_text.append("\n")
        padded_label = f"  {label:<{HEADER_LABEL_WIDTH}}"
        info_text.append(padded_label, style=HEADER_LABEL_STYLE)
        info_text.append(
            _fit(value, max(1, body_width - cell_len(padded_label))),
            style=HEADER_VALUE_STYLE,
        )
    parts.append(info_text)

    footers = _snapshot_footers(snapshot, selected_theme, body_width)
    if footers:
        parts.append(Rule(style=HEADER_INNER_RULE_STYLE))
        parts.extend(footers)

    state.console.print()
    state.console.print(
        Panel(
            Group(*parts),
            box=box.SQUARE,
            border_style=HEADER_BORDER_STYLE,
            padding=(0, 1),
            width=width,
        )
    )
    state.console.print()


def _snapshot_info_lines(
    snapshot: CLIHeaderSnapshot,
    body_width: int,
) -> list[tuple[str, str]]:
    lines: list[tuple[str, str]] = []

    thread_parts = _join_parts(
        snapshot.thread_title,
        snapshot.short_thread_id,
        snapshot.platform,
        "pinned" if snapshot.pinned else "",
        _callable_status(snapshot),
        separator=" · ",
    )
    lines.append(("Thread", thread_parts))

    lines.append(("Provider", _provider_api_line(snapshot)))
    lines.append(("Model", _model_thinking_line(snapshot)))

    context = _context_inline(snapshot)
    lines.append(("Context", context))

    tools = _join_parts(
        f"{snapshot.tool_count} tools",
        f"{snapshot.mcp_tool_count} mcp",
        f"{snapshot.callable_tool_count} callable",
        separator=" · ",
    )
    lines.append(("Tools", tools))

    skills = f"{snapshot.skill_count} skills · {snapshot.skill_kit_count} kits"
    lines.append(("Skills", skills))

    lines.append(("Todos", _label_summary(snapshot.todo_labels, snapshot.todo_count)))
    lines.append(("Triggers", _label_summary(snapshot.trigger_labels, snapshot.trigger_count)))

    if snapshot.callable_team:
        lines.append(("Team", snapshot.callable_team))

    user_parts = _join_parts(
        snapshot.user_display_name or snapshot.user_id,
        snapshot.user_role if snapshot.user_role else "",
        separator=" · ",
    )
    lines.append(("User", user_parts))

    backend = _join_parts(
        snapshot.backend_url or "local",
        snapshot.health.label,
        separator=" · ",
    )
    lines.append(("Backend", backend))

    return lines


def _snapshot_footers(
    snapshot: CLIHeaderSnapshot,
    theme: CLITheme,
    body_width: int,
) -> list[Text]:
    footers: list[Text] = []
    flags = _flags_text(snapshot.flags, max(1, body_width - HEADER_LABEL_WIDTH - 4))
    if flags:
        footers.append(
            _footer(
                "CONFIG",
                flags,
                slot="prompt_busy",
                theme=theme,
                width=body_width,
            )
        )
    if snapshot.failures:
        footers.append(
            _footer(
                "Warning",
                f"fetch timed out: {', '.join(snapshot.failures)}",
                slot="error",
                theme=theme,
                width=body_width,
            )
        )
    return footers


def _footer(
    label: str,
    value: str,
    *,
    slot: str,
    theme: CLITheme,
    width: int,
) -> Text:
    label_text = f"  {label.upper():<{HEADER_LABEL_WIDTH}}"
    value_width = max(1, width - cell_len(label_text))
    text = Text()
    text.append(
        label_text,
        style=_section_header_style(theme, fallback_slot=slot),
    )
    text.append(_fit(value, value_width), style=HEADER_VALUE_STYLE)
    return text


def _section_header_style(
    theme: CLITheme,
    *,
    fallback_slot: str,
) -> str:
    del fallback_slot
    del theme
    return HEADER_LABEL_STYLE


def _callable_status(snapshot: CLIHeaderSnapshot) -> str:
    if not snapshot.callable:
        return ""
    parts = ["callable"]
    if snapshot.callable_name:
        parts.append(snapshot.callable_name)
    if snapshot.callable_team:
        parts.append(f"team {snapshot.callable_team}")
    return " ".join(parts)


def _context_inline(snapshot: CLIHeaderSnapshot) -> str:
    tokens = _count_text(snapshot.context_tokens)
    limit = _count_text(snapshot.context_limit)
    parts = []
    if tokens and limit:
        parts.append(f"{tokens} / {limit}")
    elif tokens:
        parts.append(tokens)
    if snapshot.context_percent is not None:
        parts.append(f"({snapshot.context_percent:.0f}%)")
    if snapshot.compaction_count is not None:
        parts.append(f"· {snapshot.compaction_count} compactions")
    return " ".join(parts) if parts else "unknown"


def _flags_text(flags: tuple[str, ...], width: int) -> str:
    if not flags:
        return ""
    selected = list(flags)
    while selected:
        text = "  ".join(selected)
        if cell_len(text) <= width:
            return _fit(text, width)
        selected.pop()
    return ""


def _label_summary(labels: tuple[str, ...], count: int) -> str:
    if count <= 0:
        return "none"
    parts = list(labels)
    remaining = count - len(parts)
    if remaining > 0:
        parts.append(f"+{remaining} more")
    if not parts:
        parts.append(f"{count} active")
    return ", ".join(parts)


def _provider_api_line(snapshot: CLIHeaderSnapshot) -> str:
    if not snapshot.api_type:
        return snapshot.provider
    return f"{snapshot.provider} ({snapshot.api_type})"


def _model_thinking_line(snapshot: CLIHeaderSnapshot) -> str:
    thinking = _thinking_label(snapshot.thinking_mode)
    if not thinking:
        return snapshot.model
    return f"{snapshot.model} ({thinking})"


def _thinking_label(value: str) -> str:
    text = str(value or "").strip()
    if not text or text.casefold() == "off":
        return ""
    if text.casefold().startswith("adaptive"):
        effort = text.removeprefix("adaptive").strip()
        effort = effort.removeprefix("(").removesuffix(")").strip()
        return f"Adaptive {effort.title()}".strip()
    if text.casefold() == "xhigh":
        return "XHigh"
    return text.title()


def _join_parts(*parts: str, separator: str = "  ") -> str:
    return separator.join(str(part) for part in parts if part)


def _count_text(value: int | None) -> str:
    if value is None:
        return ""
    return f"{value:,}"


def _fit(value: str, width: int) -> str:
    return truncate_cell_width(str(value or ""), max(1, width))


def _center(text: str, width: int) -> str:
    text_width = cell_len(text)
    if text_width >= width:
        return text
    pad = (width - text_width) // 2
    return " " * pad + text


def _terminal_width(state: "CLIState", capabilities: Any | None) -> int:
    if capabilities is not None:
        width = getattr(capabilities, "width", None)
        if isinstance(width, int) and width > 0:
            return width
    try:
        return max(40, int(state.console.width))
    except Exception:  # noqa: BLE001 - welcome rendering is best effort.
        return 80


def _header_width(state: "CLIState", capabilities: Any | None) -> int:
    return min(HEADER_MAX_WIDTH, _terminal_width(state, capabilities))


__all__ = ["render_welcome"]
