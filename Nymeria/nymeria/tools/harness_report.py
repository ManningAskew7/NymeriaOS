"""Harness-issue reporting: file agent-observed platform problems for triage.

Deployment-gated drop-box writer. The tool is inert unless
``HARNESS_REPORT_DIR`` names an EXISTING directory; on the reference dev
stack that is a bind mount of the repo's
``Nymeria/docs/private/plans/backlog/intake/`` into the api container. The
tool only ever CREATES new files there (exclusive create, sanitized slug
filename): it never edits or overwrites existing files, never touches git,
and never allocates backlog numbers. Reports are untracked drop-box files
that a human or a coding-agent triage pass later promotes into numbered
backlog items (see the intake directory's README).

A missing directory is a refusal rather than a ``mkdir`` on purpose: in
Docker the configured path is a bind mount, so auto-creating it would land
reports on the container's writable layer, where they silently vanish on
the next recreate. Refusing surfaces the broken mount instead.

Report content is agent-authored and therefore untrusted; the filename is
derived only from a ``[a-z0-9-]`` slug plus a random suffix, so no
model-supplied text can influence where the file lands.

When ``HARNESS_REPORT_EMAIL`` is also set, a successful filing additionally
emails the report (file attached) to that operator-configured address in a
background daemon thread, over the OWNER account's connected Outlook
credential, exactly as ``POST /report`` does. The model controls neither
the destination nor the credential, and an email failure never disturbs
the already-written file.
"""

from __future__ import annotations

import base64
import logging
import re
import socket
import threading
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Annotated, Literal, Optional

from langchain_core.runnables import RunnableConfig
from langchain_core.tools import InjectedToolArg, tool

from ..config import get_settings
from .outlook_email import graph_request
from .registry import ToolGroup, register_tool_group
from .utils import get_thread_id, get_user_id

logger = logging.getLogger(__name__)

# Generous caps: reports should carry verbatim error text, but a runaway
# details payload must not turn the intake dir into a log sink.
TITLE_MAX_CHARS = 200
DETAILS_MAX_CHARS = 30_000
AREA_MAX_CHARS = 200
_SLUG_MAX_CHARS = 40
_CREATE_ATTEMPTS = 5


def _slugify(title: str) -> str:
    slug = re.sub(r"[^a-z0-9]+", "-", title.lower()).strip("-")
    return slug[:_SLUG_MAX_CHARS].rstrip("-") or "report"


def _instance_label() -> str:
    label = get_settings().harness_report_instance_label
    return label if label else socket.gethostname()


def _write_exclusive(target: Path, body: str) -> None:
    """Exclusive create: an existing file (or a planted symlink under the
    same name) raises instead of being followed or overwritten. LF is
    pinned so the on-disk bytes match the emailed attachment on every
    platform."""
    with open(target, "x", encoding="utf-8", newline="\n") as fh:
        fh.write(body)


def _send_report_email(recipient: str, subject: str, body: str, filename: str) -> None:
    """Email a filed report to the developer with the file attached.

    Runs in a background daemon thread after the file write succeeded. Uses
    the OWNER account's connected Outlook credential (``user_id="default"``),
    mirroring ``POST /report``: the filing thread's own user may hold no
    Outlook credential, and report mail always belongs in the one operator
    mailbox. The destination is operator config (``HARNESS_REPORT_EMAIL``),
    never model input. Failures are logged and swallowed: the report file is
    already on disk, and the email is a convenience copy.
    """
    try:
        message = {
            "subject": subject,
            "body": {"contentType": "Text", "content": body},
            "toRecipients": [{"emailAddress": {"address": recipient}}],
            "attachments": [
                {
                    "@odata.type": "#microsoft.graph.fileAttachment",
                    "name": filename,
                    "contentType": "text/markdown",
                    "contentBytes": base64.b64encode(body.encode("utf-8")).decode(
                        "ascii"
                    ),
                }
            ],
        }
        success, result = graph_request(
            "default", "POST", "/me/sendMail", json_data={"message": message}
        )
        if success:
            logger.info("harness_report: emailed %s to %s", filename, recipient)
        else:
            logger.warning(
                "harness_report: email of %s to %s failed: %s",
                filename,
                recipient,
                result,
            )
    except Exception:  # noqa: BLE001 - background convenience copy, never raises
        logger.warning(
            "harness_report: email of %s to %s failed", filename, recipient,
            exc_info=True,
        )


