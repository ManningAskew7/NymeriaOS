"""The parent-side RPC pump: one authenticated socket peer per run.

Frames are newline-delimited JSON (safe because both ends are ours and
``json.dumps`` never emits raw newlines) with a ``request id`` on every call
so the transport supports concurrent in-flight calls later without change;
the v1 child client is lockstep.

Frame shapes:

- child -> parent  ``{"t": "hello", "token": str}``
- parent -> child  ``{"t": "welcome", "verbs": {name: {"positional": [...]}}}``
- child -> parent  ``{"t": "call", "id": int, "verb": str, "args": {}}``
- parent -> child  ``{"t": "result", "id": int, "ok": bool, "value"|"error": ...}``
- child -> parent  ``{"t": "finish", "status": str, "output"|"error": ...}``

The pump enforces: token auth (constant-time compare), a single accepted
connection, budget charging BEFORE dispatch, the per-verb timeout, result
size caps, and step-trace recording. It never raises into the server loop;
every failure becomes an error frame or a recorded rejection.
"""

from __future__ import annotations

import asyncio
import hmac
import json
import logging
import time
from typing import Any, Optional

from .budget import WorkflowBudgetExceeded
from .envelope import (
    KIND_BUDGET_EXCEEDED,
    KIND_RUNNER_ERROR,
    KIND_VERB_ERROR,
)
from .registry import VerbContext, VerbError, resolve_verb, verb_metadata
from .trace import StepTrace

logger = logging.getLogger(__name__)

# Generous per-frame ceiling; per-verb payloads are capped separately.
FRAME_LIMIT_BYTES = 8 * 1024 * 1024


