"""Welcome panel and status header for the CLI."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from rich import box
from rich.cells import cell_len
from rich.console import Group
from rich.panel import Panel
from rich.rule import Rule
from rich.table import Table
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
    # Shorten long model IDs for display
    model_display = model.split("/")[-1] if "/" in model else model

    thread_display = state.get_thread_title()

    body = Text()
    body.append("Nymeria\n", style="bold blue")
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
            padding=(0, 2),
            width=_header_width(state, capabilities),
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
    columns = _section_columns(body_width)
    column_specs = _section_column_specs(body_width, columns)
    column_widths = tuple(width for width, _ratio in column_specs)
    sections = _snapshot_sections(snapshot, selected_theme, column_widths)
    table = _sections_table(sections, column_specs)
    footers = _snapshot_footers(snapshot, selected_theme, body_width)
    body = (
        Group(table, Rule(style=HEADER_INNER_RULE_STYLE), *footers)
        if footers
        else Group(table)
    )

    title = Text(
        "[ Nymeria ]",
        style=HEADER_LABEL_STYLE,
    )
    state.console.print()
    state.console.print(
        Panel(
            body,
            title=title,
            title_align="center",
            box=box.SQUARE,
            border_style=HEADER_BORDER_STYLE,
            padding=(0, 1),
            width=width,
        )
    )
    state.console.print()


def _snapshot_sections(
    snapshot: CLIHeaderSnapshot,
    theme: CLITheme,
    column_widths: tuple[int, ...],
) -> list[Text]:
    widths = _section_width_cycle(column_widths)
    sections = [
        _section(
            "Thread",
            (
                snapshot.thread_title,
                _join_parts(
                    snapshot.short_thread_id,
                    snapshot.platform,
                    "pinned" if snapshot.pinned else "",
                    _callable_status(snapshot),
                ),
            ),
            slot="user_header",
            theme=theme,
            width=next(widths),
        ),
        _section(
            "Model",
            (
                _provider_api_line(snapshot),
                _model_thinking_line(snapshot),
            ),
            slot="assistant_header",
            theme=theme,
            width=next(widths),
        ),
        _section(
            "Context",
            _context_lines(snapshot),
            slot="thinking",
            theme=theme,
            width=next(widths),
        ),
        _section(
            "Tools",
            (
                f"tools {snapshot.tool_count}  mcp {snapshot.mcp_tool_count}",
                f"callable {snapshot.callable_tool_count}",
            ),
            slot="tool",
            theme=theme,
            width=next(widths),
        ),
        _section(
            "Skills",
            (
                f"skills {snapshot.skill_count}  kits {snapshot.skill_kit_count}",
            ),
            slot="artifact",
            theme=theme,
            width=next(widths),
        ),
        _section(
            "Autonomous",
            (
                _label_summary("Todos", snapshot.todo_labels, snapshot.todo_count),
                _label_summary(
                    "Triggers",
                    snapshot.trigger_labels,
                    snapshot.trigger_count,
                ),
            ),
            slot="prompt_busy",
            theme=theme,
            width=next(widths),
        ),
        _section(
            "Backend",
            _backend_lines(snapshot),
            slot="status_accent" if snapshot.health.status != "error" else "error",
            theme=theme,
            width=next(widths),
        ),
    ]
    return sections


def _snapshot_footers(
    snapshot: CLIHeaderSnapshot,
    theme: CLITheme,
    body_width: int,
) -> list[Text]:
    footers: list[Text] = []
    flags = _flags_text(snapshot.flags, max(1, body_width - 9))
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
                "Header",
                f"partial: {', '.join(snapshot.failures)}",
                slot="error",
                theme=theme,
                width=body_width,
            )
        )
    return footers


def _sections_table(
    sections: list[Text],
    column_specs: tuple[tuple[int, float], ...],
) -> Table:
    table = Table(
        box=box.MINIMAL,
        border_style=HEADER_INNER_RULE_STYLE,
        expand=True,
        padding=(0, 1),
        show_edge=False,
        show_header=False,
        show_lines=True,
    )
    columns = len(column_specs)
    for _width, ratio in column_specs:
        table.add_column(
            min_width=14,
            no_wrap=True,
            overflow="ellipsis",
            ratio=round(ratio * 100),
        )
    for index in range(0, len(sections), columns):
        row = list(sections[index : index + columns])
        row.extend(Text("") for _ in range(columns - len(row)))
        table.add_row(*row)
    return table


def _section(
    title: str,
    lines: tuple[str, ...],
    *,
    slot: str,
    theme: CLITheme,
    width: int,
) -> Text:
    text = Text()
    text.append(
        _fit(title.upper(), width),
        style=_section_header_style(theme, fallback_slot=slot),
    )
    for line in lines:
        if not line:
            continue
        text.append("\n")
        text.append(_fit(line, width), style=HEADER_VALUE_STYLE)
    return text


def _footer(
    label: str,
    value: str,
    *,
    slot: str,
    theme: CLITheme,
    width: int,
) -> Text:
    label_text = f"{label.upper()}  "
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


def _section_columns(body_width: int) -> int:
    if body_width >= 116:
        return 3
    if body_width >= 56:
        return 2
    return 1


def _section_column_specs(
    body_width: int, columns: int
) -> tuple[tuple[int, float], ...]:
    ratios = _section_column_ratios(columns)
    available = max(columns * 14, body_width - (columns * 2) - (columns - 1))
    total_ratio = sum(ratios)
    widths = [max(14, int(available * ratio / total_ratio)) for ratio in ratios]
    while sum(widths) > available:
        widest = max(range(columns), key=widths.__getitem__)
        if widths[widest] <= 14:
            break
        widths[widest] -= 1
    remainder = available - sum(widths)
    for index in sorted(range(columns), key=ratios.__getitem__, reverse=True):
        if remainder <= 0:
            break
        widths[index] += 1
        remainder -= 1
    return tuple((width, ratio) for width, ratio in zip(widths, ratios))


def _section_column_ratios(columns: int) -> tuple[float, ...]:
    if columns == 3:
        return (1.05, 1.2, 1.05)
    return tuple(1.0 for _ in range(columns))


def _section_width_cycle(column_widths: tuple[int, ...]):
    while True:
        yield from column_widths


def _callable_status(snapshot: CLIHeaderSnapshot) -> str:
    if not snapshot.callable:
        return ""
    parts = ["callable"]
    if snapshot.callable_name:
        parts.append(snapshot.callable_name)
    if snapshot.callable_team:
        parts.append(f"team {snapshot.callable_team}")
    return " ".join(parts)


def _context_lines(snapshot: CLIHeaderSnapshot) -> tuple[str, ...]:
    tokens = _count_text(snapshot.context_tokens)
    limit = _count_text(snapshot.context_limit)
    parts = []
    if tokens and limit:
        parts.append(f"{tokens} / {limit}")
    elif tokens:
        parts.append(tokens)
    if snapshot.context_percent is not None:
        parts.append(f"({snapshot.context_percent:.0f}%)")
    lines = [" ".join(parts) if parts else "unknown"]
    if snapshot.compaction_count is not None:
        lines.append(f"compactions {snapshot.compaction_count}")
    return tuple(lines)


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


def _label_summary(label: str, labels: tuple[str, ...], count: int) -> str:
    if count <= 0:
        return f"{label}: none"
    parts = list(labels)
    remaining = count - len(parts)
    if remaining > 0:
        parts.append(f"+{remaining} more")
    if not parts:
        parts.append(f"{count} active")
    return f"{label}: {', '.join(parts)}"


def _backend_lines(snapshot: CLIHeaderSnapshot) -> tuple[str, ...]:
    lines = [snapshot.backend_url or "local"]
    parts = [snapshot.health.label, f"user {snapshot.user_id}"]
    if snapshot.health.message:
        parts.append(snapshot.health.message)
    lines.append(_join_parts(*parts))
    return tuple(line for line in lines if line)


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
