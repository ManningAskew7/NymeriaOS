"""A wakeup doorbell: any thread rings it, listeners on one loop wake.

The third appearance of this shape in the codebase (``turn_stream_buffer``
and ``browser_login_sessions`` grew it independently), extracted so the
next consumer does not write a fourth copy. The discipline it encodes:

* The listening side must CAPTURE the current event via :meth:`listen`
  BEFORE checking for data, then await the captured event only if the
  check came up empty. A ring landing between the check and the await
  sets the already-captured event, so a wakeup can never be lost to that
  race.
* :meth:`ring` REPLACES the event rather than clearing it, so any number
  of listeners each wake exactly once per ring with no clear/set race.
* ``asyncio.Event.set`` is not thread-safe, so a ring from off the bound
  loop is marshalled onto it with ``call_soon_threadsafe``. Publishers in
  this codebase are frequently worker threads (the ticker, Redis
  subscriber loops, sync tool paths), which is the whole reason this class
  exists instead of a bare Event.

A pulse with no bound loop still rings (a plain ``set()``): that is only
correct when nothing awaits it from another thread, which holds for the
sync consumers that poll their queue directly and never call
:meth:`listen`.
"""

from __future__ import annotations

import asyncio
import threading
from typing import Optional

__all__ = ["LoopPulse"]


class LoopPulse:
    def __init__(self) -> None:
        self._event = asyncio.Event()
        self._lock = threading.Lock()
        self._loop: Optional[asyncio.AbstractEventLoop] = None

    def bind(self, loop: Optional[asyncio.AbstractEventLoop]) -> None:
        """Name the loop the listeners run on (wakeups are marshalled to it)."""
        self._loop = loop

    def bind_running_loop(self) -> bool:
        """Bind to the caller's running loop; False when there is none."""
        try:
            self._loop = asyncio.get_running_loop()
            return True
        except RuntimeError:
            return False

    def listen(self) -> asyncio.Event:
        """The event to await. Capture it BEFORE checking for data."""
        with self._lock:
            return self._event

    def ring(self) -> None:
        """Wake every captured listener, from any thread."""
        with self._lock:
            event = self._event
            self._event = asyncio.Event()
        loop = self._loop
        if loop is not None and loop.is_running():
            try:
                running: Optional[asyncio.AbstractEventLoop] = asyncio.get_running_loop()
            except RuntimeError:
                running = None
            if running is not loop:
                try:
                    loop.call_soon_threadsafe(event.set)
                    return
                except RuntimeError:
                    # Loop closed between the check and the call; the plain
                    # set below is then as good as it gets.
                    pass
        event.set()
