"""LLM-domain command bodies for the central command service.

House style: new backend handler families live in their own domain mixin
module rather than being appended to the ~5,000-line ``command_service.py``.
``_CommandExecutor`` inherits :class:`LLMCommandsMixin`, which owns the
``/fallback`` chain command, ``/think`` (catalog aliases ``/reasoning`` and
``/thinking``; scope-aware since the 2026-07 config-group migration: with an
active thread it writes that thread's llm_config, an explicit trailing
``global``/``thread`` token overrides), and the ``/provider`` family ported
from the retired CLI ``triggers/cli/commands/provider.py`` module (minus the
CLI-local credential file, which was retired with it: provider secrets live
in backend settings/env and the credential vault). The chained ``/provider
setup`` flow lives in the sibling ``command_executor_provider_setup.py``
mixin and the ``/provider cliproxy`` OAuth chain in
``command_executor_cliproxy.py``. Handler methods are resolved by
``CommandService.execute`` via ``getattr(executor, "_cmd_<path>")``.

Nothing is imported from ``command_service`` here, so the module stays a
runtime leaf with no import cycle (``command_service`` imports this module,
not the reverse).
"""

from __future__ import annotations

import logging
from collections.abc import Mapping
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from ..config.llm_providers import LLMProviderSpec

from .command_forms import (
    CommandOutput,
    command_data,
    form_option,
    form_payload,
    form_tab,
    radio_field,
    search_field,
    text_field,
)

logger = logging.getLogger(__name__)


# ── Provider catalog (ported from the retired CLI provider command) ──────────

# Maps /provider set credential fields to backend settings keys. Adding a
# provider here requires the matching fields on the /settings model. Only
# these providers have credential-bearing settings fields, so /provider set
# (and the rich credential status) stays scoped to them; switch and test
# accept ANY registered provider spec.
PROVIDER_SECRET_SETTINGS = {
    "anthropic": {
        "api_key": "anthropic_api_key",
        "direct_api_key": "anthropic_direct_api_key",
    },
    "openai": {"api_key": "openai_api_key"},
    "openrouter": {"api_key": "openrouter_api_key"},
}

OPENAI_API_MODES = {"chat_completions", "responses"}
PROVIDERS = tuple(PROVIDER_SECRET_SETTINGS)

_TIER_BADGES = {
    "native": "[NATIVE]",
    "gateway": "[GATEWAY]",
    "unverified": "[UNVERIFIED]",
}
_TIER_ORDER = ("native", "gateway", "unverified")

_THINK_LEVELS = ("low", "medium", "high", "xhigh", "max")
_THINK_USAGE = (
    "[Error]: Usage: /think [off|on|low|medium|high|xhigh|max] [global|thread]"
)


def custom_model_tab(placeholder: str, submit_command: str) -> dict[str, Any]:
    """The custom-model escape hatch tab shared by the chained model steps
    (LLM-domain copy over the pure ``command_forms`` contract builders)."""

    return form_tab(
        "Model",
        [text_field("model", label="Model id", placeholder=placeholder)],
        submit_command=submit_command,
    )


def model_pick_tab(
    options: list[dict[str, Any]],
    preselect: str,
    preselect_meta: str,
    submit_command: str,
) -> dict[str, Any]:
    """The identical tail of the chained model steps (takes ownership of
    ``options``): surface a held decision missing from the listed set as a
    current row, append the custom escape hatch, build the filter + radio
    tab. Everything upstream of this (data source, cache keying, option
    metas, fallbacks) is deliberately per-chain: notably the setup chain's
    model list doubles as its credential probe, so its cache is
    fingerprint-keyed to the pending credentials, while the cliproxy list
    is credential-free."""

    if preselect and all(option["id"] != preselect for option in options):
        options.insert(
            0, form_option(preselect, meta=preselect_meta, current=True)
        )
    options.append(form_option("custom", label="Custom model id…"))
    return form_tab(
        "Model",
        [
            search_field("filter", placeholder="Filter models…"),
            radio_field("model", options),
        ],
        submit_command=submit_command,
    )


