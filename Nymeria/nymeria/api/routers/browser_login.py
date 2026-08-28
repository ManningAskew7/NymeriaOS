"""HTTP surface for the human login handoff into the agent's browser.

Three wires, one session (see :mod:`nymeria.core.browser_login_sessions`):

* ``POST /browser-login/{session_id}/frame`` takes screencast frames UP
  from the Chrome extension.
* ``GET /browser-login/{session_id}/stream`` sends them DOWN to
  nymeria-desktop as SSE, and is the ONLY route frame bytes ever leave by.
* ``POST /browser-login/{session_id}/input`` takes the operator's
  keystrokes and clicks and publishes them as a chrome-only,
  fire-and-forget ``browser_login_input`` event for the extension to
  replay as trusted input (the ``browser_session_release`` shape: no
  command id, no result POST, because there is nothing to wait for).

Plus ``/end`` (the viewer's Done button) and a listing so a desktop that
reloads mid-login can find its way back to the session.

**Owner-only, with no admin exemption.** Every sibling browser route lets
an admin act for another user; these do not. What crosses these wires is a
live picture of somebody typing their password, so "an admin may watch it"
is a capability nobody asked for and the whole handoff exists to prevent.
Non-owners get 404 rather than 403, so the route never confirms that
another user's session exists.
"""

from __future__ import annotations

import json
import logging
from collections.abc import Callable
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Query
from fastapi.responses import StreamingResponse

from ...core.accounts import AuthenticatedUser
from ...core.browser_login_sessions import (
    STATE_ACTIVE,
    BrowserLoginSession,
    get_browser_login_registry,
)
from ...core.event_bus import publish_autonomous_event
from ..schemas.browser_login import (
    LoginFrameAck,
    LoginFrameBatch,
    LoginInputAck,
    LoginInputBatch,
    LoginSessionEndRequest,
    LoginSessionList,
    LoginSessionStatus,
)
from ..sse import SSE_RESPONSE_HEADERS, with_sse_keepalive

logger = logging.getLogger(__name__)


