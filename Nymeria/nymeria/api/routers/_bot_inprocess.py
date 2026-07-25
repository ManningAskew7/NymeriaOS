"""Shared in-process Nymeria API adapter for the API-hosted webhook bot clients.

The webhook bot routers (whatsapp, teams) drive the agent through an identical
adapter object: the native ``NymeriaXxxBot`` thin-client is constructed with
this adapter as its ``api`` and calls
``api.claim_*``/``api.chat_stream``/``api.stop`` etc. The class bodies were
byte-identical apart from three per-platform axes, now injected:

- ``origin_client_id``: the lowercase wire id stamped on outbound sync events
  (e.g. ``"whatsapp"``, ``"teams"``).
- ``display_name``: the human-readable platform name used only in best-effort
  ``logger.debug`` breadcrumbs (e.g. ``"WhatsApp"``, ``"Microsoft Teams"``).
- ``error_cls``: the platform's own ``BotAPIError`` class (each native bot module
  defines its own and catches it with ``except BotAPIError``), so the adapter must
  raise that exact class for the native handler to keep matching.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Callable
from typing import Any, Optional, cast

from ...core.accounts import AuthenticatedUser, Provider
from ...core.chat_bindings import (
    BindClaimError,
    BindCodeInvalid,
    BindingAlreadyExists,
    claim_platform_link,
    claim_thread_bind,
)
from ...core.command_service import (
    CommandBackendClient,
    CommandContext,
    get_command_service,
)
from ...core.interactive_admission import (
    InteractiveCapacityError,
    admit_interactive_turn,
)
from .threads import _thread_list_platform

logger = logging.getLogger(__name__)


class InProcessBotAPI:
    """Nymeria API adapter shared by the API-hosted webhook bot clients."""

    def __init__(
        self,
        *,
        agent: Any,
        require_thread_access_fn: Callable[[AuthenticatedUser, str], None],
        publish_sync_event_fn: Callable[..., Any],
        origin_client_id: str,
        display_name: str,
        # Callable, not type[Exception]: each platform's BotAPIError ctor takes a
        # status_code kwarg that BaseException.__init__ does not (type[Exception]
        # would make the checker reject the status_code= call sites).
        error_cls: Callable[..., Exception],
    ) -> None:
        self.agent = agent
        self._require_thread_access = require_thread_access_fn
        self._publish_sync_event = publish_sync_event_fn
        self._origin_client_id = origin_client_id
        self._display_name = display_name
        self._error_cls = error_cls

    async def resolve_platform_user(self, platform: str, platform_user_id: str) -> Optional[str]:
        return self.agent.accounts_repo.resolve_platform(platform, platform_user_id)

    async def list_chatapp_bindings(self, provider: str) -> list[dict[str, Any]]:
        return [
            {
                "id": binding.id,
                "thread_id": binding.thread_id,
                "provider": binding.provider,
                "platform_chat_id": binding.platform_chat_id,
                "user_id": binding.user_id,
                "created_at": binding.created_at,
                "user_telegram_bot_id": binding.user_telegram_bot_id,
            }
            for binding in self.agent.chat_bindings_repo.list_thread_bindings_global(
                provider=provider
            )
        ]

    async def claim_platform_link_code(
        self,
        *,
        code: str,
        provider: str,
        platform_user_id: str,
    ) -> dict[str, Any]:
        try:
            platform = claim_platform_link(
                self.agent.accounts_repo,
                self.agent.chat_bindings_repo,
                code=code,
                provider=cast(Provider, provider),
                platform_user_id=platform_user_id,
            )
        except BindCodeInvalid as exc:
            raise self._error_cls(f"Invalid code: {exc}", status_code=400) from exc
        except BindClaimError as exc:
            raise self._error_cls(exc.reason, status_code=exc.http_status) from exc
        return {
            "user_id": platform.user_id,
            "provider": provider,
            "provider_user_id": platform_user_id,
            "created_at": platform.created_at,
        }

    async def claim_thread_bind_code(
        self,
        *,
        code: str,
        provider: str,
        platform_chat_id: str,
        expected_provider_user_id: str,
    ) -> dict[str, Any]:
        try:
            binding = claim_thread_bind(
                self.agent.accounts_repo,
                self.agent.chat_bindings_repo,
                code=code,
                provider=cast(Provider, provider),
                platform_chat_id=platform_chat_id,
                expected_provider_user_id=expected_provider_user_id,
            )
        except BindCodeInvalid as exc:
            raise self._error_cls(f"Invalid code: {exc}", status_code=400) from exc
        except BindingAlreadyExists as exc:
            raise self._error_cls(str(exc), status_code=409) from exc
        except BindClaimError as exc:
            raise self._error_cls(exc.reason, status_code=exc.http_status) from exc
        self._publish_platform_sync(binding.thread_id, binding.user_id)
        return {
            "binding_id": binding.id,
            "thread_id": binding.thread_id,
            "user_id": binding.user_id,
        }

    async def unbind_chatapp_by_chat(
        self,
        *,
        provider: str,
        platform_chat_id: str,
        user_id: Optional[str] = None,
    ) -> dict[str, Any]:
        repo = self.agent.chat_bindings_repo
        binding = repo.lookup_thread_binding_by_chat(provider, platform_chat_id)
        if binding is None:
            return {"unbound": False}
        if user_id is not None and binding.user_id != user_id:
            raise self._error_cls(
                "Binding belongs to a different user",
                status_code=403,
            )
        repo.delete_thread_binding(binding.id, user_id=binding.user_id)
        self._publish_platform_sync(binding.thread_id, binding.user_id)
        return {"unbound": True, "thread_id": binding.thread_id}

    async def execute_command(
        self,
        command: str,
        *,
        thread_id: Optional[str] = None,
        source: str = "user",
        actor: Optional[str] = None,
        surface: Optional[str] = None,
        user_id: Optional[str] = None,
    ) -> dict[str, Any]:
        """Execute a backend slash command in-process.

        Mirrors ``POST /commands/execute`` (and the HTTP client's
        ``NymeriaAPIClient.execute_command`` signature and return shape) so the
        webhook bots can forward slash commands through the same adapter they
        already chat through. ``surface`` defaults to this adapter's
        ``origin_client_id``, which matches the ``CommandSurface`` literal for
        every webhook platform.
        """
        authed = self._authenticated_user(user_id)
        ctx = CommandContext(
            user_id=authed.id,
            thread_id=thread_id,
            source=cast(Any, source),
            actor=cast(Any, actor),
            surface=cast(Any, surface or self._origin_client_id),
            is_admin=authed.role == "admin",
            via_act_as=True,
        )
        backend = CommandBackendClient.from_context(ctx, agent=self.agent, user=authed)
        result = await get_command_service().execute(ctx, command, api=backend)
        return {
            "success": result.success,
            "markdown": result.markdown,
            "command": result.command,
            "level": result.level,
            "data": result.data,
        }

    async def stop(self, thread_id: str, user_id: Optional[str] = None) -> dict[str, Any]:
        from ...core.pending_prompt_queue import restored_prompts_payload

        authed = self._authenticated_user(user_id)
        self._require_thread_access(authed, thread_id)
        lock_info = self.agent._thread_locks.get_lock_info(thread_id)
        if lock_info:
            restored = self.agent.abort_with_cascade(thread_id, restore_queue=True)
            return {
                "status": "stopping",
                "thread_id": thread_id,
                "restored_prompts": restored_prompts_payload(restored),
            }
        return {"status": "idle", "thread_id": thread_id, "restored_prompts": []}

    def chat_stream(
        self,
        message: str,
        thread_id: str,
        user_id: str,
    ):
        return self._chat_stream(message, thread_id, user_id)

    async def _chat_stream(self, message: str, thread_id: str, user_id: str):
        from ...core.bot_reactions import set_turn_origin

        authed = self._authenticated_user(user_id)
        self._require_thread_access(authed, thread_id)
        # Mirror the HTTP route's platform_origin stamping: the adapter's
        # origin_client_id IS the platform name ("whatsapp"/"teams"). The
        # webhook layer does not thread message ids down here, so the ids
        # stay empty; that is enough for the platform-aware backend gates
        # (the fallback-consent park gate must see these turns as bot-origin
        # on a button-less platform and auto-swap instead of parking), and
        # the react tool has no executor bot on these platforms anyway.
        set_turn_origin(
            thread_id,
            platform=self._origin_client_id,
            channel_id="",
            message_id="",
        )
        # Global interactive-turn admission (backlog #83): the adapter is an
        # in-process mirror of POST /chat, so its turns draw against the same
        # ceiling. Busy-thread prompts pass through (they queue onto the
        # running holder). Shed before the message_added publish so other
        # clients never render a user message that was not run.
        turn_slot = await self._admit_interactive_turn(thread_id)
        try:
            self._publish_sync_event(
                event_type="message_added",
                thread_id=thread_id,
                user_id=user_id,
                data={"role": "user", "content": message},
                origin_client_id=self._origin_client_id,
            )
            async for chunk in self.agent.astream(
                message,
                thread_id=thread_id,
                user_id=user_id,
            ):
                # Lost the lock race and queued instead of holding: give the
                # slot back while this stream observes the holder turn.
                if (
                    turn_slot is not None
                    and isinstance(chunk, dict)
                    and chunk.get("type") == "prompt_queued"
                ):
                    turn_slot.release()
                yield chunk
        finally:
            if turn_slot is not None:
                turn_slot.release()
        done: dict[str, Any] = {
            "type": "done",
            "thread_id": thread_id,
        }
        try:
            done["context_stats"] = self.agent.get_context_stats(thread_id)
            done["model"] = (
                self.agent._get_llm_config_for_thread(thread_id).model
                or self.agent.settings.llm_model
            )
        except Exception:  # noqa: BLE001
            logger.debug("%s done metadata failed", self._display_name, exc_info=True)
        try:
            title = self.agent.thread_metadata_manager.auto_title(
                user_id, thread_id, message
            )
            if title:
                done["title"] = title
                done["title_source"] = "auto"
                self._publish_sync_event(
                    event_type="thread_updated",
                    thread_id=thread_id,
                    user_id=user_id,
                    data={"title": title, "title_source": "auto"},
                    origin_client_id=self._origin_client_id,
                )
        except Exception:  # noqa: BLE001
            logger.debug("%s auto-title failed", self._display_name, exc_info=True)
        yield done

    async def chat(self, message: str, thread_id: str, user_id: str) -> dict[str, Any]:
        from .chat import run_sync_turn_with_tool_count

        authed = self._authenticated_user(user_id)
        self._require_thread_access(authed, thread_id)
        # Global interactive-turn admission (backlog #83); mirrors /chat/sync.
        turn_slot = await self._admit_interactive_turn(thread_id)
        # Off the event loop: the sync turn would otherwise block every
        # stream and probe in the process until it finishes. The tool count
        # is read on the same worker thread (race-free under concurrency).
        try:
            response, tool_call_count = await asyncio.to_thread(
                run_sync_turn_with_tool_count,
                self.agent,
                message,
                thread_id=thread_id,
                user_id=user_id,
            )
        finally:
            if turn_slot is not None:
                turn_slot.release()
        return {
            "response": response,
            "thread_id": thread_id,
            "tool_call_count": tool_call_count,
        }

    async def _admit_interactive_turn(self, thread_id: str):
        """Admission-check one adapter turn, shedding as the platform error.

        Returns the slot to release at turn end, or ``None`` when exempt
        (busy thread). The native bot handlers already catch their
        ``BotAPIError`` and relay the message to the user.
        """
        try:
            return await admit_interactive_turn(
                self.agent,
                getattr(self.agent, "settings", None),
                thread_id,
            )
        except InteractiveCapacityError as exc:
            raise self._error_cls(exc.detail, status_code=429) from exc

    def _authenticated_user(self, user_id: Optional[str]) -> AuthenticatedUser:
        if not user_id:
            raise self._error_cls("User not found", status_code=404)
        user = self.agent.accounts_repo.get_user_by_id(user_id)
        if user is None or user.disabled:
            raise self._error_cls("User not found", status_code=404)
        return AuthenticatedUser(
            id=user.id,
            email=user.email,
            display_name=user.display_name,
            role=user.role,
            via_act_as=True,
        )

    def _publish_platform_sync(self, thread_id: str, user_id: str) -> None:
        try:
            meta = self.agent.thread_metadata_manager.get_thread(user_id, thread_id)
            self._publish_sync_event(
                event_type="thread_updated",
                thread_id=thread_id,
                user_id=user_id,
                data={"platform": _thread_list_platform(self.agent, thread_id, meta)},
                origin_client_id=self._origin_client_id,
            )
        except Exception:  # noqa: BLE001
            logger.debug("%s platform sync publish failed", self._display_name, exc_info=True)
