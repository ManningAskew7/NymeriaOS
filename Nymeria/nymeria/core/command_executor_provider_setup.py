"""Provider setup command bodies (backlog #110).

House style: backend handler families live in their own domain mixin module
rather than being appended to ``command_service.py`` (see the siblings
``command_executor_llm.py`` and ``command_executor_cliproxy.py``).
``_CommandExecutor`` inherits :class:`ProviderSetupCommandsMixin`, which
owns the chained ``/provider setup`` configure flow (step-rail tabbed
forms, masked key entry, test-first atomic apply; in-flight state in
``core.provider_setup``). The ``/provider cliproxy`` subscription-OAuth
chain lives in the sibling ``command_executor_cliproxy.py``. Handler
methods are resolved by ``CommandService.execute`` via
``getattr(executor, "_cmd_<path>")``.

Runtime leaf: imports the sibling leaf ``command_executor_llm`` (shared
provider constants) and ``command_forms`` (incl. the shared step-rail
renderer), never ``command_service``, so there is no import cycle.
"""

from __future__ import annotations

import hashlib
import inspect
import logging
from collections.abc import Mapping
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from ..config.llm_providers import LLMProviderSpec

from .command_executor_llm import (
    OPENAI_API_MODES,
    _TIER_BADGES,
    custom_model_tab,
    model_pick_tab,
)
from .command_forms import (
    CommandOutput,
    chain_form_output,
    command_data,
    command_error,
    command_info,
    command_success,
    form_option,
    form_payload,
    form_tab,
    radio_field,
    rest_value,
    text_field,
)

logger = logging.getLogger(__name__)


