"""Per-thread LLM configuration resolution.

Extracted from ``NymeriaAgent``. Builds an :class:`LLMConfig` by layering
per-thread overrides on top of global settings, resolving provider
credentials through the credential vault, deriving CLIProxy-compatible
base URLs for cross-provider overrides, and assembling the fallback
provider chain.

The host-dependent functions take a narrow ``LLMConfigHost`` capability
bundle (a ``Protocol``) instead of the whole ``NymeriaAgent`` god-object: it
enumerates exactly the settings, thread-config manager, accounts repo,
credential vault, thread-lock manager, and cache-invalidation method they
touch. ``NymeriaAgent`` satisfies the Protocol structurally, so its thin
facade methods pass ``self`` unchanged and external callers (agent_graph,
agent_compaction, thread_overview, the chat/settings/threads routers, and the
platform bot routers) keep their existing call shape. A hand-built stub
satisfies the same Protocol, so resolution is unit-testable without a
``NymeriaAgent``. The seam mirrors ``core/turn_executor.py``.
"""

from __future__ import annotations

import logging
from datetime import timedelta
from typing import Any, Protocol, runtime_checkable


from ..config.llm_providers import (
    cliproxy_base_url_for_provider,
    normalize_llm_provider,
    resolve_provider_route,
    resolve_provider_api_key,
    resolve_provider_base_url,
)
from ..config.model_tiers import split_provider_model
from ..config.local_llm import (
    detect_local_server_type,
    is_local_llm_base_url,
    query_local_context_length,
    query_ollama_num_ctx,
)
from ..config.model_capabilities import (
    clamp_reasoning_effort,
    register_model_metadata,
    supported_reasoning_efforts,
)
from ..vendor.react_agent import LLMConfig, LLMFallbackConfig
from .llm_credentials import (
    contains_credential_reference,
    get_llm_provider_credential,
    resolve_credential_references,
    secret_is_caller_owned,
)
from .llm_provider_utils import (
    base_url_destination_key,
    configured_llm_destinations,
    destination_redirects_away_from_config,
)
from .thread_config import ActiveLLMFallback, ThreadConfig
from .time_utils import ensure_aware_utc, utc_now


logger = logging.getLogger(__name__)

_LOCAL_PROVIDER_IDS = {"ollama", "lmstudio", "llamacpp", "vllm", "localai", "litellm", "tgi"}


@runtime_checkable
class LLMConfigHost(Protocol):
    """Narrow capability surface LLM-config resolution needs from a host.

    ``NymeriaAgent`` satisfies this structurally, so the facade methods pass
    ``self`` unchanged; a hand-built stub satisfies it for isolated unit
    tests. Data collaborators are typed ``Any`` (``settings`` alone carries
    two dozen fields the resolver reads, and ``accounts_repo`` /
    ``credential_vault`` / ``_thread_locks`` are read defensively via
    ``getattr`` because bare agents may lack them); ``invalidate_thread_config_cache``
    carries its real signature.
    """

    settings: Any
    thread_config_manager: Any
    accounts_repo: Any
    credential_vault: Any
    _thread_locks: Any

    def invalidate_thread_config_cache(self, thread_id: str) -> None: ...


def _resolve_thread_llm_override(thread_value: Any, global_value: Any) -> Any:
    """Resolve a thread LLM override against its global fallback."""
    if thread_value is None:
        return global_value
    if isinstance(thread_value, str) and thread_value == "":
        return global_value
    return thread_value


def _parse_llm_fallback_models(value: Any) -> list[str]:
    """Parse comma/newline-separated fallback model settings."""
    if value is None:
        return []
    if isinstance(value, str):
        raw_items = value.replace("\n", ",").split(",")
    elif isinstance(value, (list, tuple)):
        raw_items = value
    else:
        raw_items = [value]

    models: list[str] = []
    seen: set[str] = set()
    for item in raw_items:
        model = str(item or "").strip()
        if not model or model in seen:
            continue
        seen.add(model)
        models.append(model)
    return models


def _positive_int(value: Any) -> int | None:
    if value is None or value == "":
        return None
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        return None
    return parsed if parsed > 0 else None


def _split_llm_fallback_ref(
    value: str,
    default_provider: str,
) -> tuple[str, str]:
    """Split provider:model fallback refs while preserving model IDs with colons.

    Delegates to the shared splitter so fallback entries and the fast/smart
    tier aliases parse the ``provider:model`` convention identically.
    """
    return split_provider_model(value, default_provider)


def _active_fallback_is_expired(active: ActiveLLMFallback) -> bool:
    # expires_at is None for a permanent hold, which never auto-expires.
    if active.expires_at is None:
        return False
    return ensure_aware_utc(active.expires_at) <= utc_now()