@tool
def harness_report(
    title: str,
    details: str,
    kind: Literal["bug", "friction", "papercut", "idea"] = "friction",
    area: Optional[str] = None,
    *,
    config: Annotated[RunnableConfig, InjectedToolArg] = None,
) -> str:
    """Report a Nymeria platform issue or pain point to the developer's backlog intake.

    Use this whenever the platform itself gets in your way, and keep the
    threshold LOW: real bugs, but just as much friction, papercuts, confusing
    or misleading tool output, unclear errors, missing capabilities, docs
    that disagree with behavior, and awkward workflows. A minor annoyance you
    quietly worked around is exactly what the developer wants to hear about,
    so when in doubt, report it.

    File one report per distinct issue (not one per occurrence), skip issues
    already reported in this conversation, and never derail the user's task:
    report alongside or after the work at hand. Reports go to a triage queue;
    filing one changes nothing in this session. Where the deployment
    configures it, each report is also emailed to the developer
    automatically in the background: that is by design, so never withhold or
    soften a report out of concern about spamming them.

    Args:
        title: One-line summary of the issue.
        details: What happened vs what you expected, with enough context to
            locate the problem later: tool names, exact commands or
            arguments, verbatim error text or output snippets.
        kind: "bug" (broken behavior), "friction" (worked, but fought you),
            "papercut" (minor annoyance), or "idea" (improvement suggestion).
        area: Optional subsystem guess, e.g. "tools/web_fetch" or
            "compaction".

    Returns:
        Confirmation naming the queued report file, or the reason reporting
        is unavailable on this deployment.
    """
    intake = get_settings().harness_report_dir
    if not intake:
        return (
            "[Not configured] Harness reporting is not enabled on this "
            "deployment (HARNESS_REPORT_DIR is unset). Report not written; "
            "mention the issue to the user instead if it matters."
        )
    intake_dir = Path(intake).expanduser()
    if not intake_dir.is_dir():
        return (
            f"[Unavailable] The harness-report intake directory {intake_dir} "
            "does not exist or cannot be reached (deployment mount missing "
            "or moved, or a parent directory denies access). Report not "
            "written; tell the user so the deployment can be fixed."
        )

    title_clean = " ".join(title.split()).strip()
    truncation_notes = []
    if len(title_clean) > TITLE_MAX_CHARS:
        title_clean = title_clean[:TITLE_MAX_CHARS]
        truncation_notes.append(f"title truncated to {TITLE_MAX_CHARS} chars")
    details_clean = details.strip()
    if len(details_clean) > DETAILS_MAX_CHARS:
        details_clean = (
            details_clean[:DETAILS_MAX_CHARS]
            + f"\n\n[truncated by harness_report: details exceeded "
            f"{DETAILS_MAX_CHARS} chars]"
        )
        truncation_notes.append(f"details truncated to {DETAILS_MAX_CHARS} chars")
    area_clean = " ".join(area.split()).strip() if area else None
    if area_clean and len(area_clean) > AREA_MAX_CHARS:
        area_clean = area_clean[:AREA_MAX_CHARS]
        truncation_notes.append(f"area truncated to {AREA_MAX_CHARS} chars")

    now = datetime.now(timezone.utc)
    stamp = now.strftime("%Y%m%d-%H%M%SZ")
    slug = _slugify(title_clean)
    body = (
        f"# {title_clean}\n\n"
        f"- Filed: {now.strftime('%Y-%m-%dT%H:%M:%SZ')}\n"
        f"- Instance: {_instance_label()}\n"
        f"- Thread: {get_thread_id(config)}\n"
        f"- User: {get_user_id(config)}\n"
        f"- Kind: {kind}\n"
        f"- Area: {area_clean or 'unspecified'}\n\n"
        f"{details_clean}\n"
    )

    last_error: Optional[OSError] = None
    for _ in range(_CREATE_ATTEMPTS):
        target = intake_dir / f"{stamp}-{slug}-{uuid.uuid4().hex[:4]}.md"
        try:
            _write_exclusive(target, body)
        except FileExistsError:
            continue
        except OSError as exc:
            last_error = exc
            # A mid-write failure (ENOSPC) can leave a partial file behind
            # the already-succeeded exclusive create; triage must never see
            # a report the tool disclaimed.
            try:
                target.unlink(missing_ok=True)
            except OSError:
                # Best-effort cleanup on an already-failing path; the
                # [Error] result below is the honest signal either way.
                pass
            break
        note = f" ({'; '.join(truncation_notes)})" if truncation_notes else ""
        logger.info(
            "harness_report: queued %s (kind=%s thread=%s)",
            target.name,
            kind,
            get_thread_id(config),
        )
        email_note = ""
        recipient = get_settings().harness_report_email
        if recipient:
            try:
                threading.Thread(
                    target=_send_report_email,
                    args=(
                        recipient,
                        f"[Nymeria harness_report] {kind}: {title_clean}",
                        body,
                        target.name,
                    ),
                    daemon=True,
                    name="harness-report-email",
                ).start()
                email_note = (
                    " A copy is also being emailed to the developer in the "
                    "background (automatic and by design; filing reports is "
                    "never spamming them)."
                )
            except Exception:  # noqa: BLE001 - the filed report must stand
                logger.warning(
                    "harness_report: could not start the email dispatch "
                    "thread for %s",
                    target.name,
                    exc_info=True,
                )
        return (
            f"Report queued for triage: {target.name} (kind: {kind}){note}."
            f"{email_note} It will be reviewed and promoted to a numbered "
            "backlog item later; nothing changes in this session."
        )

    logger.warning(
        "harness_report: failed to write report to %s: %s", intake_dir, last_error
    )
    reason = last_error if last_error else "could not allocate a unique filename"
    # The "[Error]" prefix is the sentinel SafeToolNode and the ordered-batch
    # machinery read as tool failure; the two refusal branches above are
    # deliberate non-errors (the tool answered honestly).
    return (
        f"[Error] Could not write the report to {intake_dir}: {reason}. "
        "Report not recorded; tell the user."
    )


HARNESS_REPORT_TOOLS = [harness_report]

register_tool_group(
    ToolGroup(name="harness_report", tools=tuple(HARNESS_REPORT_TOOLS))
)
