"""Reusable searchable list widget for the setup wizard.

`SearchableList` pairs a search `Input` with a Textual `OptionList`: typing
filters the list, arrows navigate it, and Enter is left to the parent step's
priority binding (so a single Enter both commits the highlight and advances,
matching how the radio steps behave). Both the provider picker and the model
picker use it.

The filtering core (`filter_items`) is a pure function so it can be unit-tested
without a running app.
"""

from __future__ import annotations

from dataclasses import dataclass

from rich.text import Text
from textual.app import ComposeResult
from textual.events import Key
from textual.message import Message
from textual.widget import Widget
from textual.widgets import Input, OptionList
from textual.widgets.option_list import Option


@dataclass(frozen=True)
class ListItem:
    """One row. `is_header` rows are non-selectable group labels (e.g. a tier)."""

    value: str
    primary: str
    secondary: str = ""
    is_header: bool = False
    search_text: str = ""

    def haystack(self) -> str:
        return (self.search_text or f"{self.primary} {self.value}").lower()


def filter_items(items: list[ListItem], query: str) -> list[ListItem]:
    """Case-insensitive substring filter over selectable rows.

    A header row is kept only when its group still has at least one visible child.
    An empty query returns everything unchanged.
    """
    text = (query or "").strip().lower()
    if not text:
        return list(items)

    result: list[ListItem] = []
    pending_header: ListItem | None = None
    header_emitted = False
    for item in items:
        if item.is_header:
            pending_header = item
            header_emitted = False
            continue
        if text in item.haystack():
            if pending_header is not None and not header_emitted:
                result.append(pending_header)
                header_emitted = True
            result.append(item)
    return result


def _header_prompt(label: str) -> Text:
    return Text(label.upper(), style="bold #bbddfb")


def _row_prompt(item: ListItem) -> Text:
    prompt = Text(item.primary)
    if item.secondary:
        prompt.append(f"  {item.secondary}", style="#8a93a3")
    return prompt


class PickerOptionList(OptionList):
    """The picker's `OptionList`, with two wizard-specific tweaks over the stock list.

    1. Up on the first selectable row hands focus back to the search box (posting
       `LeaveTop`) instead of wrapping to the bottom, so the arrow keys alone can
       travel between the filter field and the list.
    2. Whenever the first selectable row becomes current, the list scrolls fully
       to the top. The only thing above that row is the leading group header, a
       disabled option the highlight can never land on, so the stock
       scroll-to-highlight never brings it back once it has scrolled off the top.

    A selectable row carries a non-None `id`; headers (and the empty-state row)
    do not, matching the convention used throughout `SearchableList`.
    """

    class LeaveTop(Message):
        """Posted when up is pressed while the first selectable row is current."""

    def _first_selectable_index(self) -> int | None:
        for index in range(self.option_count):
            if self.get_option_at_index(index).id is not None:
                return index
        return None

    def action_cursor_up(self) -> None:
        first = self._first_selectable_index()
        if first is not None and self.highlighted is not None and self.highlighted > first:
            super().action_cursor_up()
            return
        # At (or above) the first selectable row: instead of wrapping to the
        # bottom, return focus to the search box.
        self.post_message(self.LeaveTop())

    def watch_highlighted(self, highlighted: int | None) -> None:
        super().watch_highlighted(highlighted)
        if highlighted is not None and highlighted == self._first_selectable_index():
            # Reveal the leading group header sitting just above this row.
            self.scroll_home(animate=False)