# Dedup set for the one-shot clamp warning. Keyed on (provider, model,
# requested effort) so a saved over-ask logs once, not once per chat turn.
# Mirrors the _DOWNGRADED_ROUTE_WARNED pattern in config/llm_providers.py.
_CLAMPED_EFFORT_WARNED: set[tuple[str, str, str]] = set()


def _clamp_reasoning_effort_for_model(
    provider: str,
    model: Any,
    effort: Any,
    provider_route: Any = None,
) -> Any:
    """Clamp the resolved reasoning effort onto the model's supported ladder.

    None/empty stays "unset/inherit" and is passed through untouched. When the
    requested level is unsupported, the clamped value is returned and a
    one-shot warning is logged per (provider, model, requested effort).
    """
    effort_text = str(effort or "").strip().lower()
    if not effort_text:
        return effort

    model_text = str(model or "")
    clamped = clamp_reasoning_effort(provider, model_text, effort_text, provider_route)
    if clamped != effort_text:
        warn_key = (provider, model_text, effort_text)
        if warn_key not in _CLAMPED_EFFORT_WARNED:
            _CLAMPED_EFFORT_WARNED.add(warn_key)
            supported = supported_reasoning_efforts(provider, model_text, provider_route)
            logger.warning(
                "[LLM] Model %s (%s) does not support reasoning effort %r "
                "(supported: %s). Clamping to %r.",
                model_text or "unknown",
                provider,
                effort_text,
                ", ".join(supported) or "none",
                clamped,
            )
        return clamped
    return effort_text


def _thread_is_busy(host: LLMConfigHost, thread_id: str) -> bool:
    locks = getattr(host, "_thread_locks", None)
    if locks is None or not hasattr(locks, "get_lock_info"):
        return False
    try:
        return locks.get_lock_info(thread_id) is not None
    except Exception:
        return False


def fallback_end_note_stamp(active: ActiveLLMFallback, *, reason: str) -> dict[str, Any]:
    """The end-note latch stamp for a clearing hold (shared by every clear
    surface, incl. the thread-config PATCH which mutates its own config
    object instead of calling :func:`clear_active_llm_fallback`).

    from = the fallback that was holding, to = the primary coming back.
    """
    from ..vendor.react_agent.nodes import fallback_note_stamp

    return fallback_note_stamp(
        {
            "from_provider": active.provider,
            "from_model": active.model,
            "to_provider": active.source_provider,
            "to_model": active.source_model,
            "reason": reason,
        },
        kind="refusal" if active.reason == "refusal" else "transport",
        phase="end",
    )


def _reportable_destination(raw: str | None, resolved: str | None) -> str:
    """What may be named in a log, a persisted field and the model's context.

    Everything downstream of a refusal is a disclosure channel: a WARNING log,
    a field persisted to disk, a ``GET /threads/{id}/config`` response, and
    note text folded into the next turn's message. Two reductions get it there
    safely.

    The KEY, never the URL. Scheme, host and port carry no userinfo, path or
    query, and the key is also the exact granularity the decision was made at.

    And nothing at all when the field INTERPOLATED, because ``base_url``
    accepts ``${credential:...}`` like every other explicit LLM field, so the
    resolved value can BE a decrypted secret. Keying is not enough on its own:
    a hostname needs no dots, so a bare secret keys to ``http://<the secret>``.
    The cost is that two different references read as the same destination, so
    swapping one for another does not re-note; a credential reference in a
    base URL is pathological enough that losing that precision is fine.
    """
    if contains_credential_reference(raw):
        return "a credential reference"
    return base_url_destination_key(resolved) or "an unparseable address"


def _latch_rejected_destination_note(
    host: LLMConfigHost,
    thread_id: str,
    thread_config: Any,
    rejected: str,
) -> None:
    """Tell the user once that their base_url was ignored, not once per turn.

    ``rejected`` is a destination KEY (scheme, host and port), never a raw
    base URL: see the assignment site for why that distinction is load-bearing.

    Guarded on the REFUSED DESTINATION, not on the note itself: the latch is
    consumed at the start of the next turn, so without this a persistently bad
    config would re-latch on every turn, which is exactly the per-turn
    injection the fallback-note precedent forbids. A changed destination is a
    new fact, so it re-notes. That same guard is what bounds the write: config
    resolution runs on read paths too (thread overview, ``file_read``), and
    without it every one of them would persist a file.

    Best-effort by design. This runs inside per-turn config resolution, so a
    failure to persist the notice must not fail the turn: the security decision
    has already been made and logged at WARNING above, and the note is how the
    user is told, not how the credential is protected.
    """
    # The PARENT ThreadConfig, not its `llm_config` child: the latch fields
    # live on the parent, and pydantic rejects an unknown attribute silently
    # enough (a ValueError into the swallow below) to look like nothing
    # happened.
    if thread_config is None or not thread_id:
        return
    if getattr(thread_config, "rejected_llm_base_url", None) == rejected:
        return
    # One pending note, one slot. A fallback note already waiting is the end of
    # an episode the model is mid-way through and has not been told about yet;
    # overwriting it would drop that fact entirely. Deferring costs nothing:
    # nothing is written, so the guard above still misses next turn and the
    # notice re-latches once the slot is free.
    if getattr(thread_config, "pending_fallback_note", None):
        logger.debug(
            "Deferring the rejected-destination note for thread %s; a "
            "fallback note is already pending", thread_id,
        )
        return
    try:
        thread_config.rejected_llm_base_url = rejected
        thread_config.pending_fallback_note = rejected_destination_note_stamp(rejected)
        if host.thread_config_manager.save_config(thread_config):
            host.invalidate_thread_config_cache(thread_id)
        else:
            logger.warning(
                "Could not persist the rejected-base_url notice for thread %s; "
                "the destination was still refused", thread_id,
            )
    except Exception:  # noqa: BLE001 - the notice is not the control
        # WARNING, not debug. This swallow exists so a notice failure cannot
        # fail a turn, but it once hid a plain programming error (the wrong
        # config object was passed) for a full test run. A control that cannot
        # fail the turn still has to be able to complain.
        logger.warning(
            "Failed to latch rejected-destination note for thread %s; the "
            "destination was still refused", thread_id, exc_info=True,
        )


