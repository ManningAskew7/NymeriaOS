"""Workflow child runner. STDLIB-ONLY and launched BY FILE PATH.

The executor invokes this file as ``[sys.executable, /abs/path/runner.py]``
rather than ``-m nymeria...`` so the child never imports the ``nymeria``
package (whose ``core/__init__`` pulls the whole agent stack). Keep this file
free of any non-stdlib import and free of intra-package imports; it must run
under a scrubbed environment with no project dependencies available.

Bootstrap (one JSON object on stdin):

    {"socket": {"family": "unix", "path": ...} | {"family": "tcp", "host": ..., "port": ...},
     "token": str, "source": str, "entrypoint": str, "params": {},
     "run_id": str, "io_timeout_seconds": float}

The author's code executes in a namespace that provides ``nym`` (a generic
verb proxy speaking newline-JSON frames over the socket), an identity
``workflow`` decorator, and a small ``retry`` helper. stdout/stderr are NOT
redirected: the protocol has its own channel, so author prints are plain
logs captured by the parent.

Exit code is 0 whenever the protocol completed (including author errors,
which travel in the finish frame); nonzero only for bootstrap/protocol
failures where no finish frame could be sent.
"""

from __future__ import annotations

import asyncio
import json
import socket
import sys
import time
import traceback

TRACEBACK_FRAME_LIMIT = 8


class NymVerbError(Exception):
    """A ``nym.*`` call failed. Authors may catch this and continue."""

    def __init__(self, message: str, *, kind: str = "verb_error", verb: str = "", step: int = 0) -> None:
        super().__init__(message)
        self.kind = kind
        self.message = message
        self.verb = verb
        self.step = step


class _Channel:
    """Blocking newline-JSON frame channel over a connected socket."""

    def __init__(self, sock: socket.socket) -> None:
        self._sock = sock
        self._file = sock.makefile("rwb")

    def send(self, frame: dict) -> None:
        self._file.write(json.dumps(frame, default=str).encode("utf-8") + b"\n")
        self._file.flush()

    def recv(self) -> dict:
        line = self._file.readline()
        if not line:
            raise NymVerbError(
                "the workflow engine closed the connection", kind="runner_error"
            )
        frame = json.loads(line)
        if not isinstance(frame, dict):
            raise NymVerbError("malformed frame from engine", kind="runner_error")
        return frame


class _Nym:
    """Generic attribute proxy: ``nym.todo.add(x=1)`` -> verb ``todo.add``.

    Verb-agnostic by design (plan: "Verb registry"): the verb list and the
    positional-argument mapping arrive in the welcome frame, so a new verb
    needs no change here. Unknown verbs outside ``tools.*`` fail fast with
    the known-verb list in the message.
    """

    def __init__(
        self, channel: _Channel, verbs: dict, prefix: str = "", counter=None
    ) -> None:
        self._channel = channel
        self._verbs = verbs
        self._prefix = prefix
        # One shared request-id counter for the whole proxy tree.
        self._counter = counter if counter is not None else [0]

    def __getattr__(self, name: str) -> "_Nym":
        if name.startswith("_"):
            raise AttributeError(name)
        dotted = f"{self._prefix}.{name}" if self._prefix else name
        return _Nym(self._channel, self._verbs, dotted, self._counter)

    def __call__(self, *args, **kwargs):
        verb = self._prefix
        if not verb:
            raise NymVerbError("nym is a namespace; call a verb like nym.llm(...)")
        known = verb in self._verbs or verb.startswith("tools.")
        if not known:
            raise NymVerbError(
                f"unknown verb nym.{verb} "
                f"(known: {', '.join(sorted(self._verbs)) or 'none'})",
                verb=verb,
            )
        if args:
            positional = (self._verbs.get(verb) or {}).get("positional") or []
            if len(args) > len(positional):
                raise NymVerbError(
                    f"nym.{verb} takes at most {len(positional)} positional "
                    "argument(s); use keyword arguments",
                    verb=verb,
                )
            for name, value in zip(positional, args):
                if name in kwargs:
                    raise NymVerbError(
                        f"nym.{verb} got {name!r} both positionally and by keyword",
                        verb=verb,
                    )
                kwargs[name] = value
        self._counter[0] += 1
        request_id = self._counter[0]
        wire_args = {name: _wire_safe(value) for name, value in kwargs.items()}
        self._channel.send(
            {"t": "call", "id": request_id, "verb": verb, "args": wire_args}
        )
        while True:
            frame = self._channel.recv()
            if frame.get("t") == "result" and frame.get("id") == request_id:
                break
        if frame.get("ok"):
            return frame.get("value")
        error = frame.get("error") or {}
        raise NymVerbError(
            str(error.get("message") or "verb failed"),
            kind=str(error.get("kind") or "verb_error"),
            verb=verb,
            step=request_id,
        )


