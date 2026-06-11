"""Agent-tuning catalog for the init flow: context, limits, and LLM tuning.

TUI-free (like ``setup/voice_catalog.py`` and ``setup/rag_catalog.py``) so the
interactive wizard, the headless finalize path, and tests share one source of
truth. Three sections ride here:

- ``context`` step: the context-management strategy (token-triggered
  auto-compaction, percentage-triggered, sliding window, or none) plus the
  matching trigger value.
- ``agent_limits`` step: memory caps, timezone, tool output cap, tool timeout.
- ``llm_tuning`` step: reasoning effort plus sampling knobs (temperature, max
  output tokens, top_p, top_k).

Every numeric field is optional: a blank input writes nothing, so the
settings default (or an existing hand-set env line) stays in charge. Values
the user types are stored in ``state.extras`` as raw strings and validated
both at collect time (focus + error in the TUI) and again here at env
production (flags and hydrated values take the same path).
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from typing import TYPE_CHECKING, Optional

if TYPE_CHECKING:
    from .state import WizardState


@dataclass(frozen=True)
class TuningChoice:
    value: str
    label: str
    description: str


@dataclass(frozen=True)
class TuningField:
    """One optional input field: extras key, env var, parsing and bounds."""

    key: str  # state.extras key (raw string as typed)
    env_var: str
    label: str
    placeholder: str
    kind: str  # "int" | "percent" | "float" | "timezone"
    minimum: float | None = None
    maximum: float | None = None


# --- context step -------------------------------------------------------------

CONTEXT_CHOICES: tuple[TuningChoice, ...] = (
    TuningChoice(
        "compact_tokens",
        "Auto-compact at a token count (recommended)",
        "Summarize the conversation when it reaches an absolute token count. "
        "Stays put when you switch models with different context windows.",
    ),
    TuningChoice(
        "compact_percent",
        "Auto-compact at a percentage",
        "Summarize when the conversation fills this share of the model's "
        "context window. 30-80% recommended.",
    ),
    TuningChoice(
        "sliding_window",
        "Sliding window",
        "No summaries: silently drop the oldest turns, keeping the last N "
        "user-message cycles.",
    ),
    TuningChoice(
        "none",
        "No context management",
        "Never compact or trim. Long threads will eventually overflow the "
        "model's context window and error.",
    ),
)

CONTEXT_VALUES = frozenset(choice.value for choice in CONTEXT_CHOICES)
_CONTEXT_LABELS = {choice.value: choice.label for choice in CONTEXT_CHOICES}

# Per-strategy trigger field (shown only for the matching strategy).
CONTEXT_FIELDS: dict[str, TuningField] = {
    "compact_tokens": TuningField(
        key="compact_threshold_tokens",
        env_var="COMPACT_THRESHOLD_TOKENS",
        label="Trigger token count",
        placeholder="200000 (recommended)",
        kind="int",
        minimum=1_000,
        maximum=2_000_000,
    ),
    "compact_percent": TuningField(
        key="compact_threshold_percent",
        env_var="COMPACT_THRESHOLD",
        label="Trigger at percent full (5-95)",
        placeholder="80 (30-80 recommended)",
        kind="percent",
        minimum=5,
        maximum=95,
    ),
    "sliding_window": TuningField(
        key="sliding_window_cycles",
        env_var="SLIDING_WINDOW_CYCLES",
        label="User-message cycles to keep",
        placeholder="5 (default)",
        kind="int",
        minimum=1,
        maximum=50,
    ),
}

# Strategy value -> the CONTEXT_MANAGEMENT / COMPACT_THRESHOLD_MODE lines.
_CONTEXT_ENV: dict[str, dict[str, str]] = {
    "compact_tokens": {
        "CONTEXT_MANAGEMENT": "auto_compact",
        "COMPACT_THRESHOLD_MODE": "tokens",
    },
    "compact_percent": {
        "CONTEXT_MANAGEMENT": "auto_compact",
        "COMPACT_THRESHOLD_MODE": "percentage",
    },
    "sliding_window": {"CONTEXT_MANAGEMENT": "sliding_window"},
    "none": {"CONTEXT_MANAGEMENT": "none"},
}


# --- agent_limits step ---------------------------------------------------------

LIMIT_FIELDS: tuple[TuningField, ...] = (
    TuningField(
        key="user_timezone",
        env_var="USER_TIMEZONE",
        label="Your timezone (IANA name)",
        placeholder="UTC (default), e.g. Australia/Sydney",
        kind="timezone",
    ),
    TuningField(
        key="memory_max_entries",
        env_var="MEMORY_MAX_ENTRIES",
        label="Max saved memories (key-value facts)",
        placeholder="100 (default)",
        kind="int",
        minimum=1,
        maximum=10_000,
    ),
    TuningField(
        key="memory_char_limit",
        env_var="MEMORY_CHAR_LIMIT",
        label="Memory/notepad character budget",
        placeholder="8000 (default)",
        kind="int",
        minimum=1,
        maximum=2_000_000,
    ),
    TuningField(
        key="memory_value_max_chars",
        env_var="MEMORY_VALUE_MAX_CHARS",
        label="Max characters per saved memory",
        placeholder="1000 (default)",
        kind="int",
        minimum=50,
        maximum=100_000,
    ),
    TuningField(
        key="tool_output_max_chars",
        env_var="TOOL_OUTPUT_MAX_CHARS",
        label="Max characters kept per tool result",
        placeholder="100000 (default)",
        kind="int",
        minimum=1_000,
        maximum=2_000_000,
    ),
    TuningField(
        key="tool_timeout",
        env_var="TOOL_TIMEOUT",
        label="Tool timeout (seconds)",
        placeholder="300 (default)",
        kind="int",
        minimum=30,
        maximum=900,
    ),
)


# --- llm_tuning step -----------------------------------------------------------

RECOMMENDED_EFFORT = "medium"

# Value, plain label, core description, and the generic clamp prose. The plain
# label and core description are shared by both rendering modes; the generic
# prose appears only when the wizard cannot resolve the active model (the
# model-aware path replaces it with the actual clamp target per level).
_EFFORT_BASE: tuple[tuple[str, str, str, str], ...] = (
    (
        "off",
        "Off",
        "No extended thinking. Fastest and cheapest; fine for chat and "
        "simple tool use.",
        "Models that cannot fully disable thinking use their lowest level.",
    ),
    (
        "low",
        "Low",
        "A little thinking for harder requests.",
        "",
    ),
    (
        "medium",
        "Medium",
        "Balanced thinking on capable models. Good default for an assistant "
        "that plans and uses tools.",
        "",
    ),
    (
        "high",
        "High",
        "Thorough reasoning; slower and more tokens.",
        "",
    ),
    (
        "xhigh",
        "Extra high",
        "Maximum-depth reasoning on frontier models.",
        "Models without this level fall back to their highest supported "
        "level automatically.",
    ),
    (
        "max",
        "Max",
        "Unconstrained thinking where the provider supports it.",
        "Models without it fall back to their highest supported level "
        "automatically.",
    ),
)

_EFFORT_PLAIN_LABELS = {value: plain for value, plain, _, _ in _EFFORT_BASE}


def _effort_choice_label(value: str, plain: str) -> str:
    return f"{plain} (recommended)" if value == RECOMMENDED_EFFORT else plain


EFFORT_CHOICES: tuple[TuningChoice, ...] = tuple(
    TuningChoice(
        value,
        _effort_choice_label(value, plain),
        f"{core} {generic}" if generic else core,
    )
    for value, plain, core, generic in _EFFORT_BASE
)

EFFORT_VALUES = frozenset(choice.value for choice in EFFORT_CHOICES)
_EFFORT_LABELS = {choice.value: choice.label for choice in EFFORT_CHOICES}

SAMPLING_FIELDS: tuple[TuningField, ...] = (
    TuningField(
        key="llm_temperature",
        env_var="LLM_TEMPERATURE",
        label="Temperature (0-2)",
        placeholder="1.0 (provider default)",
        kind="float",
        minimum=0.0,
        maximum=2.0,
    ),
    TuningField(
        key="llm_max_tokens",
        env_var="LLM_MAX_TOKENS",
        label="Max output tokens",
        placeholder="blank = provider default",
        kind="int",
        minimum=1,
        maximum=1_000_000,
    ),
    TuningField(
        key="llm_top_p",
        env_var="LLM_TOP_P",
        label="Top-p (0-1)",
        placeholder="blank = provider default",
        kind="float",
        minimum=0.0,
        maximum=1.0,
    ),
    TuningField(
        key="llm_top_k",
        env_var="LLM_TOP_K",
        label="Top-k (1-100)",
        placeholder="blank = provider default",
        kind="int",
        minimum=1,
        maximum=100,
    ),
)

ALL_FIELDS: tuple[TuningField, ...] = (
    tuple(CONTEXT_FIELDS.values()) + LIMIT_FIELDS + SAMPLING_FIELDS
)


# --- parsing -------------------------------------------------------------------


def parse_field(field: TuningField, raw: str) -> tuple[Optional[str], Optional[str]]:
    """Parse one typed value. Returns (env_value, error); blank -> (None, None)."""
    text = (raw or "").strip()
    if not text:
        return None, None
    if field.kind == "timezone":
        try:
            from zoneinfo import ZoneInfo

            ZoneInfo(text)
        except Exception:  # noqa: BLE001 (ZoneInfoNotFoundError, ValueError, ...)
            return None, (
                f"{field.label}: unknown timezone. Use an IANA name like "
                "Australia/Sydney or UTC."
            )
        return text, None
    cleaned = text.replace(",", "").replace("_", "").rstrip("%")
    try:
        number = float(cleaned)
    except ValueError:
        return None, f"{field.label}: enter a number."
    if not math.isfinite(number):
        return None, f"{field.label}: enter a number."
    if field.kind in ("int", "percent") and number != int(number):
        return None, f"{field.label}: enter a whole number."
    if field.minimum is not None and number < field.minimum:
        return None, f"{field.label}: minimum is {field.minimum:g}."
    if field.maximum is not None and number > field.maximum:
        return None, f"{field.label}: maximum is {field.maximum:g}."
    if field.kind == "percent":
        return f"{int(number) / 100:g}", None
    if field.kind == "int":
        return str(int(number)), None
    return f"{number:g}", None


def field_error(state: "WizardState", field: TuningField) -> Optional[str]:
    """Validation error for the stored extras value, or None."""
    raw = state.extras.get(field.key)
    if not isinstance(raw, str):
        return None
    _, error = parse_field(field, raw)
    return error


# --- selections and env production ----------------------------------------------


def selected_context(state: "WizardState") -> Optional[str]:
    """The context pick, or None when the step was never reached."""
    value = state.extras.get("context_strategy")
    return value if isinstance(value, str) and value in CONTEXT_VALUES else None


def selected_effort(state: "WizardState") -> Optional[str]:
    """The effort pick, or None when the step was never reached."""
    value = state.extras.get("llm_effort")
    return value if isinstance(value, str) and value in EFFORT_VALUES else None


def context_label(value: str) -> str:
    return _CONTEXT_LABELS.get(value, value)


def effort_label(value: str) -> str:
    return _EFFORT_LABELS.get(value, value)


def plain_effort_label(value: str) -> str:
    """The choice label without the "(recommended)" marker, for clamp notes."""
    return _EFFORT_PLAIN_LABELS.get(value, value)


# --- model-aware effort annotations ----------------------------------------------


def _effort_provider_model(state: "WizardState") -> Optional[tuple[str, str]]:
    """The (provider, model) pair finalize will write, or None when unknown.

    Mirrors finalize so the wizard's clamp preview matches the runtime clamp
    in ``core/agent_llm_config.py``: the API-key path falls back to the
    registry spec's default model, and the first-run CLIProxy branch derives
    both from the catalog spec (``_apply_cliproxy_route`` fills state.provider
    only at finalize time). Hydrated reconfigures already carry the written
    values. None means "show the generic catalog text instead".
    """
    provider = (state.provider or "").strip()
    model = (state.model or "").strip()
    if state.auth_method_is_cliproxy() and state.cliproxy_provider:
        try:
            from ..cliproxy.catalog import get_cliproxy_provider

            cspec = get_cliproxy_provider(state.cliproxy_provider)
        except Exception:  # noqa: BLE001 (annotations are a hint, fail open)
            cspec = None
        if cspec is not None:
            # _apply_cliproxy_route overwrites the provider unconditionally
            # and fills the model only when the step left it unset; a stale
            # hydrated provider must not win over the active branch.
            provider = cspec.nymeria_provider
            model = model or (cspec.default_model or "").strip()
    if provider and not model:
        spec = state.provider_spec()
        model = ((spec.default_model if spec else None) or "").strip()
    if not model:
        return None
    return provider, model


def _effort_capabilities(state: "WizardState") -> Optional[tuple[str, str, tuple]]:
    """(provider, model, supported ladder) for the configured model, or None.

    The lookup is offline-safe (`supported_reasoning_efforts` never fetches)
    and omits provider_route: the wizard does not write LLM_PROVIDER_ROUTE,
    so the runtime resolves the same registry default. Ladders come from the
    static capability tables; the live provider-published ladders the runtime
    may merge at startup are not populated in the wizard process, so the
    annotations are a preview, not a guarantee.
    """
    resolved = _effort_provider_model(state)
    if resolved is None:
        return None
    provider, model = resolved
    try:
        from ..config.model_capabilities import supported_reasoning_efforts

        levels = supported_reasoning_efforts(provider, model)
    except Exception:  # noqa: BLE001 (fail open)
        return None
    if not levels:
        return None
    return provider, model, levels


def effective_effort_for_state(state: "WizardState", effort: str) -> Optional[str]:
    """The post-clamp level the runtime will run `effort` at, or None."""
    resolved = _effort_provider_model(state)
    if resolved is None:
        return None
    provider, model = resolved
    try:
        from ..config.model_capabilities import clamp_reasoning_effort

        return clamp_reasoning_effort(provider, model, effort)
    except Exception:  # noqa: BLE001 (fail open)
        return None


def effort_ladder_note(state: "WizardState") -> Optional[str]:
    """One-line supported-levels note for the tuning screen, or None."""
    caps = _effort_capabilities(state)
    if caps is None:
        return None
    _, model, levels = caps
    supported = ", ".join(plain_effort_label(level) for level in levels)
    return f"{model} supports {supported}."


def annotated_effort_choices(state: "WizardState") -> tuple[TuningChoice, ...]:
    """EFFORT_CHOICES annotated with the configured model's clamp targets.

    Levels the model clamps gain a "(runs at X)" label suffix and a specific
    description sentence; supported levels drop the generic clamp prose. All
    six levels stay selectable (clamping is server-side; the note is a hint,
    matching the frontend dropdowns). An unresolvable model or a failed
    lookup returns EFFORT_CHOICES unchanged.
    """
    caps = _effort_capabilities(state)
    if caps is None:
        return EFFORT_CHOICES
    provider, model, levels = caps
    choices: list[TuningChoice] = []
    for value, plain, core, generic in _EFFORT_BASE:
        marks = ["recommended"] if value == RECOMMENDED_EFFORT else []
        description = core
        if value not in levels:
            try:
                from ..config.model_capabilities import clamp_reasoning_effort

                effective = clamp_reasoning_effort(provider, model, value)
            except Exception:  # noqa: BLE001 (fail open)
                effective = None
            if effective and effective != value:
                target = plain_effort_label(effective)
                marks.append(f"runs at {target}")
                if value == "off":
                    description = (
                        f"{core} {model} cannot disable thinking; "
                        f"runs at {target}."
                    )
                else:
                    description = (
                        f"{core} Not supported by {model}; runs at {target}."
                    )
            elif generic:
                description = f"{core} {generic}"
        label = f"{plain} ({', '.join(marks)})" if marks else plain
        choices.append(TuningChoice(value, label, description))
    return tuple(choices)


def _field_env(state: "WizardState", field: TuningField) -> dict[str, str]:
    raw = state.extras.get(field.key)
    if not isinstance(raw, str):
        return {}
    value, error = parse_field(field, raw)
    if error or value is None:
        return {}
    return {field.env_var: value}


def tuning_env_for_state(state: "WizardState") -> dict[str, str]:
    """Env lines for the three tuning sections; empty for untouched steps.

    Only the trigger field matching the chosen strategy is written, so a
    leftover number for an abandoned strategy never lands in the file. Stale
    lines are retired separately by :func:`tuning_drop_env` (a stale
    COMPACT_THRESHOLD is not fully inert: overflow recovery and CLI displays
    read it regardless of the active mode).
    """
    out: dict[str, str] = {}
    context = selected_context(state)
    if context is not None:
        out.update(_CONTEXT_ENV[context])
        trigger = CONTEXT_FIELDS.get(context)
        if trigger is not None:
            out.update(_field_env(state, trigger))
    for field in LIMIT_FIELDS:
        out.update(_field_env(state, field))
    effort = selected_effort(state)
    if effort is not None:
        out["LLM_REASONING_EFFORT"] = effort
        for field in SAMPLING_FIELDS:
            out.update(_field_env(state, field))
    return out


def tuning_drop_env(state: "WizardState") -> tuple[str, ...]:
    """Stale tuning lines to retire on this run.

    Two cases. (1) A field hydrated from disk that the user cleared: blank
    means "back to the default shown", so the on-disk line is retired
    (hydrate records the seeded keys under ``extras["tuning_on_disk_keys"]``;
    a key that later vanished from extras was explicitly blanked, because an
    unvisited step never pops it). (2) A context-strategy pick retires the
    OTHER strategies' trigger lines: a stale COMPACT_THRESHOLD changes
    overflow-recovery sizing and CLI displays even in tokens mode. Values
    produced by this run always win over drops inside write_config.
    """
    drops: list[str] = []
    seeded = state.extras.get("tuning_on_disk_keys")
    seeded_keys = set(seeded) if isinstance(seeded, (list, tuple, set)) else set()
    for field in ALL_FIELDS:
        if field.key in seeded_keys and not isinstance(
            state.extras.get(field.key), str
        ):
            drops.append(field.env_var)
    context = selected_context(state)
    if context is not None:
        active = CONTEXT_FIELDS.get(context)
        for field in CONTEXT_FIELDS.values():
            if active is None or field.env_var != active.env_var:
                drops.append(field.env_var)
    return tuple(dict.fromkeys(drops))


def tuning_extras_from_env(get) -> dict[str, str]:
    """Reverse-map on-disk env lines to wizard extras (raw input strings).

    ``get`` takes an env var name and returns its on-disk value or None.
    Hand-edited values that no longer parse are skipped (treated as not
    recorded), mirroring the voice hydration. Used with ``setdefault`` so
    explicit CLI flags win over disk.
    """
    out: dict[str, str] = {}
    management = (get("CONTEXT_MANAGEMENT") or "").strip()
    mode = (get("COMPACT_THRESHOLD_MODE") or "").strip()
    if management == "auto_compact":
        out["context_strategy"] = (
            "compact_percent" if mode == "percentage" else "compact_tokens"
        )
    elif management in ("sliding_window", "none"):
        out["context_strategy"] = management
    threshold = (get("COMPACT_THRESHOLD") or "").strip()
    if threshold:
        try:
            value = float(threshold)
        except ValueError:
            value = None
        if value is not None and math.isfinite(value):
            percent = round(value * 100)
            # Only seed values the wizard can re-produce exactly: a hand-set
            # 0.333 must not be silently rewritten as 0.33 on reconfigure.
            if 5 <= percent <= 95 and abs(value * 100 - percent) < 1e-9:
                out["compact_threshold_percent"] = str(percent)
    effort = (get("LLM_REASONING_EFFORT") or "").strip()
    if effort in EFFORT_VALUES:
        out["llm_effort"] = effort
    direct_fields = (
        (CONTEXT_FIELDS["compact_tokens"],)
        + (CONTEXT_FIELDS["sliding_window"],)
        + LIMIT_FIELDS
        + SAMPLING_FIELDS
    )
    for field in direct_fields:
        raw = (get(field.env_var) or "").strip()
        if not raw:
            continue
        value, error = parse_field(field, raw)
        if error or value is None:
            continue
        out[field.key] = raw
    return out


def tuning_summary_lines(state: "WizardState") -> list[str]:
    """Review-screen lines for whatever tuning the user actually set."""
    lines: list[str] = []
    context = selected_context(state)
    if context is not None:
        detail = ""
        trigger = CONTEXT_FIELDS.get(context)
        if trigger is not None:
            env = _field_env(state, trigger)
            if env:
                detail = f" ({trigger.env_var}={env[trigger.env_var]})"
        lines.append(f"Context: {context_label(context)}{detail}")
    limit_bits = []
    for field in LIMIT_FIELDS:
        env = _field_env(state, field)
        if env:
            limit_bits.append(f"{field.env_var}={env[field.env_var]}")
    if limit_bits:
        lines.append(f"Agent limits: {', '.join(limit_bits)}")
    effort = selected_effort(state)
    if effort is not None:
        effective = effective_effort_for_state(state, effort)
        clamp = ""
        if effective and effective != effort:
            clamp = f" (runs at {plain_effort_label(effective)})"
        sampling_bits = []
        for field in SAMPLING_FIELDS:
            env = _field_env(state, field)
            if env:
                sampling_bits.append(f"{field.env_var}={env[field.env_var]}")
        suffix = f" ({', '.join(sampling_bits)})" if sampling_bits else ""
        lines.append(f"Reasoning effort: {effort_label(effort)}{clamp}{suffix}")
    return lines


__all__ = [
    "ALL_FIELDS",
    "CONTEXT_CHOICES",
    "CONTEXT_FIELDS",
    "CONTEXT_VALUES",
    "EFFORT_CHOICES",
    "EFFORT_VALUES",
    "LIMIT_FIELDS",
    "RECOMMENDED_EFFORT",
    "SAMPLING_FIELDS",
    "TuningChoice",
    "TuningField",
    "annotated_effort_choices",
    "context_label",
    "effective_effort_for_state",
    "effort_label",
    "effort_ladder_note",
    "field_error",
    "parse_field",
    "plain_effort_label",
    "selected_context",
    "selected_effort",
    "tuning_drop_env",
    "tuning_env_for_state",
    "tuning_extras_from_env",
    "tuning_summary_lines",
]