def _clear_rejected_destination(
    host: LLMConfigHost,
    thread_id: str,
    thread_config: Any,
) -> None:
    """Forget a past refusal once the thread names an accepted destination.

    Without this the field means "ever refused" and never stops meaning it: it
    would pin ``has_customizations()`` true forever, so the config file could
    never be deleted again, and a bad-then-fixed-then-same-bad sequence would
    be silent, because the latch guard above would still match. Clearing here
    makes it mean "currently refused", which is the only reading either of
    those behaviours is correct under.
    """
    if thread_config is None or not thread_id:
        return
    if getattr(thread_config, "rejected_llm_base_url", None) is None:
        return
    try:
        thread_config.rejected_llm_base_url = None
        if host.thread_config_manager.save_config(thread_config):
            host.invalidate_thread_config_cache(thread_id)
    except Exception:  # noqa: BLE001 - bookkeeping, not the control
        logger.warning(
            "Failed to clear the rejected-destination marker for thread %s",
            thread_id, exc_info=True,
        )


def rejected_destination_note_stamp(rejected: str) -> dict[str, Any]:
    """The once-only note for a per-thread base_url the gate refused.

    Reuses the fallback-note stamp rather than inventing a second notice path:
    the shape is the same (a persisted, once-only explanation that the
    EFFECTIVE config differs from the REQUESTED one) and, more importantly, the
    stamped ``text`` is the exact-suffix strip contract every reader already
    honours, so history strips it from the rendered bubble and re-emits it as a
    typed notice for free. ``kind="destination"`` is what stops it rendering as
    a model switch (``agent_history._fallback_notice_summary``).
    """
    from ..vendor.react_agent.nodes import fallback_note_stamp

    return fallback_note_stamp(
        {"reason": "unconfigured_destination"},
        kind="destination",
        phase="rejected",
        text=(
            "[System info]: This thread is configured with a custom model "
            f"endpoint ({rejected}) that this deployment is not set up to use, "
            "so it was ignored and the configured provider answered instead. "
            "The server's provider credential is deliberately never sent to an "
            "address the deployment has not been configured for. To use this "
            "endpoint, set an api_key on this thread as well, or ask an "
            "administrator to configure it. Tell the user this if it is "
            "relevant to what they asked."
        ),
    )


def clear_active_llm_fallback(
    host: LLMConfigHost,
    thread_id: str,
    *,
    reason: str,
):
    """Clear a thread's active fallback hold and latch the model-facing end note.

    The shared clear path for every revert/expiry surface (expiry sweep,
    ``/fallback revert``, the REST ``clear_active_fallback`` flag). Stamps
    ``ThreadConfig.pending_fallback_note`` so the next turn (any source) tells
    the model it is back on the primary (persisted-context principle; the
    latch shape is the ``fallback_note`` message stamp). ``reason`` is
    "expired" or "reverted". Returns the cleared ``ActiveLLMFallback`` record,
    or None when there was nothing to clear or the save failed.
    """
    if not thread_id:
        return None
    tc = host.thread_config_manager.get_config(thread_id)
    active = tc.active_llm_fallback if tc is not None else None
    if tc is None or active is None:
        return None

    tc.active_llm_fallback = None
    tc.pending_fallback_note = fallback_end_note_stamp(active, reason=reason)
    # Always save (never delete): the latch itself is state worth keeping even
    # on an otherwise-default config; the consume path runs the save-or-delete
    # choice once the latch is gone.
    if not host.thread_config_manager.save_config(tc):
        logger.warning("Failed to clear LLM fallback for thread %s", thread_id)
        return None
    host.invalidate_thread_config_cache(thread_id)
    logger.info(
        "Cleared %s LLM fallback for thread %s (%s/%s)",
        reason, thread_id, active.provider, active.model,
    )
    return active