class VerbPump:
    """Serves one workflow child over one authenticated connection."""

    def __init__(self, *, ctx: VerbContext, trace: StepTrace, token: str) -> None:
        self._ctx = ctx
        self._trace = trace
        self._token = token
        self._accepted = False
        self._accepted_task: Optional[asyncio.Task] = None
        self._tasks: set = set()
        self._done = asyncio.Event()
        self.auth_failures = 0
        # The child's terminal report, if one arrived.
        self.finish: Optional[dict] = None

    async def wait_terminal(self) -> None:
        """Block until the accepted connection reaches a terminal state.

        Set when the pump receives the child's ``finish`` frame (``self.finish``
        is populated first, in ``_loop``, then ``_done`` in ``serve``'s finally)
        or the connection drops. This is the RELIABLE run-completion signal:
        ``proc.wait()`` can block until the child's stdout/stderr pipes close,
        which a backgrounded grandchild holds open long after the child exits,
        so the executor races THIS against ``proc.wait()`` rather than trusting
        the process handle alone. Never resolves if no child ever connected, so
        callers must also race the wall-clock timeout.
        """
        await self._done.wait()

    def abort(self) -> None:
        """Cancel in-flight connection handlers (and their verb dispatches).

        Called by the executor once the child is dead (kill, timeout, or turn
        abort): a verb still executing at that point has no consumer, and on
        an abort the cascade MUST reach it (a sub-agent turn or LLM call must
        not keep running after its workflow was killed). Also what keeps
        ``Server.wait_closed()`` from stalling until the verb timeout.
        """
        for task in list(self._tasks):
            task.cancel()

    async def serve(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        """Connection handler for ``asyncio.start_unix_server``/``start_server``."""
        task = asyncio.current_task()
        if task is not None:
            self._tasks.add(task)
        try:
            if not await self._handshake(reader, writer):
                return
            await self._loop(reader, writer)
        except (asyncio.IncompleteReadError, ConnectionResetError):
            pass  # child died; the executor reports it from the exit status
        except Exception:  # noqa: BLE001 - the pump must never crash the parent
            logger.warning("workflow pump connection failed", exc_info=True)
        finally:
            if task is not None:
                self._tasks.discard(task)
            if task is not None and task is self._accepted_task:
                self._done.set()
            try:
                writer.close()
            except Exception:  # noqa: BLE001
                pass

    async def _handshake(self, reader, writer) -> bool:
        frame = await self._read_frame(reader)
        token = str((frame or {}).get("token") or "")
        if (
            frame is None
            or frame.get("t") != "hello"
            or not hmac.compare_digest(token, self._token)
        ):
            self.auth_failures += 1
            logger.warning(
                "workflow pump rejected a connection (run %s): bad hello",
                self._ctx.run_id,
            )
            return False
        if self._accepted:
            self.auth_failures += 1
            logger.warning(
                "workflow pump rejected a second connection (run %s)",
                self._ctx.run_id,
            )
            return False
        self._accepted = True
        self._accepted_task = asyncio.current_task()
        await self._send(writer, {"t": "welcome", "verbs": verb_metadata()})
        return True

    async def _loop(self, reader, writer) -> None:
        while True:
            frame = await self._read_frame(reader)
            if frame is None:
                return  # EOF: child exited
            kind = frame.get("t")
            if kind == "call":
                response = await self._dispatch(frame)
                await self._send(writer, response)
            elif kind == "finish":
                self.finish = frame
                return
            elif kind == "oversize":
                # An oversized inbound frame cannot be parsed for its request
                # id, so it cannot be answered in lockstep. Drop the connection:
                # the child's next socket op fails fast, it reports runner_error
                # and exits, so proc.wait() returns promptly (no wall-clock pin).
                logger.warning(
                    "workflow run %s dropped after an oversized frame", self._ctx.run_id
                )
                return
            else:
                await self._send(
                    writer,
                    {
                        "t": "result",
                        "id": frame.get("id"),
                        "ok": False,
                        "error": {
                            "kind": KIND_RUNNER_ERROR,
                            "message": f"unknown frame type {kind!r}",
                        },
                    },
                )

    async def _dispatch(self, frame: dict) -> dict:
        request_id = frame.get("id")
        verb = str(frame.get("verb") or "")
        args = frame.get("args")
        args = args if isinstance(args, dict) else {}
        started = time.monotonic()

        def _error(kind: str, message: str) -> dict:
            self._trace.record(
                verb=verb,
                status="error",
                duration_ms=int((time.monotonic() - started) * 1000),
                args=args,
                error_kind=kind,
            )
            return {
                "t": "result",
                "id": request_id,
                "ok": False,
                "error": {"kind": kind, "message": message, "verb": verb},
            }

        spec = resolve_verb(verb)
        if spec is None:
            return _error(KIND_VERB_ERROR, f"unknown workflow verb {verb!r}")

        try:
            self._ctx.usage.charge_call(self._ctx.budget, ai=spec.ai)
        except WorkflowBudgetExceeded as exc:
            return _error(KIND_BUDGET_EXCEEDED, exc.message)

        timeout = self._ctx.budget.resolve_verb_timeout()
        try:
            result = await asyncio.wait_for(
                spec.handler(self._ctx, verb, args), timeout=timeout
            )
        except asyncio.TimeoutError:
            return _error(
                KIND_VERB_ERROR, f"verb {verb!r} timed out after {timeout:.0f}s"
            )
        except VerbError as exc:
            return _error(exc.kind, exc.message)
        except Exception as exc:  # noqa: BLE001 - handler bug; report, don't crash
            logger.warning(
                "workflow verb %r handler failed (run %s)",
                verb,
                self._ctx.run_id,
                exc_info=True,
            )
            return _error(KIND_VERB_ERROR, f"verb {verb!r} failed: {exc}")

        value = self._normalize(result)
        self._trace.record(
            verb=verb,
            status="ok",
            duration_ms=int((time.monotonic() - started) * 1000),
            args=args,
            result=value,
        )
        return {"t": "result", "id": request_id, "ok": True, "value": value}

    def _normalize(self, result: Any) -> Any:
        """JSON-safe, size-capped verb result for the wire.

        The cap applies to BOTH raw strings and the serialized form of
        structured results (a tool returning a large dict/list is the common
        case), so a verb cannot flood the author's context past the budget.
        An over-cap structured result is replaced by a truncation marker
        rather than silently half-serialized.
        """
        cap = self._ctx.budget.result_cap_chars
        if not isinstance(result, (str, int, float, bool, dict, list, type(None))):
            result = str(result)
        if isinstance(result, str):
            return self._cap_str(result, cap)
        try:
            encoded = json.dumps(result)
        except (TypeError, ValueError):
            encoded = json.dumps(result, default=str)
            result = json.loads(encoded)
        if len(encoded) > cap:
            return {
                "_truncated": True,
                "message": f"verb result exceeded the workflow cap ({cap} chars)",
                "preview": self._cap_str(encoded, cap),
            }
        return result

    @staticmethod
    def _cap_str(text: str, cap: int) -> str:
        if len(text) <= cap:
            return text
        return text[: max(0, cap - 32)] + "\n[...truncated by workflow cap...]"

    async def _read_frame(self, reader: asyncio.StreamReader) -> Optional[dict]:
        try:
            line = await reader.readline()
        except ValueError:
            # Frame exceeded the stream limit (FRAME_LIMIT_BYTES). Drop it as a
            # defined protocol error rather than tearing the connection down as
            # a generic failure; the sentinel drives a clean result frame.
            logger.warning(
                "workflow pump got an oversized frame (run %s)", self._ctx.run_id
            )
            return {"t": "oversize"}
        if not line:
            return None
        try:
            frame = json.loads(line)
        except json.JSONDecodeError:
            logger.warning("workflow pump got a non-JSON frame (run %s)", self._ctx.run_id)
            return {}
        return frame if isinstance(frame, dict) else {}

    async def _send(self, writer: asyncio.StreamWriter, frame: dict) -> None:
        writer.write(json.dumps(frame, default=str).encode("utf-8") + b"\n")
        await writer.drain()
