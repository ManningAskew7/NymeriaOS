"""Schemas for the browser login-session endpoints.

Three wires meet here, and each one is deliberately narrow:

* ``LoginFrameBatch`` carries screencast JPEG frames UP from the extension.
* ``LoginInputBatch`` carries the operator's keystrokes and clicks DOWN
  toward the extension.
* ``LoginSessionStatus`` is the only session shape that may be shown
  anywhere, and carries no frame bytes.

The input events are SEMANTIC, not CDP calls. The desktop says "the user
pressed A" and the extension decides which ``Input.*`` dispatch that is,
which keeps this channel from becoming a general CDP tunnel between a
frontend and the browser (the same reason ``chrome_cdp`` is denylisted for
the agent) and keeps the CDP knowledge in ``input.ts``, where it already
lives.
"""

from __future__ import annotations

from typing import Any, Literal, Optional

from pydantic import BaseModel, Field

# Frames arrive micro-batched to keep the POST rate down at ~20fps. Measured
# at ~17KB per frame (JPEG q60), so the per-frame cap is generous slack, and
# the batch cap keeps a worst-case body inside the transport valve.
MAX_FRAMES_PER_BATCH = 8
MAX_FRAME_DATA_CHARS = 1_048_576
# One burst of typing, not a macro: a human cannot outrun this.
MAX_INPUT_EVENTS_PER_BATCH = 32


class LoginFrame(BaseModel):
    """One screencast frame as the extension received it from CDP."""

    data: str = Field(
        max_length=MAX_FRAME_DATA_CHARS,
        description="Base64 JPEG, straight from Page.screencastFrame.",
    )
    metadata: Optional[dict[str, Any]] = Field(
        default=None,
        description=(
            "The frame's CDP metadata (deviceWidth/deviceHeight/pageScale"
            "Factor), so the viewer can size and letterbox honestly."
        ),
    )


class LoginFrameBatch(BaseModel):
    frames: list[LoginFrame] = Field(min_length=1, max_length=MAX_FRAMES_PER_BATCH)


class LoginFrameAck(BaseModel):
    accepted: int = Field(description="How many frames were buffered.")
    session_active: bool = Field(
        description=(
            "False once the session has ended. This is how an extension "
            "learns about an ending it never heard: stop the screencast."
        )
    )
    last_seq: int = 0


class LoginInputEvent(BaseModel):
    """One operator action, in terms the viewer can produce without CDP.

    Each variant maps onto exactly one of the extension's existing trusted
    dispatchers, which is why a key event carries no down/up: `dispatchKey`
    sends both as one atomic keystroke and derives the character insertion
    itself (splitting them here would invite stuck modifiers and the
    double-insertion bug that helper's docstring warns about). Chords ride
    the modifier mask instead.

    Pointer coordinates are NORMALIZED to the frame (0.0 to 1.0 on each
    axis) rather than sent in pixels. The viewer renders a scaled,
    letterboxed image and the extension knows the tab's real viewport, so
    normalizing is the one representation neither side can misread: a pixel
    coordinate would silently mean the wrong point at any zoom, device
    pixel ratio, or window size.
    """

    type: Literal["key", "text", "mouse", "wheel"]

    #: key: the key's name ("a", "Enter", "Backspace", "Tab").
    key: Optional[str] = Field(default=None, max_length=32)
    #: CDP modifier bitmask: Alt=1, Ctrl=2, Meta=4, Shift=8.
    modifiers: int = Field(default=0, ge=0, le=15)

    #: text: a paste or bulk entry, inserted as one edit.
    text: Optional[str] = Field(default=None, max_length=4096)

    # mouse / wheel
    action: Optional[Literal["click", "move"]] = None
    x: Optional[float] = Field(default=None, ge=0.0, le=1.0)
    y: Optional[float] = Field(default=None, ge=0.0, le=1.0)
    #: Only the buttons the extension's trusted input path carries.
    button: Optional[Literal["left", "right"]] = None
    click_count: int = Field(default=1, ge=1, le=3)
    delta_x: float = 0.0
    delta_y: float = 0.0


class LoginInputBatch(BaseModel):
    events: list[LoginInputEvent] = Field(
        min_length=1, max_length=MAX_INPUT_EVENTS_PER_BATCH
    )


class LoginInputAck(BaseModel):
    dispatched: int
    session_active: bool


class LoginSessionEndRequest(BaseModel):
    reason: Literal["completed", "cancelled"] = Field(
        default="completed",
        description=(
            "'completed' is the operator clicking Done (they signed in); "
            "'cancelled' is them closing the viewer without finishing."
        ),
    )


class LoginSessionStatus(BaseModel):
    """The session shape safe to render, log, or hand to the agent.

    Frame bytes are structurally absent: this model has no field that could
    hold one.
    """

    session_id: str
    thread_id: str
    tab_id: int
    url: str
    state: str
    end_reason: Optional[str] = None
    last_seq: int = 0
    frames_received: int = 0
    frames_dropped: int = 0
    has_frame: bool = False
    seconds_remaining: float = 0.0
    expires_at: str


class LoginSessionList(BaseModel):
    sessions: list[LoginSessionStatus]