def _workflow(fn=None, **_kwargs):
    """Identity decorator so authored ``@workflow`` sources run unmodified."""
    if fn is None:
        return lambda f: f
    return fn


def _retry(fn, attempts: int = 3, delay: float = 1.0):
    """Call ``fn`` up to ``attempts`` times, sleeping ``delay`` between tries."""
    attempts = max(1, int(attempts))
    last = None
    for i in range(attempts):
        try:
            return fn()
        except Exception as exc:  # noqa: BLE001 - author-facing helper
            last = exc
            if i + 1 < attempts:
                time.sleep(max(0.0, float(delay)))
    raise last  # type: ignore[misc]


def _wire_safe(value):
    """Duck-type pydantic-style values into JSON before a frame is sent.

    Stdlib-only by construction: no import, just attribute probes. A model
    CLASS passed as an argument (the ``schema=MyModel`` idiom) becomes its
    JSON schema dict; a model INSTANCE becomes its field dict. Without this,
    ``json.dumps(default=str)`` would stringify either into garbage the
    parent cannot use. Anything else passes through untouched.
    """
    try:
        if isinstance(value, type):
            mjs = getattr(value, "model_json_schema", None)
            if callable(mjs):
                out = mjs()
                if isinstance(out, dict):
                    return out
        else:
            dump = getattr(value, "model_dump", None)
            if callable(dump):
                out = dump()
                if isinstance(out, dict):
                    return out
    except Exception:  # noqa: BLE001
        pass  # best-effort conversion; the plain value falls through below
    return value


def _json_safe(value):
    try:
        json.dumps(value)
        return value
    except (TypeError, ValueError):
        return json.loads(json.dumps(value, default=str))


def _connect(spec: dict, timeout: float) -> socket.socket:
    family = (spec or {}).get("family")
    if family == "unix" and hasattr(socket, "AF_UNIX"):
        sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        sock.settimeout(timeout)
        sock.connect(spec["path"])
        return sock
    if family == "tcp":
        sock = socket.create_connection(
            (spec["host"], int(spec["port"])), timeout=timeout
        )
        return sock
    raise RuntimeError(f"unsupported socket spec: {spec!r}")


def main() -> int:
    try:
        boot = json.loads(sys.stdin.read() or "{}")
        io_timeout = float(boot.get("io_timeout_seconds") or 600.0)
        sock = _connect(boot.get("socket") or {}, io_timeout)
        channel = _Channel(sock)
        channel.send({"t": "hello", "token": str(boot.get("token") or "")})
        welcome = channel.recv()
        if welcome.get("t") != "welcome":
            print("workflow runner: engine refused the handshake", file=sys.stderr)
            return 1
    except Exception as exc:  # noqa: BLE001 - no channel yet; stderr is all we have
        print(f"workflow runner bootstrap failed: {exc}", file=sys.stderr)
        return 1

    run_id = str(boot.get("run_id") or "run")
    nym = _Nym(channel, welcome.get("verbs") or {})
    namespace = {
        "__builtins__": __builtins__,
        "__name__": f"nymeria_workflow_{run_id}",
        "__file__": f"<nymeria_workflow:{run_id}>",
        "nym": nym,
        "workflow": _workflow,
        "retry": _retry,
    }

    try:
        source = str(boot.get("source") or "")
        entrypoint = str(boot.get("entrypoint") or "run")
        params = boot.get("params") or {}
        if not isinstance(params, dict):
            raise TypeError("params must be a JSON object")
        compiled = compile(source, f"<nymeria_workflow:{run_id}>", "exec")
        exec(compiled, namespace)  # noqa: S102 - executing author code IS the feature
        func = namespace.get(entrypoint)
        if not callable(func):
            raise TypeError(f"entrypoint {entrypoint!r} is not callable")
        result = func(**params)
        if asyncio.iscoroutine(result):
            result = asyncio.run(result)
        channel.send(
            {"t": "finish", "status": "ok", "output": _json_safe(result)}
        )
        return 0
    except NymVerbError as exc:
        # An uncaught verb failure keeps its taxonomy (budget_exceeded stays
        # budget_exceeded) instead of collapsing into author_error.
        return _send_finish_error(
            channel,
            {
                "kind": exc.kind,
                "message": exc.message,
                "verb": exc.verb or None,
                "step": exc.step or None,
            },
        )
    except BaseException as exc:  # noqa: BLE001 - isolate all author failures
        return _send_finish_error(
            channel,
            {
                "kind": "author_error",
                "message": f"{type(exc).__name__}: {exc}",
                "traceback": traceback.format_exc(limit=TRACEBACK_FRAME_LIMIT),
            },
        )


def _send_finish_error(channel: _Channel, error: dict) -> int:
    """Report a terminal error; nonzero exit only if the channel is gone."""
    try:
        channel.send({"t": "finish", "status": "error", "error": error})
        return 0
    except Exception as exc:  # noqa: BLE001 - engine gone; stderr is all we have
        print(f"workflow runner could not report its result: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
