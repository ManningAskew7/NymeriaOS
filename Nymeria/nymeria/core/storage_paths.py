"""Canonical filesystem path-segment sanitization for per-identifier storage.

Several core storage layers derive a JSON or SQLite file path from a caller
supplied identifier (a user id or thread id): ``TodoManager``,
``TriggerManager``, ``GoalManager``, ``UserProfileManager``,
``ThreadMetadataManager``, ``ActivityLog``, the notification store, the ticker's
TODO-completion indexer, and the per-user memory-index path in
``agent_prompt``. Each historically inlined the same path-traversal guard: keep
only ``[A-Za-z0-9-_]`` characters and fall back to a default when nothing
survives. The copies had already begun to drift (some inline ``or "default"``,
others a separate ``if not safe:`` block).

This module is the single home for that rule, so a change to the allowed
character set or the fallback happens in one place instead of ten. It imports
nothing from the rest of the package (stdlib only), so any storage module can
depend on it without a cycle.

For that same no-cycle reason it has become the shared home for the rest of
the file-backed stores' common plumbing, in four groups:

- Path derivation: ``safe_path_segment``, ``mtime_sort_key``.
- Durable writes: ``write_text_atomic`` (also used by the ``file_write``
  tool, so it carries an existing file's permission bits across the rename)
  and ``quarantine_corrupt_file``, the corrupt-store quarantine
  (resource-filesystem-layout plan, slice 3).
- Freshness: ``compare_fingerprint`` and friends, the ONE primitive behind
  every hot-load cache in the package. Read its docstring before touching a
  store's staleness check; the coarse-mtime trap it exists for is not
  obvious.
- The cross-process ``.sig`` sidecar behind the external-edit audit
  (slice 4): ``record_store_fingerprint``/``read_store_fingerprint``.
"""

from __future__ import annotations

import hashlib
import os
import stat
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Dict, Iterable, List, Optional, Tuple

# Characters permitted in a path segment in addition to alphanumerics. Path
# separators and "." are intentionally excluded, which is what blocks traversal.
_ALLOWED_EXTRA = "-_"


def safe_path_segment(value: str, *, default: str = "default") -> str:
    """Reduce ``value`` to a filesystem-safe path segment.

    Keeps only alphanumeric characters plus ``-`` and ``_``, dropping everything
    else (including ``/``, ``\\`` and ``.``). Using the result as a filename or
    directory component therefore cannot escape its parent directory. When no
    allowed character survives (an empty or all-punctuation input), ``default``
    is returned instead.

    Args:
        value: The raw identifier (user id, thread id, ...) to sanitize.
        default: Value returned when sanitization leaves an empty string. Pass
            ``""`` to keep the legacy "return possibly-empty" contract that a
            few non-core call sites still rely on.

    Returns:
        The sanitized segment, or ``default`` when it would otherwise be empty.
    """
    return "".join(c for c in value if c.isalnum() or c in _ALLOWED_EXTRA) or default


def mtime_sort_key(path: Path) -> Tuple[int, str]:
    """Sort key ordering store files by mtime (newest first under ``reverse=True``).

    The file-backed stores in this package list a directory with ``glob()`` and
    then ``sorted(..., key=...)``. ``sorted`` evaluates the key eagerly for every
    globbed path, so a file removed between the ``glob`` and the key evaluation (a
    normal store race: an approval resolving, a sweep, a prune, a claim rename)
    would raise ``FileNotFoundError`` (an ``OSError``) straight out of ``sorted``.

    This key guards the ``stat`` so a vanished path ranks oldest (``mtime_ns`` 0)
    and is simply skipped by the read/prune loop that follows, instead of crashing
    the whole listing. Never raises.

    ORDERING CAVEAT: file mtimes come from a coarse clock (~1ms on Linux/ext4,
    coarser elsewhere), so files written in one tick share an mtime and fall
    back to the filename tiebreak. That keeps the sort deterministic and stable,
    but for names that do not encode time (a ``{run_id}.json``, a uuid) the
    order WITHIN such a burst is arbitrary rather than chronological. Callers
    that need true chronological order must sort by a timestamp inside the
    record; see ``workflows/trace.py``, which uses this key only to bound which
    files it opens and then orders the parsed records itself.
    """
    try:
        return (path.stat().st_mtime_ns, path.name)
    except OSError:
        return (0, path.name)