class SearchableList(Widget):
    """A search box over a filtered OptionList. See module docstring."""

    DEFAULT_CSS = """
    SearchableList {
        layout: vertical;
        height: auto;
    }
    SearchableList > Input {
        margin: 0 0 1 0;
    }
    SearchableList > OptionList {
        height: auto;
        max-height: 14;
    }
    """

    class Selected(Message):
        """Posted when a row is chosen (mouse click)."""

        def __init__(self, value: str) -> None:
            self.value = value
            super().__init__()

    class Highlighted(Message):
        """Posted whenever the highlighted row changes (value is None when empty)."""

        def __init__(self, value: str | None) -> None:
            self.value = value
            super().__init__()

    def __init__(
        self,
        items: list[ListItem],
        *,
        placeholder: str = "Type to filter...",
        initial_value: str | None = None,
        search_id: str = "search",
        list_id: str = "options",
        empty_text: str = "No matches - your text is used as-is",
        **kwargs,
    ) -> None:
        super().__init__(**kwargs)
        self._items = list(items)
        self._placeholder = placeholder
        self._initial_value = initial_value
        self._search_id = search_id
        self._list_id = list_id
        self._empty_text = empty_text

    def compose(self) -> ComposeResult:
        yield Input(placeholder=self._placeholder, id=self._search_id)
        yield PickerOptionList(id=self._list_id)

    def on_mount(self) -> None:
        self._populate(self._items)
        self._highlight_value(self._initial_value)
        self._input.focus()

    # --- public API ---------------------------------------------------------

    @property
    def search_value(self) -> str:
        return self._input.value.strip()

    @property
    def selected_value(self) -> str | None:
        """The id of the highlighted selectable row, or None (header/empty)."""
        option_list = self._option_list
        index = option_list.highlighted
        if index is None:
            return None
        try:
            return option_list.get_option_at_index(index).id
        except Exception:
            return None

    def set_items(self, items: list[ListItem]) -> None:
        """Replace the items (e.g. after an async fetch) and re-apply the filter."""
        self._items = list(items)
        self._populate(filter_items(self._items, self._input.value))
        self._highlight_value(self._initial_value)

    def set_placeholder(self, placeholder: str) -> None:
        self._input.placeholder = placeholder

    def focus(self, scroll_visible: bool = True) -> "SearchableList":
        try:
            self._input.focus(scroll_visible)
        except Exception:
            pass
        return self

    # --- internals ----------------------------------------------------------

    @property
    def _input(self) -> Input:
        return self.query_one(f"#{self._search_id}", Input)

    @property
    def _option_list(self) -> PickerOptionList:
        return self.query_one(f"#{self._list_id}", PickerOptionList)

    def _populate(self, items: list[ListItem]) -> None:
        option_list = self._option_list
        option_list.clear_options()
        options: list[Option] = []
        for item in items:
            if item.is_header:
                options.append(Option(_header_prompt(item.primary), disabled=True))
            else:
                options.append(Option(_row_prompt(item), id=item.value))
        if not options:
            options.append(Option(self._empty_text, disabled=True))
        option_list.add_options(options)

    def _highlight_value(self, value: str | None) -> None:
        option_list = self._option_list
        if value:
            for index in range(option_list.option_count):
                if option_list.get_option_at_index(index).id == value:
                    option_list.highlighted = index
                    return
        self._highlight_first_selectable()

    def _highlight_first_selectable(self) -> None:
        option_list = self._option_list
        for index in range(option_list.option_count):
            if option_list.get_option_at_index(index).id is not None:
                option_list.highlighted = index
                return
        option_list.highlighted = None

    # --- events -------------------------------------------------------------

    def on_input_changed(self, event: Input.Changed) -> None:
        if event.input.id != self._search_id:
            return
        event.stop()
        self._populate(filter_items(self._items, event.value))
        self._highlight_first_selectable()

    def on_option_list_option_selected(self, event: OptionList.OptionSelected) -> None:
        event.stop()
        if event.option.id is not None:
            self.post_message(self.Selected(event.option.id))

    def on_option_list_option_highlighted(
        self, event: OptionList.OptionHighlighted
    ) -> None:
        event.stop()
        option_id = event.option.id if event.option is not None else None
        self.post_message(self.Highlighted(option_id))

    def on_picker_option_list_leave_top(self, event: PickerOptionList.LeaveTop) -> None:
        # Up at the top of the list returns focus to the search box.
        event.stop()
        self._input.focus()

    def on_key(self, event: Key) -> None:
        # Down from the search box jumps into the list; the OptionList then owns
        # arrow navigation. Enter is intentionally left to the step's priority
        # binding, so it is not handled here.
        if event.key in ("down", "pagedown") and self.app.focused is self._input:
            option_list = self._option_list
            if option_list.option_count:
                option_list.focus()
                event.stop()
                event.prevent_default()


__all__ = ["ListItem", "SearchableList", "filter_items"]