class ProviderSetupCommandsMixin:
    """Provider-setup command bodies mixed into ``_CommandExecutor``.

    The host provides ``api`` and ``user_id``; ``_unknown_provider_error``
    comes from the sibling :class:`LLMCommandsMixin` on the same host. The
    annotations and stubs below let the static checker see them on the
    mixin in isolation.
    """

    api: Any
    user_id: str

    if TYPE_CHECKING:
        # An instance method since 2026-08-04: it consults
        # _command_offerable before naming the CLIProxy catalog.
        def _unknown_provider_error(self, provider: str) -> CommandOutput: ...

    # ── Provider setup flow (chained configure forms, backlog #110) ───────
    #
    # One registered path (``provider setup``) serves the whole chain: the
    # first argument is either a provider id (start/restart) or a reserved
    # step token dispatched by the form the previous step returned. Every
    # response re-renders the chain as ONE tabbed form: each step reached so
    # far is a tab (API key, API mode, Base URL, Model, Review) and the
    # first undecided step carries the contract's ``active`` flag, so in a
    # rich client the tab bar is a step rail and arrowing left revisits an
    # earlier decision. Form-less frontends read the same flow as guided
    # markdown. In-flight state, including the pasted key (which must NEVER
    # ride a form payload), lives server-side in ``core.provider_setup``
    # with a 10-minute TTL.

    _SETUP_STEPS = frozenset(
        {
            "key",
            "keep",
            "replace",
            "clear",
            "mode",
            "baseurl",
            "model",
            "apply",
            "notest",
            "cancel",
        }
    )
    # Returned as-is by every step whose pending record vanished; a frozen
    # CommandOutput is safe to share.
    _SETUP_GONE = command_error(
        "No provider setup is in progress (or it expired). "
        "Start one with /provider setup <provider>."
    )

    async def _cmd_provider_setup(
        self, args: list[str], rest: str
    ) -> str | CommandOutput:
        """Guided provider configuration: key -> connection -> model -> apply."""
        from ..config.llm_providers import get_llm_provider_spec
        from . import provider_setup as setup_store

        if not args:
            pending = setup_store.get_setup(self.user_id)
            if pending is None:
                return command_error(
                    "Usage: /provider setup <provider> "
                    "(see /provider list for the registered providers)."
                )
            spec = get_llm_provider_spec(pending.provider)
            if spec is None:
                setup_store.clear_setup(self.user_id)
                return self._unknown_provider_error(pending.provider)
            # Resume: re-render the chain at its first undecided step.
            settings = await self.api.get_settings()
            return await self._setup_chain(pending, spec, settings)

        token = args[0].strip().lower()
        if token in self._SETUP_STEPS:
            return await self._setup_step(token, args[1:], rest)
        spec = get_llm_provider_spec(args[0])
        if spec is None:
            return self._unknown_provider_error(args[0])
        return await self._setup_start(spec)

    async def _setup_start(self, spec: "LLMProviderSpec") -> str | CommandOutput:
        """Start (or restart) the chain: provider brief, then the step rail."""
        from . import provider_setup as setup_store

        pending = setup_store.start_setup(self.user_id, spec.id)
        existing = await self._setup_existing_key_fields(spec)
        setup_store.update_setup(
            self.user_id,
            server_key_envs=tuple(env_var for env_var, _field in existing),
            server_key_fields=self._setup_clearable_fields(existing),
        )
        settings = await self.api.get_settings()
        lines = [f"Configure {spec.label} {_TIER_BADGES.get(spec.tier, '')}".rstrip()]
        if spec.notes_for_user:
            lines.append(f"Note: {spec.notes_for_user}")
        if spec.signup_url:
            lines.append(f"Get a key: {spec.signup_url}")
        if spec.signup_guidance:
            lines.append(spec.signup_guidance)
        if not spec.requires_api_key:
            setup_store.update_setup(self.user_id, key_choice="none")
            lines.append("No API key is required for this provider.")
        return await self._setup_chain(pending, spec, settings, note_lines=lines)

    def _setup_update(self, **fields: Any) -> bool:
        """Apply pending-setup fields; False when the record is gone.

        The store's TTL is sliding, so a mid-step expiry is rare, but a
        silent drop would render a chain form that contradicts the (empty)
        server state; callers return ``_SETUP_GONE`` on False instead.
        """
        from . import provider_setup as setup_store

        return setup_store.update_setup(self.user_id, **fields) is not None

    async def _setup_step(
        self, token: str, args: list[str], rest: str
    ) -> str | CommandOutput:
        """Dispatch one reserved step token against the pending setup."""
        from ..config.llm_providers import get_llm_provider_spec
        from . import provider_setup as setup_store

        if token == "cancel":
            cleared = setup_store.clear_setup(self.user_id)
            return (
                "Provider setup cancelled. Nothing was saved."
                if cleared
                else "No provider setup was in progress."
            )
        pending = setup_store.get_setup(self.user_id)
        if pending is None:
            return self._SETUP_GONE
        spec = get_llm_provider_spec(pending.provider)
        if spec is None:
            setup_store.clear_setup(self.user_id)
            return self._unknown_provider_error(pending.provider)
        settings = await self.api.get_settings()

        if token == "keep":
            if pending.key_choice == "paste" and pending.api_key:
                # Keeping the already-pasted key: only leave entry mode.
                updated = self._setup_update(key_entry=False)
            else:
                updated = self._setup_update(
                    key_choice="keep", api_key="", key_entry=False
                )
            if not updated:
                return self._SETUP_GONE
            return await self._setup_chain(pending, spec, settings)
        if token == "replace":
            if not self._setup_update(key_entry=True):
                return self._SETUP_GONE
            return await self._setup_chain(pending, spec, settings)
        if token == "clear":
            if not self._setup_update(
                key_choice="clear", api_key="", key_entry=False
            ):
                return self._SETUP_GONE
            lines = ["The stored key will be cleared when you apply."]
            if spec.requires_api_key:
                lines.append(
                    "Warning: this provider requires an API key; it will stop"
                    " working after apply until a new one is set."
                )
            return await self._setup_chain(pending, spec, settings, note_lines=lines)
        if token == "key":
            return await self._setup_take_key(pending, spec, settings, rest)
        if token == "mode":
            value = (args[0] if args else "").strip().lower()
            if value not in OPENAI_API_MODES:
                return command_error(
                    "Usage: /provider setup mode"
                    " <responses|chat_completions>"
                )
            if not self._setup_update(api_mode=value):
                return self._SETUP_GONE
            return await self._setup_chain(pending, spec, settings)
        if token == "baseurl":
            return await self._setup_take_base_url(pending, spec, settings, rest)
        if token == "model":
            value = rest_value(rest)
            if not value:
                return command_error(
                    "Usage: /provider setup model <model-id|custom>"
                )
            if value.lower() == "custom":
                updated = self._setup_update(model_custom=True)
            else:
                updated = self._setup_update(model=value, model_custom=False)
            if not updated:
                return self._SETUP_GONE
            return await self._setup_chain(pending, spec, settings)
        if token == "apply":
            # Typed path: "/provider setup apply notest" also skips the test.
            notest = bool(args) and args[0].strip().lower() == "notest"
            return await self._setup_apply(pending, spec, settings, notest=notest)
        if token == "notest":
            # Form path: option ids must be single tokens (the client
            # shell-quotes multi-word values into one backend token).
            return await self._setup_apply(pending, spec, settings, notest=True)
        return self._SETUP_GONE

    async def _setup_take_key(
        self,
        pending: Any,
        spec: "LLMProviderSpec",
        settings: Mapping[str, Any],
        rest: str,
    ) -> str | CommandOutput:
        """Store a pasted key after hygiene, then re-render the chain."""
        from ..setup.providers import valid_key_format_for_spec
        from . import provider_setup as setup_store

        value, hygiene = setup_store.clean_pasted_secret(rest_value(rest))
        if not value:
            if not self._setup_update(key_entry=True):
                return self._SETUP_GONE
            return await self._setup_chain(
                pending,
                spec,
                settings,
                note_lines=[
                    "The pasted key was empty after cleanup. Paste it again."
                ],
            )
        lines: list[str] = []
        ok, prefix = valid_key_format_for_spec(spec, value)
        if not ok and prefix:
            lines.append(
                f"Warning: {spec.label} keys usually start with `{prefix}`;"
                " continuing anyway (the model list and the apply test"
                " validate it for real)."
            )
        lines.extend(f"Warning: {warning}" for warning in hygiene)
        if not self._setup_update(
            api_key=value, key_choice="paste", key_entry=False
        ):
            return self._SETUP_GONE
        return await self._setup_chain(pending, spec, settings, note_lines=lines)

    async def _setup_take_base_url(
        self,
        pending: Any,
        spec: "LLMProviderSpec",
        settings: Mapping[str, Any],
        rest: str,
    ) -> str | CommandOutput:
        value = rest_value(rest)
        lowered = value.lower()
        if not value:
            return command_error(
                "Usage: /provider setup baseurl <url|default|custom>"
            )
        if lowered == "custom":
            if not self._setup_update(base_url_custom=True):
                return self._SETUP_GONE
            return await self._setup_chain(pending, spec, settings)
        if lowered == "default":
            if not self._setup_update(base_url="", base_url_custom=False):
                return self._SETUP_GONE
            return await self._setup_chain(pending, spec, settings)
        if not lowered.startswith(("http://", "https://")):
            if not self._setup_update(base_url_custom=True):
                return self._SETUP_GONE
            return await self._setup_chain(
                pending,
                spec,
                settings,
                note_lines=[
                    "That does not look like a URL (expected http:// or"
                    " https://). Try again."
                ],
            )
        if not self._setup_update(
            base_url=value.rstrip("/"), base_url_custom=False
        ):
            return self._SETUP_GONE
        return await self._setup_chain(pending, spec, settings)

    # -- the chain form (step rail) ----------------------------------------

    async def _setup_chain(
        self,
        pending: Any,
        spec: "LLMProviderSpec",
        settings: Mapping[str, Any],
        *,
        note_lines: list[str] | None = None,
    ) -> CommandOutput:
        """Render the chain as one tabbed form.

        Steps are appended in order until the first undecided one (tabs grow
        as the user advances); that step carries the contract's ``active``
        flag and contributes the guidance markdown. When every step is
        decided the Review tab (always "undecided") lands last and active,
        with the earlier tabs still present for revision.
        """
        steps: list[tuple[bool, Any]] = [
            (spec.requires_api_key, lambda: self._setup_key_tab(pending, spec)),
            (
                spec.supports_responses,
                lambda: self._setup_mode_tab(pending, spec, settings),
            ),
            (
                self._setup_needs_base_url(spec, settings),
                lambda: self._setup_base_url_tab(pending, spec, settings),
            ),
            (True, lambda: self._setup_model_tab(pending, spec, settings)),
            (True, lambda: self._setup_review_tab(pending, spec)),
        ]
        chain: list[tuple[dict[str, Any], bool, list[str]]] = []
        for applies, build in steps:
            if not applies:
                continue
            entry = build()
            if inspect.isawaitable(entry):
                entry = await entry
            chain.append(entry)
            if not entry[1]:
                # First undecided step: the rail stops growing here (Review
                # is never "decided", so it terminates a fully decided
                # chain as the last, active tab).
                break

        active_tab, _decided, guidance = chain[-1]
        tabs = [tab for tab, _d, _l in chain]
        # No notes here, deliberately: this rail's step guidance is
        # load-bearing beyond the panel (key-hygiene warnings, the Review
        # tab's confirmation table), so every response prints in full.
        return chain_form_output(
            f"Setup: {spec.label}",
            tabs,
            active_tab,
            list(note_lines or []) + guidance,
            fallback_text=f"Configuring {spec.label}.",
        )

    def _setup_key_tab(
        self, pending: Any, spec: "LLMProviderSpec"
    ) -> tuple[dict[str, Any], bool, list[str]]:
        """The API key step: masked entry, or Keep/Replace/Clear when there
        is something to keep (the hermes reconfigure idiom)."""
        server_envs = tuple(pending.server_key_envs or ())
        has_pasted = pending.key_choice == "paste" and bool(pending.api_key)
        entry_mode = pending.key_entry or (
            not has_pasted
            and not server_envs
            and pending.key_choice not in ("keep", "clear")
        )
        if entry_mode:
            from ..setup.providers import key_prefix_for_spec

            prefix = key_prefix_for_spec(spec)
            placeholder = (
                f"{prefix}..." if prefix else f"paste your {spec.label} API key"
            )
            tab = form_tab(
                "API key",
                [
                    text_field(
                        "api_key",
                        label="API key",
                        placeholder=placeholder,
                        secret=True,
                    )
                ],
                submit_command="provider setup key {api_key}",
            )
            return tab, False, [
                "Paste the API key (input is masked).",
                "Type: /provider setup key <api-key>",
            ]

        options: list[dict[str, Any]] = []
        if has_pasted:
            options.append(
                form_option("keep", label="Keep the pasted key", current=True)
            )
        elif server_envs:
            options.append(
                form_option(
                    "keep",
                    label="Keep the existing key",
                    meta=", ".join(server_envs),
                    current=pending.key_choice in ("", "keep"),
                )
            )
        options.append(form_option("replace", label="Paste a new key"))
        if tuple(pending.server_key_fields or ()):
            # Clearing writes ""-patches to the provider's key settings
            # fields; only offered when at least one such field is
            # patchable (see _setup_clearable_fields).
            options.append(
                form_option(
                    "clear",
                    label="Clear the stored key",
                    meta=(
                        "provider requires a key"
                        if spec.requires_api_key
                        else ""
                    ),
                    current=pending.key_choice == "clear",
                )
            )
        tab = form_tab(
            "API key",
            [radio_field("key_choice", options)],
            submit_command="provider setup {key_choice}",
        )
        decided = pending.key_choice in ("paste", "keep", "clear", "none")
        lines = (
            [f"A server API key is already configured ({', '.join(server_envs)})."]
            if server_envs
            else []
        )
        # Choose line derived centrally from the submit template (#158);
        # the option rows carry the ids for formless callers.
        return tab, decided, lines

    def _setup_mode_tab(
        self,
        pending: Any,
        spec: "LLMProviderSpec",
        settings: Mapping[str, Any],
    ) -> tuple[dict[str, Any], bool, list[str]]:
        current = (
            pending.api_mode
            or str(settings.get("openai_api_mode", "") or "").strip().lower()
        )
        if current not in OPENAI_API_MODES:
            current = str(spec.default_api_mode or "chat_completions")
        options = [
            form_option(
                "responses",
                label="Responses API",
                meta="stateful; richer reasoning passback",
                current=current == "responses",
            ),
            form_option(
                "chat_completions",
                label="Chat Completions",
                meta="widest gateway compatibility",
                current=current == "chat_completions",
            ),
        ]
        tab = form_tab(
            "API mode",
            [radio_field("api_mode", options)],
            submit_command="provider setup mode {api_mode}",
        )
        return (
            tab,
            pending.api_mode is not None,
            # No id-enumerating dispatch line: the central renderer derives
            # "Choose: /provider setup mode <api_mode>" beside the option
            # rows for formless callers (#158).
            ["Choose the OpenAI-compatible API mode."],
        )

    def _setup_base_url_tab(
        self,
        pending: Any,
        spec: "LLMProviderSpec",
        settings: Mapping[str, Any],
    ) -> tuple[dict[str, Any], bool, list[str]]:
        if pending.base_url_custom:
            tab = form_tab(
                "Base URL",
                [
                    text_field(
                        "base_url",
                        label="Base URL",
                        placeholder=spec.default_base_url
                        or "https://host[:port]/v1",
                    )
                ],
                submit_command="provider setup baseurl {base_url}",
            )
            return tab, False, [
                "Type the base URL to use.",
                "Type: /provider setup baseurl <url>",
            ]

        current = str(settings.get("llm_base_url", "") or "").strip()
        decided = pending.base_url is not None
        options: list[dict[str, Any]] = []
        seen: set[str] = set()
        if current:
            options.append(
                form_option(
                    current,
                    label=f"Keep current ({current})",
                    current=pending.base_url == current or not decided,
                )
            )
            seen.add(current)
        if pending.base_url and pending.base_url not in seen:
            options.append(
                form_option(
                    pending.base_url,
                    label=f"Custom ({pending.base_url})",
                    current=True,
                )
            )
            seen.add(pending.base_url)
        options.append(
            form_option(
                "default",
                label="Provider default",
                meta=spec.default_base_url or "SDK default",
                current=pending.base_url == "" or (not decided and not current),
            )
        )
        options.append(form_option("custom", label="Custom URL…"))
        tab = form_tab(
            "Base URL",
            [radio_field("base_url_choice", options)],
            submit_command="provider setup baseurl {base_url_choice}",
        )
        return tab, decided, [
            "Choose the API base URL.",
            "Type: /provider setup baseurl <url>|default|custom",
        ]

    async def _setup_model_tab(
        self,
        pending: Any,
        spec: "LLMProviderSpec",
        settings: Mapping[str, Any],
    ) -> tuple[dict[str, Any], bool, list[str]]:
        """The model step: live list using the PENDING credentials (the
        hermes trick: a successful list doubles as the credential probe),
        cached on the record so revisiting the tab never refetches with
        unchanged credentials; degrades to known defaults with an honest
        source line, never blocking the chain."""
        from ..config.llm_providers import get_llm_provider_spec
        from . import provider_setup as setup_store

        if pending.model_custom:
            tab = custom_model_tab(
                spec.default_model or "model-id",
                "provider setup model {model}",
            )
            return tab, False, [
                "Type the model id to use.",
                "Type: /provider setup model <model-id>",
            ]

        # The fingerprint keys the cached list to the credentials that
        # produced it; the key rides as a digest so the raw secret never
        # sits in a non-redacted store field.
        key_digest = (
            hashlib.sha256(pending.api_key.encode("utf-8")).hexdigest()[:16]
            if pending.api_key
            else ""
        )
        fingerprint = (
            f"{pending.key_choice}|{key_digest}|{pending.base_url!r}"
        )
        if (
            pending.model_options is None
            or pending.models_fingerprint != fingerprint
        ):
            api_key = pending.api_key or None
            base_url = pending.base_url or None
            if pending.base_url == "" and spec.default_base_url:
                # Cleared-to-default: list against the provider default
                # instead of letting the facade adopt the current (cleared)
                # custom URL.
                base_url = spec.default_base_url
            try:
                models = await self.api.list_available_models(
                    spec.id, self.user_id, api_key=api_key, base_url=base_url
                )
            except Exception:  # noqa: BLE001 - the list degrades, never blocks.
                logger.debug("provider setup: model list failed", exc_info=True)
                models = []
            raw: list[dict[str, Any]] = []
            for entry in models or []:
                model_id = str(entry.get("id") or entry.get("name") or "")
                if not model_id:
                    continue
                ctx_len = entry.get("context_length") or entry.get(
                    "context_window"
                )
                raw.append(
                    {
                        "id": model_id,
                        "meta": f"{self._fmt_ctx(ctx_len)} ctx" if ctx_len else "",
                    }
                )
            note = (
                f"{len(raw)} models listed from {spec.label}."
                if raw
                else (
                    "Model list unavailable (credentials not accepted yet, or"
                    " the provider is unreachable); showing known defaults."
                )
            )
            # An empty list is never cached (model_options=None keeps the
            # next render retrying): the failure may be transient, and the
            # user may fix the credentials on the key tab in between.
            setup_store.update_setup(
                self.user_id,
                model_options=raw or None,
                models_fingerprint=fingerprint,
                models_note=note,
            )

        active_spec = get_llm_provider_spec(
            str(settings.get("llm_provider", "") or "")
        )
        active = active_spec is not None and active_spec.id == spec.id
        current_model = (
            str(settings.get("llm_model", "") or "").strip() if active else ""
        )
        preselect = (
            str(pending.model or "")
            or current_model
            or str(spec.default_model or "")
        )
        options: list[dict[str, Any]] = []
        for entry in pending.model_options or []:
            model_id = str(entry.get("id") or "")
            options.append(
                form_option(
                    model_id,
                    meta=str(entry.get("meta") or ""),
                    current=model_id == preselect,
                )
            )
        if not options:
            for fallback_id in dict.fromkeys(
                value
                for value in (current_model, str(spec.default_model or ""))
                if value
            ):
                options.append(
                    form_option(
                        fallback_id,
                        meta=(
                            "current model"
                            if fallback_id == current_model
                            else "spec default"
                        ),
                        current=fallback_id == preselect,
                    )
                )
        insert_meta = (
            "current model"
            if preselect == current_model
            else "spec default"
            if preselect == str(spec.default_model or "")
            else "custom"
        )
        tab = model_pick_tab(
            options, preselect, insert_meta, "provider setup model {model}"
        )
        return tab, pending.model is not None, [
            str(pending.models_note or ""),
            "Choose: /provider setup model <model-id>|custom",
        ]

    def _setup_review_tab(
        self, pending: Any, spec: "LLMProviderSpec"
    ) -> tuple[dict[str, Any], bool, list[str]]:
        key_label = {
            "paste": "new key (pasted)",
            "keep": "keep the existing server key",
            "clear": "CLEAR the stored key",
            "none": "not required",
        }.get(pending.key_choice, "unchanged")
        if pending.base_url is None:
            base_label = "unchanged"
        elif pending.base_url == "":
            base_label = (
                f"provider default ({spec.default_base_url or 'SDK default'})"
            )
        else:
            base_label = pending.base_url
        rows = [
            ("Provider", spec.label),
            ("API key", key_label),
            ("Model", pending.model or "?"),
            ("Base URL", base_label),
        ]
        if spec.supports_responses and pending.api_mode:
            rows.append(("API mode", pending.api_mode))
        width = max(len(label) for label, _value in rows)
        lines = ["Review the pending provider change"]
        for label, value in rows:
            lines.append(f"  {label:<{width}}  {value}")
        if pending.key_choice == "clear" and spec.requires_api_key:
            lines.append(
                "Warning: the connectivity test still uses the OLD stored key"
                " (the clear happens on apply), so a passing test does not"
                " mean the provider will work afterwards."
            )
        lines.append("Nothing is saved until you apply.")
        # Choose line derived centrally from the submit template (#158).
        options = [
            form_option("apply", label="Test and apply", current=True),
            form_option("notest", label="Apply without testing"),
            form_option("cancel", label="Cancel"),
        ]
        tab = form_tab(
            "Review",
            [radio_field("action", options)],
            submit_command="provider setup {action}",
        )
        # Review is never "decided": it is always the chain's pending
        # decision, so it lands last and active once everything else is set.
        return tab, False, lines

    @staticmethod
    def _setup_needs_base_url(
        spec: "LLMProviderSpec", settings: Mapping[str, Any]
    ) -> bool:
        """Mirror the wizard ConnectionStep conditions, plus "a base URL is
        currently configured": switching providers must force an explicit
        keep/clear decision so a stale gateway URL cannot silently leak into
        the new provider's config."""
        current = str(settings.get("llm_base_url", "") or "").strip()
        return bool(
            spec.requires_base_url
            or spec.default_base_url is None
            or not spec.requires_api_key
            or current
        )

    @staticmethod
    def _setup_clearable_fields(
        existing: list[tuple[str, str]]
    ) -> tuple[str, ...]:
        """Settings fields a ""-patch can actually clear.

        A clear is only offered for key fields the update model declares:
        anything else now 400s at the applier's unknown-key gate (it was a
        silent no-op before 2026-08-10), so offering it would trade a silent
        lie for a confusing hard error.
        """
        from ..api.schemas.settings import ServerSettingsUpdate

        return tuple(
            field_name
            for _env_var, field_name in existing
            if field_name and field_name in ServerSettingsUpdate.model_fields
        )

    async def _setup_existing_key_fields(
        self, spec: "LLMProviderSpec"
    ) -> list[tuple[str, str]]:
        """(env_var, settings_field) pairs for this provider's SET key vars.

        Resolved via the admin env listing (masked values only; presence is
        all this flow needs). Best effort: empty when nothing is set or the
        listing is unavailable.
        """
        wanted = set(spec.api_key_env_vars or ())
        if not wanted:
            return []
        try:
            data = await self.api.get_env_vars(user_id=self.user_id)
        except Exception:  # noqa: BLE001 - presence check degrades, never fails.
            return []
        matches: list[tuple[str, str]] = []
        for entry in data.get("entries", []):
            if not isinstance(entry, Mapping):
                continue
            env_var = str(entry.get("env_var") or "")
            if env_var in wanted and entry.get("is_set"):
                matches.append((env_var, str(entry.get("name") or "")))
        return matches

    async def _setup_apply(
        self,
        pending: Any,
        spec: "LLMProviderSpec",
        settings: Mapping[str, Any],
        *,
        notest: bool,
    ) -> str | CommandOutput:
        """Test-first atomic apply: nothing is written on a failed test, and
        the write is ONE ``update_settings`` patch (the applier routes the
        virtual ``llm_api_key`` to the patch's provider in the same pass)."""
        from ..config.llm_providers import get_llm_provider_spec
        from . import provider_setup as setup_store

        model = str(pending.model or "").strip()
        if not model:
            # Reached apply without a decided model (stale form): re-chain.
            return await self._setup_chain(pending, spec, settings)

        if not notest:
            request: dict[str, Any] = {"llm_provider": spec.id, "llm_model": model}
            resolved_base = pending.base_url
            if resolved_base is None:
                active_spec = get_llm_provider_spec(
                    str(settings.get("llm_provider", "") or "")
                )
                if active_spec is not None and active_spec.id == spec.id:
                    resolved_base = str(
                        settings.get("llm_base_url", "") or ""
                    ).strip()
            if resolved_base:
                request["llm_base_url"] = resolved_base
            if spec.supports_responses and pending.api_mode:
                request["openai_api_mode"] = pending.api_mode
            if pending.key_choice == "paste" and pending.api_key:
                request["api_key"] = pending.api_key
            result = await self.api.test_llm_provider_config(
                request, user_id=self.user_id
            )
            if not bool(result.get("ok", False)):
                message = (
                    str(result.get("message", "") or "").strip()
                    or "unknown error"
                )
                options = [
                    form_option("apply", label="Retry the test", current=True),
                    form_option("notest", label="Apply anyway"),
                    form_option("replace", label="Re-enter the API key"),
                    form_option("cancel", label="Cancel"),
                ]
                form = form_payload(
                    "Provider test failed",
                    [
                        form_tab(
                            "Next",
                            [radio_field("action", options)],
                            submit_command="provider setup {action}",
                        )
                    ],
                    submit_command="provider setup {action}",
                    footer_hint="Enter select · Esc cancel",
                )
                return command_info(
                    f"{spec.label} provider test FAILED: {message}\n"
                    "Nothing was saved.",
                    data=command_data(form=form),
                )

        patch: dict[str, Any] = {"llm_provider": spec.id, "llm_model": model}
        if pending.base_url is not None:
            patch["llm_base_url"] = pending.base_url
        if spec.supports_responses and pending.api_mode:
            patch["openai_api_mode"] = pending.api_mode
        if pending.key_choice == "paste" and pending.api_key:
            patch["llm_api_key"] = pending.api_key
        elif pending.key_choice == "clear":
            # Re-derive the clearable fields at apply time (the world may
            # have changed since start); an empty result must be an honest
            # refusal, because a ""-patch for an undeclared field now 400s
            # at the applier's unknown-key gate and the clear would fail
            # noisily instead of completing.
            clearable = self._setup_clearable_fields(
                await self._setup_existing_key_fields(spec)
            )
            if not clearable:
                return command_error(
                    "No clearable stored key was found for"
                    f" {spec.label}: its key field cannot be patched through"
                    " settings, so nothing was written. Unset the environment"
                    " variable directly (see /env) and rerun /provider setup."
                )
            for field_name in clearable:
                patch[field_name] = ""
        result = await self.api.update_settings(user_id=self.user_id, **patch)
        setup_store.clear_setup(self.user_id)
        updated = result.get("updated") or sorted(patch)
        message = (
            f"Switched to {spec.label} with model {model}."
            f" Updated: {', '.join(str(item) for item in updated)}."
        )
        if not notest:
            message = f"Provider test passed. {message}"
        if result.get("restart_required"):
            message += " Restart required for some changes."
        return command_success(message)

    @staticmethod
    def _fmt_ctx(value: Any) -> str:
        try:
            number = int(value)
        except (TypeError, ValueError):
            return ""
        return f"{number // 1000}k" if number >= 1000 else str(number)