def quarantine_corrupt_file(path: Path) -> Optional[Path]:
    """Move an unparseable store file into a ``quarantine/`` sibling directory.

    The uniform fail-safe for raw edits to the file-backed resource stores:
    without it a corrupt file is either silently skipped or, worse for the
    whole-store-per-file layouts (hooks, triggers), overwritten with an empty
    store by the next save. The bytes survive under
    ``<store dir>/quarantine/<stem>.corrupt-<utc stamp>-<nonce><suffix>``; the
    subdirectory keeps the quarantined file out of the loaders' ``*.json`` /
    directory globs so it is not re-parsed (and re-quarantined) every sweep.

    Returns the quarantine path, or ``None`` when the move failed, in which
    case the caller should leave the file in place and degrade to its old
    skip-or-replace behavior. Never raises.
    """
    try:
        stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
        target_dir = path.parent / "quarantine"
        target_dir.mkdir(parents=True, exist_ok=True)
        target = target_dir / (
            f"{path.stem}.corrupt-{stamp}-{uuid.uuid4().hex[:6]}{path.suffix}"
        )
        path.rename(target)
        return target
    except OSError:
        return None


def write_text_atomic(path: Path, content: str, *, encoding: str = "utf-8") -> Path:
    """Write ``content`` to ``path`` atomically (sibling temp file + rename).

    A bare ``path.write_text`` can leave a torn or truncated file if the process
    dies mid-write, which the file-backed store loaders then quarantine as
    corrupt (losing the prior good bytes). Writing to a temp file in the SAME
    directory and renaming it into place makes the on-disk file flip atomically
    from the old contents to the new: a crash leaves either the old file or the
    new one, never a partial. Same-directory keeps the rename on one filesystem
    so it stays atomic. The temp name carries a random suffix so concurrent
    writers to the same target do not collide, and is unlinked if the write or
    rename raises (a hard kill between the two can leave an orphan ``.tmp``,
    which is harmless litter: the loaders glob only ``*.json``). Returns ``path``.

    A rename REPLACES the directory entry rather than editing the file in
    place, which matters now that a general-purpose writer (the ``file_write``
    tool) shares this and not just the JSON stores. An existing file's
    permission bits are therefore carried onto the replacement, so a mode the
    user set (an executable script, a tightened 0600) survives the write; a
    fresh file keeps the process umask, as a plain write would.

    Still lost to the rename, and not worth restoring for the stores this
    serves: hard links to the old inode (they keep the OLD contents), file
    OWNERSHIP (the replacement belongs to the writing process, which matters
    when a container runs as root over a bind-mounted host directory), and
    xattrs/ACLs/SELinux labels. Watchers see a replace, not a modify.

    Symlinks are deliberately NOT followed. Resolving them here would let a
    link planted in an agent-writable store directory redirect a manager's
    write outside the data dir, and the caller that actually needs symlink
    semantics (``file_write``) already resolves its path before arriving.
    """
    tmp = path.with_name(f"{path.name}.{uuid.uuid4().hex[:8]}.tmp")
    try:
        existing_mode = stat.S_IMODE(path.stat().st_mode)
    except OSError:
        existing_mode = None  # new file: umask default, as a plain write gives
    try:
        if existing_mode is None:
            tmp.write_text(content, encoding=encoding)
        else:
            # The mode is applied at CREATE, not after the write: a temp born
            # at the umask default and chmod'ed afterwards is briefly
            # group/world readable WITH the new bytes already in it, which
            # would leak the contents of a 0600 file that a plain in-place
            # write never widened. O_CREAT masks the mode by the umask, so
            # this window is never more permissive than the target, and the
            # chmod after restores any bits the umask took off.
            fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL, existing_mode)
            with os.fdopen(fd, "w", encoding=encoding) as handle:
                handle.write(content)
            try:
                os.chmod(tmp, existing_mode)
            except OSError:
                pass  # best-effort mode carry-over; never fail the write
        tmp.replace(path)
    except BaseException:
        try:
            tmp.unlink(missing_ok=True)
        except OSError:
            pass  # best-effort temp cleanup; re-raise the original error
        raise
    return path


FileFingerprint = Tuple[int, int, str]

