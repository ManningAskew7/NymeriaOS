"""System endpoint schemas."""

from typing import Literal

from pydantic import BaseModel, Field

from ... import __version__


class HealthResponse(BaseModel):
    """Response model for health check."""

    status: str = "ok"
    version: str = __version__
    # Whether this deployment is already set up (claimed by a human, its
    # bootstrap token used or more than one account minted, and the LLM
    # provider set), so a client pointed at it can open on the token-only
    # sign-in instead of the install hub (#323). Coarse on purpose: /health
    # is unauthenticated and must not say which provider or account.
    configured: bool = False


class DependencyReadiness(BaseModel):
    """Readiness state for one runtime dependency."""

    status: Literal["ok", "skipped", "error"]
    detail: str | None = None


class ReadinessResponse(BaseModel):
    """Response model for readiness checks."""

    status: Literal["ok", "error"]
    version: str = __version__
    checks: dict[str, DependencyReadiness]


class BusyThread(BaseModel):
    """One currently held thread lock (an in-flight turn)."""

    thread_id: str
    holder: str
    held_seconds: float | None = None


class TurnActivityResponse(BaseModel):
    """Live activity; all-zero counts are the restart-safe signal."""

    active_turns: int
    interactive_active: int
    # Bash jobs, runner tasks and embedding tails can outlive thread locks.
    # Runner tasks may also overlap active_turns while executing.
    background_jobs: int = 0
    # Detached Claude Code / /code runs: running, or finished and still
    # delivering their report (#340). They hold no thread lock and are not
    # bash jobs, so they have their own counter (#339); a restart kills the
    # local run, or the watcher and delivery of a remote one.
    claude_code_jobs: int = 0
    # Populated only for admin callers: thread ids and holder labels are
    # cross-user metadata; the counts alone carry the idle predicate.
    busy_threads: list[BusyThread] = Field(default_factory=list)
    # Which code this process booted from, for deploy automation (#423):
    # the boot record's commit (null with no git metadata, as in a Docker
    # container) and the digest of its boot-time file fingerprint
    # (``_provenance.fingerprint_digest``). Admin callers only; for anyone
    # else the route leaves them unset and the response omits both keys.
    code_version: str | None = None
    code_fingerprint: str | None = None


class ReportRequest(BaseModel):
    """Request model for error report endpoint."""

    thread_id: str | None = None
    message_id: str = ""
    description: str = ""
    messages: list[dict] = Field(default_factory=list)
    timestamp: str = ""
    client_info: dict = Field(default_factory=dict)
