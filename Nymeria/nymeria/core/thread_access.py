"""The read half of thread access, shared by every surface that checks it.

Thread ownership is enforced in two mirrored places, the REST
``triggers/api.py::_require_thread_access`` and the command layer's
``CommandBackendClient._require_thread_access``; both claim on write and
only compare owners on read. This module holds the pieces of the READ rule
that a third surface (an agent tool acting on a thread id it was handed) also
needs, so the rule is written once:

- an admin reads anything;
- a shared channel (Discord guild channel, Telegram group, Twitch chat, ...)
  is readable by a non-admin only through the bot relay's act-as;
- a thread owned by the caller is readable, one owned by someone else is not;
- an OWNERLESS thread is readable only when its id is canonical.

The last rule is the read half of the #349 identity boundary. Every store
keyed by a thread id folds it through ``safe_path_segment``, which is
many-to-one, and #349 only gated CREATION (a non-canonical NEW id is refused
at claim). A read never claims, so an ownerless ``alice.-thread`` passed the
owner check and read the store of the owned ``alice-thread`` it folds onto.
Legitimate non-canonical ids predate #349 and carry an owner row, and every
id the platform mints is canonical, so refusing the ownerless ones costs
nothing real.
"""

from __future__ import annotations

from typing import Any

from .storage_paths import canonical_segment_error
from .thread_classification import is_shared_channel


def ownerless_read_refused(thread_id: str) -> bool:
    """True when an ownerless ``thread_id`` must not be read: it is not its
    own storage segment, so it folds onto an id someone else may own."""
    return canonical_segment_error(thread_id) is not None


def may_read_thread(
    accounts_repo: Any,
    *,
    user_id: str,
    is_admin: bool,
    thread_id: str,
    via_act_as: bool = False,
) -> bool:
    """The read rule above, for surfaces that check a thread id outside the
    two ``_require_thread_access`` twins (the watchdog tools)."""
    if is_admin:
        return True
    if is_shared_channel(thread_id):
        return via_act_as
    owner = accounts_repo.get_thread_owner(thread_id)
    if owner is None:
        return not ownerless_read_refused(thread_id)
    return owner == user_id


__all__ = ["may_read_thread", "ownerless_read_refused"]