# Recorded as a fingerprint's hash when a file that NEEDS one cannot be read
# (``stat`` needs only directory search on the parent; ``open`` needs read on
# the file, so the two can disagree). Not valid sha256 hex, so it never equals
# a real digest: the next comparison re-reads and reports a change, which is
# the right answer because whatever the caller parsed came from the same
# unreadable file. The two wrong answers it exists to avoid are "" (which
# asserts mtime is trustworthy, reintroducing the permanent same-tick miss)
# and a dropped entry (which makes a directory scan report the file REMOVED
# and evict a live definition over a transient read error).
UNREADABLE_FINGERPRINT_HASH = "?"
"""``(st_mtime_ns, st_size, content_hash)``; the hash is ``""`` when mtime alone
is trustworthy for this file.

Never compare two fingerprints with ``!=``: one side may legitimately carry a
hash while the other does not, and a raw comparison reads that as an edit.
Go through :func:`compare_fingerprint` (or :func:`scan_fingerprint_map` for a
directory), which is the only thing that knows when a hash is required."""

# How far in the past a file's mtime must sit before mtime equality is proof
# that the bytes did not change.
#
# Despite the ``_ns`` suffix, the kernel stamps mtimes from a coarse clock.
# Measured on this project's Linux/ext4 hosts: the smallest nonzero delta
# between two back-to-back writes is 1ms, and ~58% of same-size rewrite pairs
# land on an IDENTICAL st_mtime_ns. So two writes inside one tick that keep the
# file's size leave (mtime, size) unchanged, and a fingerprint captured between
# them compares equal forever: the second write is missed permanently, not
# merely late.
#
# 3s is deliberately far larger than the 1ms measured here. Granularity is a
# property of the host, not of this contract: Windows' system clock ticks at
# ~15.6ms (and we ship a Windows installer), FAT/exFAT is 2s, ext3/HFS+ 1s, and
# container bind-mount backends have historically been lossier still. The
# margin also absorbs clock skew between the filesystem and this process.
#
# This is a CORRECTNESS bound, not just a cost knob: it must exceed both the
# filesystem's timestamp granularity and any skew where the filesystem clock
# runs BEHIND this process (network and virtualised mounts). Past that, freshly
# written files look quiet, no hash is ever stored, and the mechanism degrades
# silently to the plain (mtime, size) behavior it replaced. That degradation is
# graceful (never worse than before the hash existed), which is why a generous
# constant beats a measured one: being wrong is cheap in one direction and
# invisible in the other. Cost of generosity is only how long a just-written
# file keeps paying for a hash, and "written in the last 3s" is normally zero
# files, so probing the real granularity at startup would be over-engineering.
RACY_FINGERPRINT_MARGIN_NS = 3_000_000_000


def _content_hash(path: Path) -> Optional[str]:
    """SHA-256 of ``path``'s bytes, or ``None`` when it cannot be read."""
    try:
        digest = hashlib.sha256()
        with path.open("rb") as handle:
            for block in iter(lambda: handle.read(65536), b""):
                digest.update(block)
        return digest.hexdigest()
    except OSError:
        return None