def clear_expired_llm_fallback_if_idle(
    host: LLMConfigHost,
    thread_id: str,
) -> bool:
    """Clear an expired active fallback once no turn is using that thread."""
    if not thread_id:
        return False
    tc = host.thread_config_manager.get_config(thread_id)
    if tc is None:
        return False
    active = tc.active_llm_fallback
    if active is None or not _active_fallback_is_expired(active):
        return False
    if _thread_is_busy(host, thread_id):
        return False
    return clear_active_llm_fallback(host, thread_id, reason="expired") is not None


def consume_pending_fallback_note(
    host: LLMConfigHost,
    thread_id: str,
) -> dict[str, Any] | None:
    """Pop the latched end-note stamp for this thread's next turn, or None.

    Clearing the latch persists BEFORE the note is returned; if that save
    fails the note is withheld this turn (deferred to a later successful
    consume) rather than risking a duplicate injection every turn.
    """
    if not thread_id:
        return None
    tc = host.thread_config_manager.get_config(thread_id)
    note = getattr(tc, "pending_fallback_note", None) if tc is not None else None
    if not note:
        return None
    tc.pending_fallback_note = None
    saved = (
        host.thread_config_manager.save_config(tc)
        if tc.has_customizations()
        else host.thread_config_manager.delete_config(thread_id)
    )
    if not saved:
        logger.warning(
            "Failed to clear pending fallback note for thread %s; "
            "withholding it this turn", thread_id,
        )
        return None
    host.invalidate_thread_config_cache(thread_id)
    return dict(note)


def activate_temporary_llm_fallback(
    host: LLMConfigHost,
    thread_id: str,
    payload: dict[str, Any],
) -> dict[str, Any]:
    """Persist the selected fallback as this thread's temporary effective LLM.

    The hold duration is taken from a user-approved choice on the payload when
    present (``hold_permanent`` for an until-manually-reverted hold, or
    ``hold_seconds`` for a specific duration), otherwise from the global
    ``llm_fallback_hold_seconds`` default. A permanent hold persists an
    ``ActiveLLMFallback`` with ``expires_at=None``; a non-positive,
    non-permanent hold persists nothing (no cross-turn hold).
    """
    permanent = bool(payload.get("hold_permanent"))
    if not permanent:
        raw_hold = payload.get("hold_seconds")
        if raw_hold is None:
            raw_hold = getattr(host.settings, "llm_fallback_hold_seconds", 7200)
        try:
            hold_seconds = int(raw_hold or 0)
        except (TypeError, ValueError):
            hold_seconds = 7200
        hold_seconds = max(0, min(604800, hold_seconds))
    else:
        hold_seconds = 0

    if not thread_id or (not permanent and hold_seconds <= 0):
        return {"hold_seconds": hold_seconds, "expires_at": None, "permanent": permanent}

    provider = str(payload.get("to_provider") or "").strip()
    model = str(payload.get("to_model") or "").strip()
    source_provider = str(payload.get("from_provider") or "").strip()
    source_model = str(payload.get("from_model") or "").strip()
    if not provider or not model:
        return {"hold_seconds": hold_seconds, "expires_at": None, "permanent": permanent}

    activated_at = utc_now()
    expires_at = None if permanent else activated_at + timedelta(seconds=hold_seconds)
    tc = host.thread_config_manager.get_config(thread_id)
    if tc is None:
        tc = ThreadConfig(thread_id=thread_id)
    # A hold-end note latched by a previous hold's expiry/revert is obsolete
    # the moment a new hold activates: delivering "back on the primary" while
    # a different fallback is live would misinform the model. The swap note
    # for THIS activation supersedes it.
    tc.pending_fallback_note = None
    tc.active_llm_fallback = ActiveLLMFallback(
        provider=provider,
        model=model,
        source_provider=source_provider,
        source_model=source_model,
        hold_seconds=hold_seconds,
        activated_at=activated_at,
        expires_at=expires_at,
        provider_route=payload.get("to_provider_route"),
        openai_api_mode=payload.get("to_openai_api_mode"),
        reason=payload.get("reason"),
        http_status=payload.get("http_status"),
    )
    if not host.thread_config_manager.save_config(tc):
        logger.warning("Failed to activate LLM fallback for thread %s", thread_id)
    else:
        host.invalidate_thread_config_cache(thread_id)
        logger.warning(
            "Activated temporary LLM fallback for thread %s: %s/%s for %s",
            thread_id,
            provider,
            model,
            "permanent (until reverted)" if permanent else f"{hold_seconds}s",
        )
    return {
        "hold_seconds": hold_seconds,
        "expires_at": expires_at.isoformat() if expires_at is not None else None,
        "permanent": permanent,
    }