class LLMCommandsMixin:
    """LLM-domain command bodies mixed into ``_CommandExecutor``.

    The host (:class:`nymeria.core.command_service._CommandExecutor`) provides
    ``api``, ``thread_id`` and ``user_id``; the annotations below let the
    static checker see them on the mixin in isolation.
    """

    api: Any
    thread_id: str
    user_id: str
    actor: str
    is_admin: bool | None

    if TYPE_CHECKING:
        def _require_thread(self) -> str | None: ...
        def _usage_error(self, name: str, *, hint: str | None = None) -> str: ...
        def _agent(self) -> Any: ...
        async def _list_threads(self) -> list[Any]: ...

    # ── Fallback chain + consent prompts ──────────────────────────────────

    async def _fallback_thread_allowed(self) -> bool:
        """Owner guard for the thread-scoped fallback surfaces.

        The executor context receives a caller-supplied ``thread_id`` with no
        pre-validated ownership (unlike the REST config path, which runs
        ``_require_thread_access``), so status/revert verify the thread is in
        this user's visible list before reading or clearing its config.
        Admins pass unconditionally, mirroring the REST rule.
        """
        from ..tools.utils import is_admin

        if not self.thread_id:
            return False
        if is_admin(self.user_id, agent=self._agent()):
            return True
        threads = await self._list_threads()
        return any(
            str(t.get("thread_id") or t.get("id") or "") == self.thread_id
            for t in threads
        )

    async def _cmd_fallback(self, args: list[str], rest: str) -> str:
        """Manage the fallback chain (list/add/remove/clear/set).

        The consent subcommands (status/revert/approvals/approve/deny) are
        registered child paths, so the registry's longest-prefix match routes
        them to ``_cmd_fallback_<sub>`` before this parent ever parses; only
        the chain grammar and unknown tokens reach here.
        """
        sub = args[0].lower() if args else "list"

        settings = await self.api.get_settings()
        chain = self._fallback_chain(settings)

        if sub == "list":
            primary = str(settings.get("llm_model", "") or "").strip() or "Unknown"
            provider = str(settings.get("llm_provider", "") or "").strip()
            lines = [f"Primary: {primary}" + (f" ({provider})" if provider else "")]
            if chain:
                lines += [f"  {i}. {model}" for i, model in enumerate(chain, start=1)]
            else:
                lines.append("  (no fallback models configured)")
            return "[Info]: Model Fallbacks\n" + "\n".join(lines)

        if sub == "add":
            model, position, error = self._parse_fallback_add(args[1:])
            if error:
                return f"[Error]: {error}"
            next_chain = [m for m in chain if m != model]
            if position is None:
                next_chain.append(model)
            else:
                next_chain.insert(min(position - 1, len(next_chain)), model)
            return await self._save_fallback_chain(next_chain, f"Added fallback model: {model}")

        if sub in {"remove", "rm"}:
            model = " ".join(args[1:]).strip()
            if not model:
                return "[Error]: Usage: /fallback remove <model-id>"
            next_chain = [m for m in chain if m != model]
            if len(next_chain) == len(chain):
                return f"[Error]: Fallback model is not configured: {model}"
            return await self._save_fallback_chain(next_chain, f"Removed fallback model: {model}")

        if sub == "clear":
            return await self._save_fallback_chain([], "Cleared fallback chain.")

        if sub == "set":
            next_chain = self._dedupe_models(args[1:])
            if not next_chain:
                return "[Error]: Usage: /fallback set <model1> <model2> ..."
            label = " -> ".join(next_chain)
            return await self._save_fallback_chain(next_chain, f"Fallback chain set: {label}")

        return self._usage_error("fallback")

    async def _fallback_status_markdown(self) -> str:
        """The consent modes plus the active thread's fallback hold, if any."""
        settings = await self.api.get_settings()
        lines = [
            f"Switch mode: {settings.get('llm_fallback_switch_mode') or 'auto'} "
            f"(transport errors), refusal swap: "
            f"{settings.get('llm_refusal_swap_mode') or 'ask'}",
            f"Default hold: {int(settings.get('llm_fallback_hold_seconds') or 0)}s, "
            f"prompt timeout: "
            f"{int(settings.get('llm_fallback_prompt_timeout_seconds') or 0)}s",
        ]
        if self.thread_id and await self._fallback_thread_allowed():
            tc = self._thread_config_manager_or_none()
            config = tc.get_config(self.thread_id) if tc else None
            active = getattr(config, "active_llm_fallback", None)
            llm = getattr(config, "llm_config", None) if config else None
            overrides = []
            if getattr(llm, "fallback_switch_mode", None):
                overrides.append(f"switch mode={llm.fallback_switch_mode}")
            if getattr(llm, "refusal_swap_mode", None):
                overrides.append(f"refusal swap={llm.refusal_swap_mode}")
            if overrides:
                lines.append("Thread overrides: " + ", ".join(overrides))
            if active is None:
                lines.append("This thread: no fallback hold active.")
            else:
                if active.expires_at is None:
                    until = "PERMANENT (until /fallback revert)"
                else:
                    until = f"until {active.expires_at.isoformat()}"
                lines.append(
                    f"This thread: on {active.provider}/{active.model} "
                    f"(from {active.source_model or '?'}; "
                    f"reason: {active.reason or '?'}) {until}."
                )
        return "[Info]: Fallback status\n" + "\n".join(lines)

    async def _fallback_revert_markdown(self) -> str:
        """Clear the active thread's fallback hold (permanent or timed)."""
        missing = self._require_thread()
        if missing:
            return missing
        if not await self._fallback_thread_allowed():
            # Same non-leaking shape as the REST 404: existence is not
            # disclosed to a non-owner.
            return "[Error]: No thread matching this id."
        agent = self._agent()
        tc = self._thread_config_manager_or_none()
        if agent is None or tc is None:
            return "[Error]: Thread configuration is unavailable."
        config = tc.get_config(self.thread_id)
        active = getattr(config, "active_llm_fallback", None) if config else None
        if config is None or active is None:
            return "[Info]: This thread has no active fallback hold."
        from .agent_llm_config import clear_active_llm_fallback

        # Shared clear path: also latches the model-facing end note so the
        # next turn tells the model it is back on the primary.
        cleared = clear_active_llm_fallback(agent, self.thread_id, reason="reverted")
        if cleared is None:
            return "[Error]: Failed to clear the fallback hold."
        return (
            f"[Success]: Fallback hold cleared; this thread returns to its "
            f"configured model (was on {cleared.provider}/{cleared.model})."
        )

    # -- registered child-path handlers (Phase 3) ---------------------------
    #
    # The registry's longest-prefix match dispatches "/fallback <sub>" to
    # these directly (the parent never parses these tokens), and
    # CommandService.execute rejects agent callers pre-dispatch for the
    # agent_allowed=False registrations, so no actor re-check is needed here.

    async def _cmd_fallback_status(self, args: list[str], rest: str) -> str:
        return await self._fallback_status_markdown()

    async def _cmd_fallback_revert(self, args: list[str], rest: str) -> str:
        return await self._fallback_revert_markdown()

    async def _cmd_fallback_approvals(self, args: list[str], rest: str) -> str:
        return self._fallback_approvals_markdown()

    async def _cmd_fallback_approve(self, args: list[str], rest: str) -> str:
        return self._resolve_fallback_approval(args, approved=True)

    async def _cmd_fallback_deny(self, args: list[str], rest: str) -> str:
        return self._resolve_fallback_approval(args, approved=False)

    def _thread_config_manager_or_none(self) -> Any | None:
        agent = self._agent()
        return getattr(agent, "thread_config_manager", None) if agent else None

    def _visible_fallback_approvals(self) -> list[dict]:
        from ..tools.utils import is_admin
        from .fallback_approvals import list_pending

        if is_admin(self.user_id, agent=self._agent()):
            return list_pending()
        return list_pending(self.user_id)

    def _fallback_approvals_markdown(self) -> str:
        records = self._visible_fallback_approvals()
        if not records:
            return "[Info]: No pending fallback prompts."
        lines = [
            f"Pending fallback prompts: {len(records)}",
            "",
            "| ID | Kind | From | To | Expires | Thread |",
            "|---|---|---|---|---|---|",
        ]
        for r in records:
            lines.append(
                f"| `{r.get('record_id')}` | {r.get('kind') or 'transport'} "
                f"| {r.get('from_model') or '?'} | {r.get('to_model') or '?'} "
                f"| {r.get('expires_at') or '?'} | {r.get('thread_id') or '?'} |"
            )
        lines.append("")
        lines.append(
            "Resolve with /fallback approve <id> [minutes|permanent] [note] "
            "or /fallback deny <id> [note]. Unanswered prompts auto-swap."
        )
        return "[Info]: " + "\n".join(lines)

    def _resolve_fallback_approval(self, args: list[str], *, approved: bool) -> str:
        """Shared approve/deny path: prefix-resolve, authorize, wake the park.

        Mirrors the REST endpoint's semantics (owner-or-admin; a resolve with
        no live waiter cleans the stale record). On approve, an optional
        second token picks the hold: integer minutes or ``permanent``.
        """
        verb = "approve" if approved else "deny"
        usage = (
            f"[Error]: Usage: /fallback {verb} <prompt-id>"
            + (" [minutes|permanent] [note]" if approved else " [note]")
        )
        if not args:
            return usage
        from .fallback_approvals import (
            delete_record,
            get_fallback_approval_coordinator,
            publish_resolved_event,
        )

        prefix = args[0]
        hold_seconds: int | None = None
        hold_permanent = False
        note_args = args[1:]
        if approved and note_args:
            token = note_args[0].lower()
            if token in {"permanent", "perm", "forever"}:
                hold_permanent = True
                note_args = note_args[1:]
            else:
                try:
                    hold_seconds = max(0, int(token)) * 60
                    note_args = note_args[1:]
                except ValueError:
                    # Not a minutes count: the token is part of the free-text
                    # note, so leave note_args untouched.
                    pass
        note = " ".join(note_args).strip()
        visible = self._visible_fallback_approvals()
        matches = [
            r for r in visible if str(r.get("record_id") or "").startswith(prefix)
        ]
        if not matches:
            return f"[Error]: No pending fallback prompt matching '{prefix}'."
        if len(matches) > 1:
            ids = ", ".join(sorted(str(r.get("record_id")) for r in matches))
            return (
                f"[Error]: '{prefix}' matches multiple prompts: {ids}. "
                "Use a longer id."
            )
        record = matches[0]
        record_id = str(record.get("record_id") or "")
        woke = get_fallback_approval_coordinator().resolve(
            record_id,
            approved=approved,
            resolved_by=self.user_id,
            hold_seconds=hold_seconds,
            hold_permanent=hold_permanent,
            note=note,
        )
        if not woke:
            delete_record(record_id)
            publish_resolved_event(record, outcome="stale", resolved_by=self.user_id)
            return (
                f"[Error]: Prompt `{record_id}` is no longer pending (it timed "
                "out and auto-swapped, was resolved elsewhere, or its turn ended)."
            )
        if approved:
            hold_label = (
                " permanently"
                if hold_permanent
                else (f" for {hold_seconds // 60} minutes" if hold_seconds else "")
            )
            return (
                f"[Success]: Swapping to {record.get('to_model') or 'the fallback'}"
                f"{hold_label} ({record_id})."
            )
        return f"[Success]: Declined the model swap ({record_id})."

    async def _save_fallback_chain(self, chain: list[str], message: str) -> str:
        result = await self.api.update_settings(
            user_id=self.user_id, llm_fallback_models=",".join(chain)
        )
        if result.get("restart_required"):
            message += " (restart required to take effect)"
        return f"[Success]: {message}"

    @staticmethod
    def _fallback_chain(settings: dict) -> list[str]:
        raw = settings.get("llm_fallback_models", [])
        if isinstance(raw, str):
            raw = raw.replace("\n", ",").split(",")
        elif not isinstance(raw, (list, tuple)):
            raw = [raw]
        return LLMCommandsMixin._dedupe_models(raw)

    @staticmethod
    def _dedupe_models(values) -> list[str]:
        output: list[str] = []
        seen: set[str] = set()
        for value in values:
            model = str(value or "").strip()
            if model and model not in seen:
                output.append(model)
                seen.add(model)
        return output

    @staticmethod
    def _parse_position(value: str) -> tuple[int | None, str]:
        try:
            position = int(value)
        except ValueError:
            return None, f"Invalid position: {value}"
        if position < 1:
            return None, "Position must be 1 or greater."
        return position, ""

    @staticmethod
    def _parse_fallback_add(args: list[str]) -> tuple[str, int | None, str]:
        model = ""
        position: int | None = None
        index = 0
        while index < len(args):
            token = args[index]
            if token == "--position":
                if index + 1 >= len(args):
                    return "", None, "--position requires a value."
                position, error = LLMCommandsMixin._parse_position(args[index + 1])
                if error:
                    return "", None, error
                index += 1
            elif token.startswith("--position="):
                position, error = LLMCommandsMixin._parse_position(
                    token.split("=", 1)[1]
                )
                if error:
                    return "", None, error
            elif token.startswith("--"):
                return "", None, f"Unknown option: {token}"
            elif model:
                return "", None, "Usage: /fallback add <model-id> [--position N]"
            else:
                model = token.strip()
            index += 1
        if not model:
            return "", None, "Usage: /fallback add <model-id> [--position N]"
        return model, position, ""

    # ── Think / reasoning ─────────────────────────────────────────────────

    async def _cmd_think(self, args: list[str], rest: str) -> str | CommandOutput:
        """Show or change thinking mode, scoped like /model.

        Default scope is the active thread when there is one, else global; a
        trailing ``global`` or ``thread`` token overrides. ``/reasoning`` and
        ``/thinking`` are catalog aliases of this handler.
        """
        tokens = [a.lower() for a in args]
        scope = ""
        if tokens and tokens[-1] in ("global", "thread"):
            scope = tokens.pop()
        if len(tokens) > 1:
            return _THINK_USAGE
        value = tokens[0] if tokens else ""
        if value and value != "on" and value != "off" and value not in _THINK_LEVELS:
            return _THINK_USAGE
        if not value:
            if scope:
                return _THINK_USAGE
            return await self._think_show()

        if not scope:
            scope = "thread" if self.thread_id else "global"
        if scope == "thread":
            thread_error = self._require_thread()
            if thread_error:
                return thread_error
            return await self._think_set_thread(value)
        return await self._think_set_global(value)

    async def _think_show(self) -> str | CommandOutput:
        settings = await self.api.get_settings()
        global_thinking = bool(settings.get("llm_extended_thinking", False))
        global_effort = str(settings.get("llm_reasoning_effort") or "")

        override_thinking: bool | None = None
        override_effort: str | None = None
        thread_llm: Mapping[str, Any] = {}
        if self.thread_id:
            tc = await self.api.get_thread_config(self.thread_id)
            raw_llm = (tc or {}).get("llm_config") or {}
            if isinstance(raw_llm, Mapping):
                thread_llm = raw_llm
                if thread_llm.get("extended_thinking") is not None:
                    override_thinking = bool(thread_llm.get("extended_thinking"))
                raw_effort = thread_llm.get("reasoning_effort")
                # "" is explicit inherit at the LLMConfig choke point
                # (_resolve_thread_llm_override), not an override.
                if raw_effort is not None and str(raw_effort) != "":
                    override_effort = str(raw_effort)

        effective_thinking = (
            override_thinking if override_thinking is not None else global_thinking
        )
        effective_effort = (
            override_effort if override_effort is not None else global_effort
        )
        if str(effective_effort or "").lower() == "off":
            # Explicit effort "off" wins over extended_thinking.
            effective_thinking = False
        elif str(effective_effort or "").lower() in _THINK_LEVELS:
            # An explicit level enables thinking at every provider factory
            # (they gate on extended_thinking OR a set effort), so report it
            # as on even when the extended_thinking flag itself is False.
            effective_thinking = True

        provider, model = self._resolve_provider_model(settings, thread_llm)
        runs_at = self._clamp_for_model(provider, model, effective_effort)
        ladder = self._model_ladder(provider, model)

        state_label = "on" if effective_thinking else "off"
        effort_label = effective_effort or "default"
        if runs_at and effective_effort and runs_at != effective_effort:
            effort_label = f"{effective_effort} (runs at {runs_at})"

        rows = [
            f"Thinking: {state_label}",
            f"  Global     {'on' if global_thinking else 'off'},"
            f" effort: {global_effort or 'default'}",
        ]
        if self.thread_id:
            if override_thinking is not None or override_effort is not None:
                override_label = (
                    "-" if override_thinking is None
                    else ("on" if override_thinking else "off")
                )
                rows.append(
                    f"  Thread     {override_label},"
                    f" effort: {override_effort or 'default'} (override)"
                )
            else:
                rows.append("  Thread     none (using global)")
        rows.append(f"  Effective  {state_label}, effort: {effort_label}")
        if ladder:
            rows.append(f"  Supported  {', '.join(ladder)} ({model})")
        rows.append("Set with: /think <off|on|low|medium|high|xhigh|max> [global|thread]")
        text = "[Info]: " + "\n".join(rows)
        form = self._think_picker_form(
            provider=provider,
            model=model,
            global_current=self._think_current_value(global_thinking, global_effort),
            thread_current=self._think_current_value(
                effective_thinking, effective_effort
            ),
        )
        return CommandOutput(text, data=command_data(form=form))

    @staticmethod
    def _think_current_value(enabled: bool, effort: str | None) -> str:
        """Collapse the (enabled, effort) pair onto the /think value space."""
        level = str(effort or "").lower()
        if level == "off":
            return "off"
        if level in _THINK_LEVELS:
            return level
        return "on" if enabled else "off"

    def _think_picker_form(
        self,
        *,
        provider: str,
        model: str,
        global_current: str,
        thread_current: str,
    ) -> dict[str, Any]:
        """The thinking-level picker attached to bare ``/think``.

        One tab per writable scope ("This thread" only with an active
        thread), each submitting the scoped set command via its tab-level
        template; option meta carries the active model's clamp notes so an
        unhonored level is visible before it is chosen.
        """

        def _options(current: str) -> list[dict[str, Any]]:
            options: list[dict[str, Any]] = []
            for value in ("off", "on", *_THINK_LEVELS):
                meta = ""
                description = ""
                if value == "on":
                    description = "enable (default effort)"
                elif value == "off":
                    description = "disable thinking"
                    clamped = self._clamp_for_model(provider, model, "off")
                    if clamped and clamped != "off":
                        meta = f"cannot disable, runs at {clamped}"
                else:
                    clamped = self._clamp_for_model(provider, model, value)
                    if clamped and clamped != value:
                        meta = f"runs at {clamped}"
                options.append(
                    form_option(
                        value,
                        meta=meta,
                        description=description,
                        current=value == current,
                    )
                )
            return options

        # Distinct field keys per tab: the renderer's FormState keys its
        # cursor by field key alone, so a shared key would let the LAST
        # tab's current value park the cursor on every tab (Enter would
        # then apply the other scope's level). The per-tab submit template
        # names its own key, so cursor state stays scope-local.
        tabs: list[dict[str, Any]] = []
        if self.thread_id:
            tabs.append(
                form_tab(
                    "This thread",
                    [radio_field("thread_level", _options(thread_current))],
                    submit_command="think {thread_level} thread",
                )
            )
        tabs.append(
            form_tab(
                "Global",
                [radio_field("global_level", _options(global_current))],
                submit_command="think {global_level} global",
            )
        )
        # The form-level default mirrors the bare command's scope default:
        # thread when one is active, else global.
        default_submit = (
            "think {thread_level} thread"
            if self.thread_id
            else "think {global_level} global"
        )
        footer = (
            "←→ tab · Enter apply · Esc cancel"
            if len(tabs) > 1
            else "Enter apply · Esc cancel"
        )
        return form_payload(
            "Thinking",
            tabs,
            submit_command=default_submit,
            footer_hint=footer,
        )

    async def _think_set_global(self, value: str) -> str | CommandOutput:
        settings = await self.api.get_settings()
        provider, model = self._resolve_provider_model(settings, {})
        if value == "off":
            # Persist effort="off" so the explicit off wins over any saved
            # effort level (and over extended_thinking on other surfaces).
            await self.api.update_settings(
                user_id=self.user_id,
                llm_extended_thinking=False,
                llm_reasoning_effort="off",
            )
            _, note = self._think_clamp_note(provider, model, "off")
            return self._think_output(
                f"[Success]: Thinking disabled (global).{note}",
                enabled=False,
                effort="off",
                provider=provider,
                model=model,
            )
        if value == "on":
            effort = str(settings.get("llm_reasoning_effort") or "").lower()
            if effort == "off":
                # A persisted effort "off" wins over extended_thinking, so
                # clear it (explicit null) back to provider-default behavior.
                await self.api.update_settings(
                    user_id=self.user_id,
                    llm_extended_thinking=True,
                    llm_reasoning_effort=None,
                )
                return self._think_output(
                    "[Success]: Thinking enabled (global, effort reset to default).",
                    enabled=True,
                    effort="",
                    provider=provider,
                    model=model,
                )
            await self.api.update_settings(
                user_id=self.user_id, llm_extended_thinking=True
            )
            return self._think_output(
                "[Success]: Thinking enabled (global).",
                enabled=True,
                effort=str(settings.get("llm_reasoning_effort") or ""),
                provider=provider,
                model=model,
            )
        await self.api.update_settings(
            user_id=self.user_id,
            llm_extended_thinking=True,
            llm_reasoning_effort=value,
        )
        _, note = self._think_clamp_note(provider, model, value)
        return self._think_output(
            f"[Success]: Thinking enabled (global), effort: {value}.{note}",
            enabled=True,
            effort=value,
            provider=provider,
            model=model,
        )

    async def _think_set_thread(self, value: str) -> str | CommandOutput:
        settings = await self.api.get_settings()
        tc = await self.api.get_thread_config(self.thread_id)
        raw_llm = (tc or {}).get("llm_config") or {}
        thread_llm = raw_llm if isinstance(raw_llm, Mapping) else {}
        provider, model = self._resolve_provider_model(settings, thread_llm)

        llm_config: dict[str, Any]
        message: str
        enabled: bool
        effort: str
        if value == "off":
            # Persist effort="off" so a thread-level off wins over a global
            # effort level.
            llm_config = {"extended_thinking": False, "reasoning_effort": "off"}
            _, note = self._think_clamp_note(provider, model, "off")
            message = f"[Success]: Thinking disabled (this thread).{note}"
            enabled, effort = False, "off"
        elif value == "on":
            llm_config = {"extended_thinking": True}
            thread_effort = str(thread_llm.get("reasoning_effort") or "").lower()
            global_effort = str(settings.get("llm_reasoning_effort") or "").lower()
            # What the choke point would resolve after this write: a
            # thread-level "off" is cleared to "" (explicit inherit), so the
            # global effort applies again. A globally persisted "off" still
            # wins over a thread-level on; there is no thread-side marker
            # that can neutralize it, so guide instead of writing a no-op.
            post_thread_effort = "" if thread_effort == "off" else thread_effort
            if (post_thread_effort or global_effort) == "off":
                return (
                    "[Error]: The global reasoning effort is persisted as"
                    " 'off', which wins over a thread-level on. Use /think on"
                    " global to re-enable globally, or set an explicit level"
                    " for this thread, e.g. /think medium."
                )
            if thread_effort == "off":
                llm_config["reasoning_effort"] = ""
                message = (
                    "[Success]: Thinking enabled (this thread,"
                    " effort restored to the global setting)."
                )
                effort = global_effort
            else:
                message = "[Success]: Thinking enabled (this thread)."
                effort = post_thread_effort or global_effort
            enabled = True
        else:
            llm_config = {"extended_thinking": True, "reasoning_effort": value}
            _, note = self._think_clamp_note(provider, model, value)
            message = f"[Success]: Thinking enabled (this thread), effort: {value}.{note}"
            enabled, effort = True, value

        await self.api.update_thread_config(
            self.thread_id, user_id=self.user_id, llm_config=llm_config
        )
        return self._think_output(
            message, enabled=enabled, effort=effort, provider=provider, model=model
        )

    def _think_output(
        self,
        message: str,
        *,
        enabled: bool,
        effort: str,
        provider: str,
        model: str,
    ) -> CommandOutput:
        """Attach the ``reasoning`` state hint so clients (the CLI status
        bar's thinking label) can sync without a follow-up fetch. The hint
        carries the level the model will actually run at."""
        effective = (
            self._clamp_for_model(provider, model, effort) if effort else effort
        )
        return CommandOutput(
            message,
            data=command_data(
                state={
                    "reasoning": {
                        "enabled": enabled and str(effort).lower() != "off",
                        "effort": effective or effort or "",
                    }
                }
            ),
        )

    @staticmethod
    def _resolve_provider_model(
        settings: Mapping[str, Any] | None,
        thread_llm: Mapping[str, Any] | None,
    ) -> tuple[str, str]:
        """Resolve the active provider/model from thread overrides over globals."""
        provider = str((settings or {}).get("llm_provider") or "")
        model = str((settings or {}).get("llm_model") or "")
        if thread_llm:
            provider = str(thread_llm.get("provider") or "") or provider
            model = str(thread_llm.get("model") or "") or model
        return provider, model

    @staticmethod
    def _clamp_for_model(provider: str, model: str, effort: str) -> str:
        if not model or not effort:
            return effort
        try:
            from ..config.model_capabilities import clamp_reasoning_effort

            return clamp_reasoning_effort(provider, model, effort)
        except Exception:
            return effort

    @staticmethod
    def _model_ladder(provider: str, model: str) -> tuple | None:
        """Supported effort levels for the active model; None when unresolvable."""
        if not model:
            return None
        try:
            from ..config.model_capabilities import supported_reasoning_efforts

            return supported_reasoning_efforts(provider, model)
        except Exception:
            return None

    @staticmethod
    def _think_clamp_note(provider: str, model: str, effort: str) -> tuple[str, str]:
        """Return (effective effort, human note) for the given model.

        The note is empty when the requested level is honored as-is; otherwise
        it names the level the model actually runs at.
        """
        effective = LLMCommandsMixin._clamp_for_model(provider, model, effort)
        if not effective or effective == effort:
            return effort, ""
        if effort == "off":
            return effective, (
                f" Note: {model} cannot disable thinking; it runs at {effective}."
            )
        return effective, f" Note: {model} runs at {effective}."

    # ── Provider ──────────────────────────────────────────────────────────

    async def _cmd_provider(self, args: list[str], rest: str) -> str | CommandOutput:
        if args:
            return self._usage_error("provider")
        from ..config.llm_providers import get_llm_provider_spec

        settings = await self.api.get_settings()
        status = await self._provider_status_map()
        active_provider = self._normalize_provider(settings.get("llm_provider", ""))
        active = status.get(active_provider)
        active_spec = get_llm_provider_spec(active_provider) if active_provider else None

        tier_text = "unknown"
        if active_spec is not None:
            tier_text = f"{active_spec.tier} {_TIER_BADGES[active_spec.tier]}"
        credential_text = (
            self._status_text(active)
            if active
            else (
                "not managed by /provider"
                if active_provider and active_provider not in PROVIDER_SECRET_SETTINGS
                else "missing key"
            )
        )
        rows = [
            (
                "Active provider",
                self._provider_label(active_provider) if active_provider else "Unknown",
            ),
            ("Tier", tier_text),
            ("Model", settings.get("llm_model", "") or "Unknown"),
            ("Base URL", settings.get("llm_base_url", "") or "Provider default"),
            ("Credential", credential_text),
        ]
        if active_spec is not None and active_spec.notes_for_user:
            rows.append(("Note", active_spec.notes_for_user))
        width = max(len(label) for label, _value in rows)
        lines = ["Provider"]
        for label, value in rows:
            lines.append(f"  {label:<{width}}  {value}")
        lines.append(
            "Manage with: /provider [setup|list|set|switch|test|reasoning-passback]"
        )
        text = "[Info]: " + "\n".join(lines)
        # The picker's submit targets (switch/test) are admin-registered, so
        # mirror the dispatch gate (which blocks only on a definite False)
        # and skip the form for callers who could never submit it.
        if self.is_admin is False:
            return text
        form = self._provider_picker_form(settings, status)
        if form is None:
            return text
        return CommandOutput(text, data=command_data(form=form))

    def _provider_picker_form(
        self,
        settings: Mapping[str, Any],
        status: Mapping[str, Mapping[str, str]],
    ) -> dict[str, Any] | None:
        """The two-tab entry point attached to bare ``/provider``.

        Tab "Providers" lists every registered API-key provider spec grouped
        by tier (native, gateway, unverified; registration order within a
        tier) and submits into the chained ``/provider setup`` configure
        flow. Tab "CLIProxy" lists the subscription-OAuth catalog and
        submits the ``/provider cliproxy`` guidance command. The markdown
        fallback always rides alongside, so form-less frontends lose
        nothing; ``/provider switch``/``test`` stay as typed subcommands.
        """
        from ..cliproxy.catalog import list_cliproxy_providers
        from ..config.llm_providers import get_llm_provider_spec

        entries = self._provider_entries(settings, status)
        ordered = sorted(
            entries,
            key=lambda entry: (
                _TIER_ORDER.index(entry["tier"])
                if entry["tier"] in _TIER_ORDER
                else len(_TIER_ORDER)
            ),
        )

        def _option(entry: Mapping[str, Any]) -> dict[str, Any]:
            # Everything user-facing rides meta (the renderer shows meta OR
            # description, and meta is never empty here, so a description
            # would be dead payload); notes_for_user is folded in so the
            # unverified-tier warnings stay visible in the picker.
            meta_parts = [_TIER_BADGES.get(entry["tier"], str(entry["tier"]))]
            if entry["provider"] in PROVIDER_SECRET_SETTINGS:
                meta_parts.append(str(entry["status"]))
            if entry["notes_for_user"]:
                meta_parts.append(str(entry["notes_for_user"]))
            return form_option(
                entry["provider"],
                label=entry["label"],
                meta=" ".join(part for part in meta_parts if part),
                current=bool(entry["active"]),
            )

        provider_options: list[dict[str, Any]] = []
        for entry in ordered:
            spec = get_llm_provider_spec(str(entry["provider"]))
            if spec is None:
                # The synthetic unregistered-active entry: it cannot be
                # configured, so it stays markdown-only.
                continue
            provider_options.append(_option(entry))
        if not provider_options:
            return None

        cliproxy_options = [
            form_option(
                proxy_spec.id,
                label=proxy_spec.label,
                meta=(
                    f"routes as {proxy_spec.nymeria_provider}"
                    + (f" ({proxy_spec.api_mode})" if proxy_spec.api_mode else "")
                ),
            )
            for proxy_spec in list_cliproxy_providers()
        ]

        return form_payload(
            "Provider",
            [
                form_tab(
                    "Providers",
                    [
                        search_field("filter", placeholder="Filter providers…"),
                        radio_field("provider", provider_options),
                    ],
                    submit_command="provider setup {provider}",
                ),
                form_tab(
                    "CLIProxy",
                    [radio_field("target", cliproxy_options)],
                    submit_command="provider cliproxy {target}",
                ),
            ],
            submit_command="provider setup {provider}",
            footer_hint="←→ tab · Enter select · Esc cancel",
        )

    async def _cmd_provider_list(self, args: list[str], rest: str) -> str:
        settings = await self.api.get_settings()
        status = await self._provider_status_map()
        entries = self._provider_entries(settings, status)

        grouped: dict[str, list[dict[str, Any]]] = {tier: [] for tier in _TIER_ORDER}
        for entry in entries:
            grouped[entry["tier"]].append(entry)

        lines = ["Providers (grouped by tier)"]
        header = "  Provider                    Active  Status          Source"
        for tier in _TIER_ORDER:
            tier_rows = grouped.get(tier, [])
            if not tier_rows:
                continue
            lines.append("")
            lines.append(f"  {_TIER_BADGES[tier]}")
            lines.append(header)
            for entry in tier_rows:
                active = "yes" if entry["active"] else "no"
                lines.append(
                    f"  {entry['provider']:<26}  {active:<6} "
                    f"{entry['status']:<14} {entry['source'] or '-'}"
                )
                if entry["tier"] == "unverified" and entry["notes_for_user"]:
                    lines.append(f"    Note: {entry['notes_for_user']}")
        return "[Info]: " + "\n".join(lines)

    async def _cmd_provider_set(self, args: list[str], rest: str) -> str:
        if len(args) < 2:
            return "[Error]: Usage: /provider set <provider> <key=value> [key=value...]"
        provider = self._normalize_provider(args[0])
        if provider not in PROVIDER_SECRET_SETTINGS:
            return (
                f"[Error]: Unknown provider for /provider set: {args[0]}. "
                f"Credential fields exist for: {', '.join(PROVIDERS)}. "
                "For any other registered provider: /provider switch "
                "<provider>, then /env set llm_api_key <key> (the virtual "
                "slot routes to the active provider's declared key env var)."
            )
        values, error = self._parse_provider_values(provider, args[1:])
        if error:
            return f"[Error]: {error}"

        patch = {
            PROVIDER_SECRET_SETTINGS[provider][key]: value
            for key, value in values.items()
        }
        result = await self.api.update_settings(user_id=self.user_id, **patch)
        updated = result.get("updated", sorted(patch))
        fields = ", ".join(sorted(values))
        message = (
            f"Applied {self._provider_label(provider)} credentials ({fields}) "
            f"to backend settings: {', '.join(str(item) for item in updated)}."
        )
        if result.get("restart_required"):
            message += " Restart required."
        return f"[Success]: {message}"

    async def _cmd_provider_switch(self, args: list[str], rest: str) -> str:
        """Switch the active provider; any registered spec is accepted.

        Credential feedback is rich only for the /provider-managed trio
        (their keys live in settings fields the status map can see); other
        providers get a generic env-var hint.
        """
        if len(args) != 1:
            return "[Error]: Usage: /provider switch <provider>"
        from ..config.llm_providers import get_llm_provider_spec

        spec = get_llm_provider_spec(args[0])
        if spec is None:
            return self._unknown_provider_error(args[0])
        provider = spec.id

        settings = await self.api.get_settings()
        result = await self.api.update_settings(
            user_id=self.user_id, llm_provider=provider
        )
        if provider in PROVIDER_SECRET_SETTINGS:
            status = await self._provider_status_map()
            entry = status.get(provider) or {}
            if entry.get("status") == "authenticated":
                suffix = " A server credential is configured."
            else:
                suffix = (
                    " Warning: no server credential found for this provider;"
                    f" set one with /provider set {provider} api_key=<key>."
                )
        elif spec.requires_api_key:
            env_name = spec.api_key_env_vars[0] if spec.api_key_env_vars else ""
            hint = f" ({env_name})" if env_name else ""
            suffix = (
                " Credential status is not tracked for this provider;"
                f" make sure its API key env var{hint} is set."
            )
        else:
            suffix = ""
        # Honest scope note: switch changes ONLY llm_provider. The model and
        # base URL usually belong to the previous provider, so name them
        # instead of implying a complete switch (deliberately not rewritten
        # automatically: on gateway installs, e.g. CLIProxy, the base URL is
        # provider-independent and clearing it would break routing).
        model = str(settings.get("llm_model", "") or "").strip()
        base_url = str(settings.get("llm_base_url", "") or "").strip()
        if model or base_url:
            kept = " and ".join(
                part
                for part in (
                    f"model ({model})" if model else "",
                    f"base URL ({base_url})" if base_url else "",
                )
                if part
            )
            suffix += (
                f" The configured {kept} stays unchanged; update it if it"
                " belongs to the previous provider (/model, /env set"
                " llm_base_url)."
            )
        if result.get("restart_required"):
            suffix += " Restart required."
        return (
            f"[Success]: Switched provider to {self._provider_label(provider)}."
            f"{suffix}"
        )

    async def _cmd_provider_test(self, args: list[str], rest: str) -> str:
        if len(args) > 1:
            return "[Error]: Usage: /provider test [provider]"
        from ..config.llm_providers import get_llm_provider_spec

        settings = await self.api.get_settings()
        raw = str(args[0] if args else settings.get("llm_provider", "") or "")
        spec = get_llm_provider_spec(raw)
        if spec is None:
            return self._unknown_provider_error(raw or "<active>")

        # No api_key is sent: the backend test resolves the credential from
        # the vault, settings, and environment in that order.
        request, error = self._provider_test_request(spec, settings)
        if error:
            return f"[Error]: {error}"
        result = await self.api.test_llm_provider_config(
            request, user_id=self.user_id
        )
        label = self._provider_label(spec.id)
        if bool(result.get("ok", False)):
            return f"[Success]: {label} provider test succeeded."
        message = str(result.get("message", "") or "").strip() or "unknown error"
        return f"[Error]: {label} provider test failed: {message}"

    _RP_STATUS_TEXT = {
        "active": "active (confirmed on the last turn)",
        "wired": "wired (capable, not yet confirmed on a turn)",
        "dropped": "DROPPED (reasoning is generated but not passed back)",
        "not_applicable": "not applicable (reasoning off, or the model can't reason)",
    }

    async def _cmd_provider_reasoning_passback(
        self, args: list[str], rest: str
    ) -> str:
        """Show whether prior-turn reasoning is replayed to the active model."""
        if args:
            return "[Error]: Usage: /provider reasoning-passback"
        from ..vendor.react_agent.reasoning_passback import (
            classify_reasoning_passback,
            resolve_status_with_observation,
        )

        provider = model = api_mode = ""
        rp: dict[str, Any] | None = None

        agent = self._agent()
        resolver = (
            getattr(agent, "_get_llm_config_for_thread", None) if agent else None
        )
        effective: Any = None
        if callable(resolver):
            try:
                effective = resolver(self.thread_id or "")
            except Exception:  # noqa: BLE001
                effective = None

        if effective is not None:
            info = classify_reasoning_passback(effective)
            status, confirmed_at = resolve_status_with_observation(
                info, self.thread_id or None
            )
            provider = str(getattr(effective, "provider", "") or "")
            model = str(getattr(effective, "model", "") or "")
            api_mode = str(getattr(effective, "openai_api_mode", "") or "")
            rp = {**info.as_dict(), "status": status, "last_confirmed_at": confirmed_at}
        else:
            if self.thread_id and hasattr(self.api, "get_thread_overview"):
                overview = await self.api.get_thread_overview(
                    self.thread_id, user_id=self.user_id
                )
                llm = (overview or {}).get("llm") or {}
                rp = llm.get("reasoning_passback")
                provider = str(llm.get("provider", "") or "")
                model = str(llm.get("model", "") or "")
                api_mode = str(llm.get("api_mode", "") or "")
            if not rp:
                return (
                    "[Error]: Reasoning-passback status is unavailable here. "
                    "Send a message to activate a thread, then retry."
                )

        scope_word = "thread" if self.thread_id else "global"
        status = str(rp.get("status", ""))
        status_text = self._RP_STATUS_TEXT.get(status, status or "unknown")
        confirmed_at = rp.get("last_confirmed_at")
        if status == "active" and confirmed_at:
            from datetime import datetime

            try:
                when = datetime.fromtimestamp(float(confirmed_at)).isoformat(
                    timespec="seconds"
                )
                status_text += f" ({when})"
            except (ValueError, OSError, OverflowError):
                pass  # bad timestamp: skip the confirmed-at annotation

        turns = {
            "all_turns": "every assistant turn",
            "tool_call_turns_only": "tool-call turns only",
            "none": "not replayed",
        }.get(str(rp.get("scope", "")), str(rp.get("scope", "")))

        rows = [
            ("Provider", provider or "Unknown"),
            ("Model", model or "Unknown"),
            ("API mode", api_mode or "-"),
            ("Mechanism", str(rp.get("mechanism_label", ""))),
            ("Fidelity", str(rp.get("fidelity", ""))),
            ("Replayed on", turns),
            ("Reasoning", "on" if rp.get("reasoning_enabled") else "off"),
            ("Status", status_text),
            ("Verified", "yes (round-trip smoke-tested)" if rp.get("verified") else "no"),
        ]
        width = max(len(label) for label, _ in rows)
        lines = [f"Reasoning passback ({scope_word} scope)"]
        for label, value in rows:
            lines.append(f"  {label:<{width}}  {value}")
        caveats = rp.get("caveats") or []
        if caveats:
            lines.append("Caveats:")
            for caveat in caveats:
                lines.append(f"  - {caveat}")
        return "[Info]: " + "\n".join(lines)

    async def _provider_status_map(self) -> dict[str, dict[str, str]]:
        """Server-side credential presence per managed provider.

        Best effort: the env listing is admin-gated, so non-admin callers see
        ``unknown`` statuses instead of an error.
        """
        env_fields: dict[str, str] = {}
        env_error = ""
        try:
            data = await self.api.get_env_vars(user_id=self.user_id)
            for entry in data.get("entries", []):
                if not isinstance(entry, Mapping):
                    continue
                name = str(entry.get("name") or "")
                if name:
                    env_fields[name] = "set" if entry.get("is_set") else "missing"
        except Exception:  # noqa: BLE001 - status views degrade, never fail.
            env_error = "unavailable (admin only)"

        statuses: dict[str, dict[str, str]] = {}
        for provider in PROVIDERS:
            server_set = any(
                env_fields.get(setting) == "set"
                for setting in PROVIDER_SECRET_SETTINGS[provider].values()
            )
            if server_set:
                statuses[provider] = {"status": "authenticated", "source": "server"}
            elif env_error:
                statuses[provider] = {"status": "unknown", "source": env_error}
            else:
                statuses[provider] = {"status": "missing key", "source": ""}
        return statuses

    @staticmethod
    def _provider_entries(
        settings: Mapping[str, Any],
        status: Mapping[str, Mapping[str, str]],
    ) -> list[dict[str, Any]]:
        from ..config.llm_providers import (
            get_llm_provider_spec,
            list_llm_provider_specs,
        )

        # Canonicalize through the spec registry so an alias in llm_provider
        # marks its canonical spec active instead of appending a ghost
        # "unknown provider" entry.
        raw_active = LLMCommandsMixin._normalize_provider(
            settings.get("llm_provider", "")
        )
        active_spec = get_llm_provider_spec(raw_active)
        active_provider = active_spec.id if active_spec is not None else raw_active
        entries: list[dict[str, Any]] = []
        seen: set[str] = set()
        for spec in list_llm_provider_specs():
            provider = spec.id
            seen.add(provider)
            if provider in PROVIDER_SECRET_SETTINGS:
                status_entry = status.get(provider, {})
                status_text = status_entry.get("status", "missing key")
                source_text = status_entry.get("source", "")
            else:
                status_text = "n/a"
                source_text = ""
            entries.append(
                {
                    "provider": provider,
                    "label": spec.label,
                    "tier": spec.tier,
                    "notes_for_user": spec.notes_for_user,
                    "active": provider == active_provider,
                    "status": status_text,
                    "source": source_text,
                }
            )
        # Surface an active provider that isn't registered (custom or removed)
        # at the top of the unverified group so the status line stays legible.
        if active_provider and active_provider not in seen:
            entries.append(
                {
                    "provider": active_provider,
                    "label": active_provider,
                    "tier": "unverified",
                    "notes_for_user": "",
                    "active": True,
                    "status": "unknown provider",
                    "source": "",
                }
            )
        return entries

    @staticmethod
    def _parse_provider_values(
        provider: str,
        args: list[str],
    ) -> tuple[dict[str, str], str]:
        allowed = PROVIDER_SECRET_SETTINGS[provider]
        values: dict[str, str] = {}
        for arg in args:
            if "=" not in arg:
                # Deliberately do not echo the raw token: a mis-typed
                # invocation may paste a bare secret as the value.
                return {}, (
                    "Expected key=value credential fields "
                    "(bare value not echoed in case it is a secret). "
                    f"Allowed fields: {', '.join(sorted(allowed))}."
                )
            key, value = arg.split("=", 1)
            key = str(key or "").strip().casefold().replace("-", "_")
            value = value.strip()
            if key not in allowed:
                return {}, (
                    f"Unsupported {LLMCommandsMixin._provider_label(provider)} "
                    f"credential field: {key}. "
                    f"Allowed fields: {', '.join(sorted(allowed))}"
                )
            if not value:
                return {}, f"Credential field {key} cannot be blank."
            values[key] = value
        return values, ""

    @staticmethod
    def _provider_test_request(
        spec: "LLMProviderSpec",
        settings: Mapping[str, Any],
    ) -> tuple[dict[str, Any], str]:
        """Build the connectivity-test request for a provider spec.

        Returns ``(request, error)``; ``error`` is non-empty when no model
        can be resolved (a registered spec with no default model that is not
        the active provider).
        """
        from ..config.llm_providers import get_llm_provider_spec

        active_spec = get_llm_provider_spec(
            str(settings.get("llm_provider", "") or "")
        )
        active = active_spec is not None and active_spec.id == spec.id
        model = (
            str(settings.get("llm_model", "") or "").strip() if active else ""
        )
        if not model:
            model = str(spec.default_model or "").strip()
        if not model:
            return {}, (
                f"No model could be resolved for {spec.label} (no configured "
                "model, no spec default). Set one with /model, then retest."
            )
        request: dict[str, Any] = {"llm_provider": spec.id, "llm_model": model}
        base_url = str(settings.get("llm_base_url", "") or "").strip()
        if active and base_url:
            request["llm_base_url"] = base_url
        if spec.id in {"openai", "openrouter"}:
            mode = str(settings.get("openai_api_mode", "") or "responses").strip()
            request["openai_api_mode"] = mode if mode in OPENAI_API_MODES else "responses"
        return request, ""

    @staticmethod
    def _provider_label(provider: str) -> str:
        canonical = LLMCommandsMixin._normalize_provider(provider)
        try:
            from ..config.llm_providers import get_llm_provider_spec

            spec = get_llm_provider_spec(canonical) if canonical else None
        except Exception:
            spec = None
        if spec is not None:
            return spec.label
        return str(provider or "Unknown")

    @staticmethod
    def _normalize_provider(value: Any) -> str:
        return str(value or "").strip().casefold()

    @staticmethod
    def _status_text(entry: Mapping[str, str] | None) -> str:
        if not entry:
            return "missing key"
        source = entry.get("source", "")
        status = entry.get("status", "missing key")
        return f"{status} ({source})" if source else status

    @staticmethod
    def _unknown_provider_error(provider: str) -> str:
        return (
            f"[Error]: Unknown provider: {provider}. "
            "Run /provider list to see the registered providers."
        )