def compare_fingerprint(
    path: Path,
    previous: Optional[FileFingerprint],
    *,
    now_ns: Optional[int] = None,
) -> Tuple[Optional[FileFingerprint], bool]:
    """Return ``(fingerprint_to_store, changed)`` for ``path``.

    The shared freshness primitive behind every hot-load cache in this package.
    It keeps the cheap one-stat-per-file path for ordinary files and pays for a
    content hash only where mtime cannot be trusted, so steady-state cost is
    identical to the plain ``(mtime, size)`` tuple it replaces.

    A file is *quiet* when its mtime is older than
    :data:`RACY_FINGERPRINT_MARGIN_NS`: any later write must then land on a
    strictly later tick, so unchanged ``(mtime, size)`` genuinely proves
    unchanged bytes. A file written more recently than that is *racy* and its
    fingerprint carries a hash, which is what closes the same-tick same-size
    hole described on the margin constant.

    Recording and comparing are deliberately ONE operation returning two
    values, because the fingerprint to store is not always the one just
    compared. A racy file that later goes quiet must *downgrade* to a hashless
    fingerprint so it stops being re-hashed forever; if that downgrade were
    expressed as tuple inequality instead, every settled file would report a
    change it never had. That matters more than it sounds: a false positive
    re-embeds the entire installed skill pool, and writes an ``EXTERNAL_EDIT``
    audit line claiming a store was edited on disk when it was not.
    Manufacturing audit records is a worse failure than the missed edit this
    helper exists to fix, so the downgrade rides the returned fingerprint and
    never the ``changed`` flag.

    Returns ``(None, previous is not None)`` when ``path`` is gone, so callers
    see a deletion as a change exactly once.

    TWO CALLER CONTRACTS, and getting them backwards is the easy mistake:

    - In-memory caches STORE the returned fingerprint (via
      :func:`settle_fingerprint` or :func:`scan_fingerprint_map`). They
      re-capture continuously, so the downgrade is what stops a settled file
      being re-hashed on every sweep. This is the contract the "same cost as
      a bare stat in steady state" claim above describes.
    - Cross-process ``.sig`` sidecar comparisons DISCARD it (``_, edited =
      compare_fingerprint(path, read_store_fingerprint(path))``). Their
      ``previous`` was captured at the manager's write, so an external edit in
      that same tick stays racy against that record forever; writing a
      downgraded fingerprint back would disarm the audit. Passing the sidecar
      as ``previous`` is also what FORCES this side to hash when the sidecar
      carries one, which is exactly the same-tick case the audit must catch.
      The cost note above therefore does NOT apply here: a sidecar recorded at
      a manager write is racy by construction and never settles, so an audit
      comparison hashes the store file on every call, forever. That is bought
      deliberately (it is what makes the audit exact) and is affordable only
      because these store files are small; do not copy the pattern onto a
      large or hot file without measuring.
    """
    stamp = time.time_ns() if now_ns is None else now_ns
    try:
        st = path.stat()
    except OSError:
        return None, previous is not None

    base = (st.st_mtime_ns, st.st_size)
    quiet = st.st_mtime_ns < stamp - RACY_FINGERPRINT_MARGIN_NS

    if previous is not None and previous[:2] == base:
        if not previous[2]:
            # Recorded while already quiet, so mtime equality is proof.
            return (base[0], base[1], ""), False
        current = _content_hash(path)
        if current is None:
            # Unreadable right now (mid-rename, permissions): keep the old
            # fingerprint and report no change, so a transient read error
            # cannot fake an edit.
            return previous, False
        return (base[0], base[1], "" if quiet else current), current != previous[2]

    # New file, or (mtime, size) moved: changed either way, so the hash is
    # needed only to keep FUTURE comparisons exact. A racy file we could not
    # READ (stat needs only directory search) records the unreadable sentinel,
    # never "": "" asserts that mtime is trustworthy, which would reintroduce
    # the permanent same-tick miss this whole primitive exists to close.
    if quiet:
        digest = ""
    else:
        hashed = _content_hash(path)
        digest = UNREADABLE_FINGERPRINT_HASH if hashed is None else hashed
    return (base[0], base[1], digest), True


def settle_fingerprint(
    stored: Optional[FileFingerprint], fresh: Optional[FileFingerprint]
) -> Optional[FileFingerprint]:
    """The fingerprint to write back over ``stored``, or ``None`` to leave it.

    The companion to :func:`compare_fingerprint` for in-memory caches: after a
    scan reports no change, the fresh fingerprint may have dropped its content
    hash because the file went quiet, and storing it is what stops that file
    being re-read on every subsequent sweep.

    Returns ``None`` unless the write is a genuine DOWNGRADE (``fresh`` drops
    the hash) of the same ``(mtime, size)`` ``stored`` describes. Both halves
    matter, and the second is not redundant: a caller that snapshotted outside
    its lock may be holding a fingerprint older than a concurrent scanner's,
    and equal ``(mtime, size)`` does NOT prove equal content here (that is the
    coarse-clock collision this module exists for), so writing back a stale
    HASH could hide an edit a peer already saw. A hashless downgrade is safe
    to write in that race because it only asserts what the mtime already
    proves. Keeping the rule here also keeps the tuple layout inside the
    module that owns the type, instead of every adopter hand-writing it.
    """
    if stored is None or fresh is None or fresh[2]:
        return None
    return fresh if stored[:2] == fresh[:2] else None