def get_llm_config_for_thread(
    host: LLMConfigHost,
    thread_id: str = "",
    acting_user_id: str | None = None,
) -> LLMConfig:
    """Build LLMConfig with per-thread overrides applied on top of global settings.

    ``acting_user_id`` is the turn's authenticated user, used ONLY as the
    credential-owner fallback when the thread has no ``thread_owners`` row
    (dev-todo #76): autonomous turns run on synthetic, never-claimed thread
    ids (``todo-<id>``, trigger threads), and without an owner the vault
    lookup narrows to system-owned credentials, silently diverging from the
    interactive path whenever the LLM credential is a user-owned vault
    record. An existing owner row always wins over the acting user.
    """
    if thread_id:
        clear_expired_llm_fallback_if_idle(host, thread_id)

    tc_obj = None
    tc = None
    active_fallback = None
    if thread_id:
        tc_obj = host.thread_config_manager.get_config(thread_id)
        if tc_obj:
            tc = tc_obj.llm_config
            active_fallback = tc_obj.active_llm_fallback

    def resolve(attr: str, global_value: Any) -> Any:
        thread_value = getattr(tc, attr, None) if tc else None
        return _resolve_thread_llm_override(thread_value, global_value)

    provider = normalize_llm_provider(resolve("provider", host.settings.llm_provider))
    configured_provider = provider
    global_provider = normalize_llm_provider(host.settings.llm_provider)
    if active_fallback:
        provider = normalize_llm_provider(active_fallback.provider)
    provider_route = resolve_provider_route(
        provider,
        route_override=(
            active_fallback.provider_route
            if active_fallback and active_fallback.provider_route
            else getattr(tc, "provider_route", None) if tc else None
        ),
        global_route=getattr(host.settings, "llm_provider_route", None),
    )
    openai_api_mode = (
        active_fallback.openai_api_mode
        if active_fallback and active_fallback.openai_api_mode
        else resolve("openai_api_mode", host.settings.openai_api_mode)
    )
    model = resolve("model", host.settings.llm_model)
    if active_fallback:
        model = active_fallback.model
    temperature = resolve("temperature", host.settings.llm_temperature)
    max_tokens = resolve("max_tokens", host.settings.llm_max_tokens)
    extended_thinking = resolve("extended_thinking", host.settings.llm_extended_thinking)
    reasoning_effort = resolve("reasoning_effort", host.settings.llm_reasoning_effort)
    # Clamp onto the resolved model's supported ladder so LLMConfig always
    # carries an effort value the provider can honor ("off" stays explicit and
    # wins over extended_thinking inside the provider factories).
    reasoning_effort = _clamp_reasoning_effort_for_model(
        provider,
        model,
        reasoning_effort,
        provider_route,
    )
    use_model_defaults = resolve("use_model_defaults", host.settings.llm_use_model_defaults)
    context_length_override = _positive_int(
        resolve("context_length", getattr(host.settings, "llm_context_length", None))
    )
    ollama_num_ctx_override = _positive_int(
        resolve("ollama_num_ctx", getattr(host.settings, "llm_ollama_num_ctx", None))
    )

    top_p = host.settings.llm_top_p
    frequency_penalty = host.settings.llm_frequency_penalty
    presence_penalty = host.settings.llm_presence_penalty

    # When use_model_defaults is enabled, don't send temperature/top_p/frequency_penalty/
    # presence_penalty — let the provider apply model-specific optimal defaults.
    if use_model_defaults:
        temperature = None
        top_p = None
        frequency_penalty = None
        presence_penalty = None

    owner_user_id = (
        host.accounts_repo.get_thread_owner(thread_id)
        if thread_id and hasattr(host, "accounts_repo")
        else None
    )
    if owner_user_id is None and acting_user_id:
        # Unclaimed (usually synthetic autonomous) thread: resolve
        # credentials as the acting user so their user-owned vault records
        # apply exactly as on an interactive thread. See the docstring.
        owner_user_id = acting_user_id
    credential_vault = getattr(host, "credential_vault", None)

    provider_credentials: dict[str, Any] = {}

    def vault_credential_for(credential_provider: str):
        credential_provider = normalize_llm_provider(credential_provider)
        if credential_provider not in provider_credentials:
            provider_credentials[credential_provider] = get_llm_provider_credential(
                credential_provider,
                vault=credential_vault,
                owner_user_id=owner_user_id,
                thread_id=thread_id or None,
            )
        return provider_credentials[credential_provider]

    def resolve_explicit_secret(value: str | None, credential_provider: str) -> str | None:
        return resolve_credential_references(
            value,
            vault=credential_vault,
            owner_user_id=owner_user_id,
            provider=credential_provider,
            thread_id=thread_id or None,
        )

    provider_credential = vault_credential_for(provider)

    # Resolve base_url: per-thread override > global when the thread is using
    # the global provider. Empty string ("") = explicit direct API.
    active_uses_configured_provider = (
        not active_fallback or provider == configured_provider
    )

    # SECURITY GATE (E10-01). The per-thread `base_url` is caller-supplied and
    # the route that sets it is deliberately NOT admin-gated, so without this a
    # non-admin names an address and the server mails the OPERATOR's provider
    # key to it, on every turn, along with the global system prompt. The check
    # lives HERE rather than on the route because `thread_configs/<id>.json` is
    # reachable by `file_write`, a seed tool (P4-03): a validated PATCH schema
    # is bypassed by one file write.
    #
    # A redirection is refused only when the OPERATOR's credential would ride
    # to it. A caller spending their OWN key at their own address is the
    # existing bring-your-own-endpoint capability and is left alone. There is
    # deliberately NO role exemption: the threat this placement was chosen for
    # is a config file planted by `file_write`, whose author is not the thread
    # owner, and on the documented solo deployment the default user IS the
    # admin, so an admin exemption would disable the control exactly where the
    # planted-file threat lives.
    #
    # Ask about the key that will ACTUALLY SHIP, not about whether the caller
    # owns one somewhere. The two are different questions because the key
    # precedence below is thread override > vault record > environment: a
    # caller who owns any user-owned record for this provider owns "a"
    # credential, but if their thread also names the operator's record as an
    # explicit override then the operator's key is what leaves. So this
    # mirrors that precedence exactly, arm for arm.
    thread_api_key_raw = resolve("api_key", None) if active_uses_configured_provider else None
    thread_api_key = (
        resolve_explicit_secret(thread_api_key_raw, provider)
        if thread_api_key_raw is not None
        else None
    )
    # ONE question, asked once and reused by the assignment far below. The
    # RESOLVED key decides, because a falsy one is not "no key": the provider
    # factory backfills the operator's env key whenever `config.api_key` is
    # empty, so an empty secret field would otherwise buy a caller the operator
    # credential at an address of their choosing while looking like they had
    # brought their own. An empty override therefore falls through to the arms
    # below, on BOTH sites.
    #
    # They have to stay one variable rather than two equivalent-looking tests.
    # When this asked about the resolved value and the assignment asked about
    # raw presence, a `${credential:<own-record>.<empty-field>}` override was a
    # complete bypass: provenance skipped to the vault arm and answered "the
    # caller's own key", while the empty string shipped and the factory
    # backfilled the operator's. One record satisfied both halves.
    thread_supplies_key = bool(thread_api_key)
    if thread_supplies_key:
        key_is_callers_own = secret_is_caller_owned(
            thread_api_key_raw, vault=credential_vault, owner_user_id=owner_user_id
        )
    elif provider_credential and provider_credential.api_key:
        # The vault lookup already narrows to records this caller may read, so
        # a user-owned one here is the caller's own.
        key_is_callers_own = provider_credential.owner_type == "user"
    else:
        # Environment or settings, i.e. the deployment's.
        key_is_callers_own = False

    rejected_destination: str | None = None

    def refuse_destination(
        candidate: str | None, *, raw: str | None, reported: str | None = None
    ) -> bool:
        """True when *candidate* may not carry the key resolved above.

        Applied at EVERY caller-provenance destination source, not just the
        per-thread field. A vault record the caller wrote carries a base_url
        too, and it is consulted even by a thread that names no destination of
        its own, so gating only the obvious field left the same theft one
        indirection away.
        """
        nonlocal rejected_destination
        if not candidate or key_is_callers_own:
            return False
        if not destination_redirects_away_from_config(
            candidate,
            configured=configured_llm_destinations(
                provider, provider_route=provider_route, settings=host.settings
            ),
        ):
            return False
        # Fall through to the configured destination rather than refusing the
        # turn: the operator's rule is to make the bad artifact inert, not to
        # stop the agent acting. The user is told once, via the note latched
        # below. First refusal wins the notice; they name the same problem.
        if rejected_destination is None:
            rejected_destination = reported or _reportable_destination(raw, candidate)
        logger.warning(
            "Thread %s names LLM destination %s, which is not one this "
            "deployment is configured for; ignoring it and using the "
            "configured provider so the operator credential is not sent "
            "there. Set the thread's own api_key to use this address.",
            thread_id,
            rejected_destination,
        )
        return True

    base_url: str | None = None
    thread_named_destination = bool(
        tc and tc.base_url is not None and active_uses_configured_provider
    )
    if thread_named_destination and tc is not None:
        candidate = resolve_explicit_secret(tc.base_url or None, provider)
        if refuse_destination(candidate, raw=tc.base_url):
            thread_named_destination = False
        else:
            base_url = candidate

    if not thread_named_destination:
        if provider != global_provider:
            # Per-thread provider differs from global. CLIProxy fronts both
            # providers on one host, so derive the matching URL from the
            # global one. Without this, the "Anthropic (Subscription)"
            # per-thread option silently falls through to api.anthropic.com
            # direct + ANTHROPIC_DIRECT_API_KEY, billing per-token instead of
            # using the subscription, and the openai arm lands on
            # api.openai.com holding a proxy-local key that will not
            # authenticate there. Derived from settings, so this arm is CONFIG
            # provenance by construction and the gate above must never see it.
            base_url = cliproxy_base_url_for_provider(provider, host.settings.llm_base_url)
        else:
            base_url = resolve_explicit_secret(host.settings.llm_base_url, provider)

    if not base_url and provider_credential and provider_credential.base_url:
        # The second caller-provenance destination source. A SYSTEM-owned
        # record is the operator's own configuration and is not gated; a
        # user-owned one is a caller-supplied address like any other, and
        # `auth_write` lets an agent create those.
        #
        # Tested as "is it explicitly the operator's", NOT as "is it not the
        # caller's". The two spellings agree on the two real values and differ
        # on everything else, and the difference decides whether an UNKNOWN
        # provenance is gated or waved through. This site and the
        # `key_is_callers_own` one above want opposite defaults, so neither
        # value of the field is safe at both: the only arrangement that fails
        # closed at both is for each to name the value it is willing to act on.
        if provider_credential.owner_type == "system" or not refuse_destination(
            provider_credential.base_url,
            raw=provider_credential.base_url,
            # Named by provenance rather than keyed. The value is a decrypted
            # vault field, so it gets the treatment an interpolated one gets:
            # a base URL that arrived as a secret must not be keyed into a
            # WARNING log, a persisted field and the model's next turn just
            # because it happens not to spell `${credential:...}` itself.
            reported="an address from a stored credential",
        ):
            base_url = provider_credential.base_url
    if not base_url:
        base_url = resolve_provider_base_url(
            provider,
            provider_route=provider_route,
            settings=host.settings,
            include_default=False,
        )

    if rejected_destination is not None:
        _latch_rejected_destination_note(host, thread_id, tc_obj, rejected_destination)
    else:
        # Unconditional, not just when a destination was accepted: removing the
        # offending base_url is the natural fix and has to clear the marker too,
        # or it would stay set forever. Cheap, because the clear early-returns
        # when there is nothing to forget.
        _clear_rejected_destination(host, thread_id, tc_obj)

    # Resolve API key along the precedence `key_is_callers_own` mirrors above:
    # per-thread override → vault record → per-provider env key → generic
    # proxy-mode key. Lets a thread point at a different CLIProxy sidecar with
    # its own auth without touching global settings.
    if thread_supplies_key:
        api_key = thread_api_key
    elif provider_credential and provider_credential.api_key:
        api_key = provider_credential.api_key
    else:
        if provider == "anthropic":
            # A configured Anthropic base_url means CLIProxy or another
            # proxy; it expects ANTHROPIC_API_KEY (usually cpx-*). Only use
            # ANTHROPIC_DIRECT_API_KEY for direct Anthropic calls.
            api_key = (
                host.settings.anthropic_api_key
                if base_url
                else (
                    host.settings.anthropic_direct_api_key
                    or host.settings.anthropic_api_key
                )
            )
        else:
            api_key = resolve_provider_api_key(provider, settings=host.settings)

    probe_base_url = base_url
    if not probe_base_url and provider in _LOCAL_PROVIDER_IDS:
        probe_base_url = resolve_provider_base_url(
            provider,
            provider_route=provider_route,
            settings=host.settings,
            include_default=True,
        )

    context_length = context_length_override
    ollama_num_ctx = ollama_num_ctx_override
    if model and probe_base_url and is_local_llm_base_url(probe_base_url):
        server_type = detect_local_server_type(probe_base_url, api_key=api_key)
        if context_length is None:
            context_length = query_local_context_length(
                str(model),
                probe_base_url,
                api_key=api_key,
                server_type=server_type,
            )
        if ollama_num_ctx is None and (server_type == "ollama" or provider == "ollama"):
            detected_num_ctx = query_ollama_num_ctx(
                str(model),
                probe_base_url,
                api_key=api_key,
            )
            if detected_num_ctx:
                if context_length_override and detected_num_ctx > context_length_override:
                    ollama_num_ctx = context_length_override
                else:
                    ollama_num_ctx = detected_num_ctx

    if model and context_length:
        register_model_metadata(
            model_id=str(model),
            name=str(model),
            context_length=context_length,
        )

    def base_url_for_provider(fallback_provider: str) -> str | None:
        if fallback_provider == provider:
            return base_url
        fallback_credential = vault_credential_for(fallback_provider)
        if fallback_provider == global_provider:
            resolved_global_base = resolve_explicit_secret(
                host.settings.llm_base_url,
                fallback_provider,
            )
            if resolved_global_base:
                return resolved_global_base
        # Never reads the per-thread base_url, so a refused destination cannot
        # reappear on a fallback candidate: this chain is settings, vault and
        # registry only.
        derived = cliproxy_base_url_for_provider(fallback_provider, host.settings.llm_base_url)
        if derived:
            return derived
        if fallback_credential and fallback_credential.base_url:
            return fallback_credential.base_url
        env_base_url = resolve_provider_base_url(
            fallback_provider,
            provider_route=resolve_provider_route(
                fallback_provider,
                global_route=getattr(host.settings, "llm_provider_route", None),
            ),
            settings=host.settings,
            include_default=False,
        )
        if env_base_url:
            return env_base_url
        return None

    def api_key_for_provider(
        fallback_provider: str,
        fallback_base_url: str | None,
    ) -> str | None:
        if fallback_provider == provider and thread_supplies_key:
            # The guard makes the two providers the same string, so this is
            # exactly the key resolved above (see `thread_supplies_key`); it
            # used to re-resolve, decrypting the same secret a second time.
            return thread_api_key
        fallback_credential = vault_credential_for(fallback_provider)
        if fallback_credential and fallback_credential.api_key:
            return fallback_credential.api_key
        if fallback_provider == "anthropic":
            return (
                host.settings.anthropic_api_key
                if fallback_base_url
                else (
                    host.settings.anthropic_direct_api_key
                    or host.settings.anthropic_api_key
                )
            )
        return resolve_provider_api_key(fallback_provider, settings=host.settings)

    fallbacks: list[LLMFallbackConfig] = []
    for fallback_ref in _parse_llm_fallback_models(
        getattr(host.settings, "llm_fallback_models", "")
    ):
        fallback_provider, fallback_model = _split_llm_fallback_ref(
            fallback_ref,
            str(provider),
        )
        if not fallback_model or (
            fallback_provider == provider and fallback_model == model
        ):
            continue
        fallback_base_url = base_url_for_provider(fallback_provider)
        fallback_route = (
            provider_route
            if fallback_provider == provider
            else resolve_provider_route(
                fallback_provider,
                global_route=getattr(host.settings, "llm_provider_route", None),
            )
        )
        fallbacks.append(
            LLMFallbackConfig(
                provider=fallback_provider,
                provider_route=fallback_route,
                model=fallback_model,
                api_key=api_key_for_provider(
                    fallback_provider,
                    fallback_base_url,
                ),
                base_url=fallback_base_url,
                openai_api_mode=resolve(
                    "openai_api_mode",
                    host.settings.openai_api_mode,
                ),
                context_length=None,
                ollama_num_ctx=None,
            )
        )

    # Consent policy for model switches (transport fallback + refusal swap):
    # effective mode = thread override else global, None-inherit via resolve().
    # The policy crosses the vendored boundary ONLY inside the decision
    # callback's closure; LLMConfig carries no mode fields.
    fallback_switch_mode = resolve(
        "fallback_switch_mode", getattr(host.settings, "llm_fallback_switch_mode", "auto")
    )
    refusal_swap_mode = resolve(
        "refusal_swap_mode", getattr(host.settings, "llm_refusal_swap_mode", "ask")
    )
    fallback_prompt_timeout = getattr(
        host.settings, "llm_fallback_prompt_timeout_seconds", 180
    )
    decision_callback = None
    if thread_id:
        from .fallback_approvals import make_fallback_decision_callback

        decision_callback = make_fallback_decision_callback(
            thread_id=thread_id,
            user_id=str(owner_user_id or acting_user_id or ""),
            switch_mode=fallback_switch_mode,
            refusal_mode=refusal_swap_mode,
            prompt_timeout_seconds=fallback_prompt_timeout,
            default_hold_seconds=getattr(host.settings, "llm_fallback_hold_seconds", 7200),
        )

    return LLMConfig(
        provider=provider,
        model=model,
        api_key=api_key,
        base_url=base_url,
        temperature=temperature,
        max_tokens=max_tokens,
        top_p=top_p,
        top_k=host.settings.llm_top_k,
        frequency_penalty=frequency_penalty,
        presence_penalty=presence_penalty,
        reasoning_effort=reasoning_effort,
        extended_thinking=extended_thinking,
        context_length=context_length,
        ollama_num_ctx=ollama_num_ctx,
        provider_route=provider_route,
        openai_api_mode=openai_api_mode,
        stream_max_retries=host.settings.llm_stream_max_retries,
        stream_retry_initial_delay=host.settings.llm_stream_retry_initial_delay,
        stream_retry_max_delay=host.settings.llm_stream_retry_max_delay,
        fallback_hold_seconds=getattr(host.settings, "llm_fallback_hold_seconds", 7200),
        fallbacks=fallbacks,
        fallback_activation_callback=(
            (lambda payload: activate_temporary_llm_fallback(host, thread_id, payload))
            if thread_id
            else None
        ),
        fallback_decision_callback=decision_callback,
    )