def create_browser_login_router(
    verify_api_key: Callable[..., Any],
) -> APIRouter:
    router = APIRouter(tags=["Browser Login"])

    def _owned_session(session_id: str, user: AuthenticatedUser) -> BrowserLoginSession:
        """The caller's own live session, or 404.

        One helper so no route can accidentally grow a different ownership
        rule; 404 (not 403) for someone else's session so existence never
        leaks.
        """
        session = get_browser_login_registry().get(session_id)
        if session is None or session.user_id != user.id:
            raise HTTPException(
                status_code=404,
                detail={
                    "code": "login_session_not_found",
                    "message": "No active login session with that id.",
                },
            )
        return session

    @router.post(
        "/browser-login/{session_id}/frame",
        response_model=LoginFrameAck,
    )
    async def post_login_frames(
        session_id: str,
        body: LoginFrameBatch,
        user: AuthenticatedUser = Depends(verify_api_key),
    ) -> LoginFrameAck:
        """Buffer screencast frames from the extension.

        A session that has already ended answers ``session_active: false``
        instead of erroring, which is the extension's cue to stop the
        screencast: a backend-side ending (Done, the time limit, a thread
        abort) otherwise never reaches an extension that is still capturing.
        """
        registry = get_browser_login_registry()
        session = registry.get(session_id)
        if session is None or session.user_id != user.id:
            # Swept, ended, or not theirs: same answer, and the same cue.
            return LoginFrameAck(accepted=0, session_active=False)
        accepted = 0
        last_seq = 0
        for frame in body.frames:
            # Buffered WITH its wire type, so the SSE route below can yield
            # the stored payload byte-identically instead of re-serializing
            # every frame (the turn-stream buffer's rule, for the same
            # reason: the buffer holds what the client receives).
            seq = session.append_frame(
                {
                    "type": "login_frame",
                    "data": frame.data,
                    "metadata": frame.metadata,
                }
            )
            if seq:
                accepted += 1
                last_seq = seq
        return LoginFrameAck(
            accepted=accepted,
            session_active=session.state == STATE_ACTIVE,
            last_seq=last_seq,
        )

    @router.get("/browser-login/{session_id}/stream")
    async def stream_login_frames(
        session_id: str,
        from_seq: int = Query(
            0, ge=0, description="Send only frames with seq greater than this."
        ),
        user: AuthenticatedUser = Depends(verify_api_key),
    ):
        """Tail a login session's frames as SSE (the desktop viewer's feed).

        Opens with a ``login_attach`` meta event carrying the session's
        status so the viewer can render its countdown and "you are driving"
        banner before any frame arrives, then streams ``login_frame``
        events until the session ends, closing with ``login_end``.

        A viewer slower than the screencast silently skips the frames it
        outran, which is right for video: the newer frame already shows
        everything the skipped one did.
        """
        session = _owned_session(session_id, user)

        async def frame_generator():
            attach = {"type": "login_attach", **session.snapshot()}
            yield f"data: {json.dumps(attach)}\n\n"
            async for payload in session.stream_frames(from_seq=from_seq):
                yield f"data: {payload}\n\n"
            end = {"type": "login_end", **session.snapshot()}
            yield f"data: {json.dumps(end)}\n\n"

        return StreamingResponse(
            with_sse_keepalive(frame_generator()),
            media_type="text/event-stream",
            headers=SSE_RESPONSE_HEADERS,
        )

    @router.post(
        "/browser-login/{session_id}/input",
        response_model=LoginInputAck,
    )
    async def post_login_input(
        session_id: str,
        body: LoginInputBatch,
        user: AuthenticatedUser = Depends(verify_api_key),
    ) -> LoginInputAck:
        """Send the operator's input to the tab they are signing into.

        Fire-and-forget by design: there is no result to wait for and a
        round trip per keystroke would put the typing latency on the wrong
        side of the network. The operator sees their keystroke land in the
        next screencast frame, which is the real acknowledgement.
        """
        session = _owned_session(session_id, user)
        publish_autonomous_event(
            event_type="browser_login_input",
            thread_id=session.thread_id,
            user_id=session.user_id,
            task_id="",
            data={
                "session_id": session.session_id,
                "tab_id": session.tab_id,
                "events": [event.model_dump(exclude_none=True) for event in body.events],
            },
        )
        return LoginInputAck(dispatched=len(body.events), session_active=True)

    @router.post(
        "/browser-login/{session_id}/end",
        response_model=LoginSessionStatus,
    )
    async def end_login_session(
        session_id: str,
        body: LoginSessionEndRequest,
        user: AuthenticatedUser = Depends(verify_api_key),
    ) -> LoginSessionStatus:
        """End a session: the viewer's Done (or its close) button.

        Ending wakes the waiting agent, drains the viewer's stream, and
        hands the tab back, so the agent can carry on with a browser that
        is now signed in.
        """
        session = _owned_session(session_id, user)
        get_browser_login_registry().finish(session_id, reason=body.reason)
        logger.info(
            "browser login session %s ended by its owner (reason=%s, frames=%d)",
            session_id,
            body.reason,
            session.frames_received,
        )
        return LoginSessionStatus(**session.snapshot())

    @router.get("/browser-login/sessions", response_model=LoginSessionList)
    async def list_login_sessions(
        user: AuthenticatedUser = Depends(verify_api_key),
    ) -> LoginSessionList:
        """The caller's live login sessions.

        The recovery path: a desktop that reloads mid-login finds its way
        back to the viewer from here rather than stranding the operator in
        front of a tab nothing is showing.
        """
        return LoginSessionList(
            sessions=[
                LoginSessionStatus(**session.snapshot())
                for session in get_browser_login_registry().active_for_user(user.id)
            ]
        )

    return router


__all__ = ["create_browser_login_router"]