def scan_fingerprint_map(
    entries: "Iterable[Tuple[str, Path]]",
    previous: Optional[Dict[str, FileFingerprint]],
    *,
    now_ns: Optional[int] = None,
) -> Tuple[Dict[str, FileFingerprint], List[str], List[str]]:
    """Fingerprint a directory of store files against ``previous``.

    Returns ``(fresh_map, changed_keys, removed_keys)``. ``entries`` is
    ``(key, path)`` pairs so each caller keeps its own keying (filename for the
    flat JSON stores, full path string for the skills tree) and its own
    enumeration (one glob, or a walk over several scope roots).

    The shared shape behind every directory-scanning hot-load sweep, so the
    "compare per file, never by comparing the two maps" rule is enforced by
    this API rather than repeated as a comment at each call site. Comparing
    maps would read a file that merely settled (hash dropped, bytes unchanged)
    as an edit.
    """
    prior = previous or {}
    fresh: Dict[str, FileFingerprint] = {}
    changed: List[str] = []
    for key, path in entries:
        fingerprint, entry_changed = compare_fingerprint(
            path, prior.get(key), now_ns=now_ns
        )
        if fingerprint is None:
            continue  # vanished mid-scan; the removed list below catches it
        fresh[key] = fingerprint
        if entry_changed:
            changed.append(key)
    removed = [key for key in prior if key not in fresh]
    return fresh, changed, removed


def store_fingerprint_path(path: Path) -> Path:
    """The ``<name>.sig`` sidecar that records a store file's fingerprint."""
    return path.with_name(path.name + ".sig")


def record_store_fingerprint(path: Path) -> Optional[FileFingerprint]:
    """Persist ``path``'s :data:`FileFingerprint` to its ``.sig`` sidecar.

    The sidecar is the shared "last acknowledged state" of a store file,
    visible across manager instances and processes (the stores that need it,
    hooks and triggers, are written and read by different instances: hook
    authoring goes through the tools/REST managers while the engine reads via
    its own, and trigger fire-state saves happen in the ticker, a separate
    process in the Docker shape). Managers call this after every save; loaders
    that detect and audit a raw on-disk edit call it again to acknowledge the
    edit, so it is audited once rather than once per reader.

    Callers reach this right after writing ``path``, so the file is racy by
    construction and the recorded fingerprint carries a content hash. That is
    what lets a reader in another process tell a raw edit from the manager's
    own write even when the two land in the same coarse mtime tick.

    The write is atomic (temp file + rename), with a random temp suffix for the
    same reason :func:`write_text_atomic` uses one: this sidecar is explicitly
    a cross-process artifact, so two processes recording at once must not share
    a temp name and publish each other's half-written bytes. When ``path``
    itself is missing (deleted or quarantined), the stale sidecar is removed
    instead. Never raises; returns the recorded fingerprint, or ``None`` when
    nothing was recorded.
    """
    sidecar = store_fingerprint_path(path)
    fingerprint, _ = compare_fingerprint(path, None)
    if fingerprint is None:
        try:
            sidecar.unlink(missing_ok=True)
        except OSError:
            pass  # best-effort stale-sidecar cleanup; helper never raises
        return None
    try:
        temp = sidecar.with_name(f"{sidecar.name}.{uuid.uuid4().hex[:8]}.tmp")
        try:
            temp.write_text(
                f"{fingerprint[0]}:{fingerprint[1]}:{fingerprint[2]}\n",
                encoding="utf-8",
            )
            temp.replace(sidecar)
        except BaseException:
            try:
                temp.unlink(missing_ok=True)
            except OSError:
                pass  # best-effort temp cleanup
            raise
    except OSError:
        return None
    return fingerprint


def read_store_fingerprint(path: Path) -> Optional[FileFingerprint]:
    """Read the fingerprint recorded by :func:`record_store_fingerprint`.

    Tolerates the legacy two-field ``mtime:size`` sidecars written before
    fingerprints carried a content hash: those still exist on deployed hosts,
    and rejecting them would silently disable the external-edit audit until
    each store's next save. They read back with an empty hash, which
    :func:`compare_fingerprint` reads as "recorded while quiet", so an
    unchanged ``(mtime, size)`` is taken as proof of unchanged bytes rather
    than as a mismatch.

    Forward/backward compatibility is one-way: an OLD process reading a NEW
    three-field sidecar fails to parse it and gets ``None``, i.e. "no manager
    write on record", so its audit goes quiet until that store's next save
    rewrites the file. Fail-safe, and only relevant on a mixed-version host or
    a rollback.

    Returns ``None`` when the sidecar is absent or unreadable (including a
    corrupt sidecar), which callers treat as "no manager write on record": the
    external-edit audit fails safe rather than false-positiving. Never raises.
    """
    try:
        raw = store_fingerprint_path(path).read_text(encoding="utf-8").strip()
        parts = raw.split(":")
        if len(parts) == 2:
            return (int(parts[0]), int(parts[1]), "")
        if len(parts) == 3:
            return (int(parts[0]), int(parts[1]), parts[2])
        return None
    except (OSError, ValueError):
        return None
