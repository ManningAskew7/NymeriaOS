"""Filesystem tools for Nymeria."""

import errno
import logging
import os
import uuid
from pathlib import Path
from typing import Annotated, Optional

from langchain_core.runnables import RunnableConfig
from langchain_core.tools import InjectedToolArg, tool

from ..config import get_settings
from ..core.exec_policy import PROC_READABLE_FILES
from ..core.storage_paths import write_text_atomic
from .execution_environment import resolve_tool_path
from .image_read import prepare_image_for_native_context, sniff_image_mime
from .utils import get_thread_id, is_admin

# The only errors that justify abandoning the atomic overwrite and writing in
# place: a rename needs write permission on the DIRECTORY, which a plain
# overwrite does not, so a writable file in a locked directory would otherwise
# stop being writable. Every other OSError left the target intact.
_FALLBACK_ERRNOS = frozenset({errno.EACCES, errno.EPERM, errno.EROFS})

logger = logging.getLogger(__name__)

# Fallback per-image byte cap when the active model's cap can't be resolved.
_DEFAULT_IMAGE_CAP_BYTES = 5 * 1024 * 1024
# Upper bound on raw bytes we will read in order to downscale an image. Higher
# than the 10 MB text guard so multi-MB phone photos can be resized down.
_IMAGE_READ_CEILING_BYTES = 50 * 1024 * 1024
_IMAGE_MIME_EXTENSIONS = {
    "image/png": ".png",
    "image/jpeg": ".jpg",
    "image/webp": ".webp",
    "image/gif": ".gif",
}

# Protected directories within Nymeria that should not be modified directly
# Use self_modify for tools/agents modifications instead
NYMERIA_PROTECTED_DIRS = [
    "nymeria/core",
    "nymeria/config",
    "nymeria/triggers",
    "nymeria/gateway",
    "nymeria/__init__.py",
]

# Get the Nymeria project root for path comparison
_NYMERIA_ROOT = Path(__file__).parent.parent.parent.resolve()


def protected_path_error(path: Path) -> Optional[str]:
    """Return an error message if ``path`` targets a protected Nymeria system
    directory, else ``None``.

    Shared by ``file_write`` (filesystem) and ``file_edit`` so the protected-dir
    policy and its message live in one place. The message is returned without an
    ``[Error]:`` prefix; callers format it for their own contract.
    """
    try:
        rel_path = path.relative_to(_NYMERIA_ROOT)
    except ValueError:
        return None  # path is outside the Nymeria project root; not protected
    rel_path_str = str(rel_path).replace("\\", "/")
    for protected in NYMERIA_PROTECTED_DIRS:
        if rel_path_str.startswith(protected) or rel_path_str == protected:
            logger.warning("Blocked write to protected path: %s", rel_path_str)
            return (
                f"Cannot modify protected system file: {rel_path_str}\n"
                f"Protected directories: {', '.join(NYMERIA_PROTECTED_DIRS)}\n"
                f"Use self_modify() to modify tools or agents instead."
            )
    return None


# Sensitive stores under the data dir that the file tools refuse to touch
# (resource-filesystem-layout plan, decision 7). Matched against the first
# path component relative to data_dir: "auth_tokens" covers the whole OAuth
# token-cache subtree; the "accounts.db" prefix also covers SQLite sidecars
# (accounts.db-wal / -shm / -journal). "mcp_servers" holds managed MCP server
# definitions: they carry residual credential material and, more importantly,
# the launch command a server runs, so letting the file tools plant/edit one
# would re-open an arbitrary-execution path (the sanctioned surface is
# manage_mcp / the MCP admin API, gated by the execution-trust hash). This is a
# tool-layer policy, not a security boundary: bash_execute is not
# path-checkable and the sanctioned credential surfaces are auth_write /
# auth_test.
_SECRET_STORE_DIRS = {"auth_tokens", "mcp_servers"}
_SECRET_STORE_FILE_PREFIXES = ("accounts.db",)

# Process state is a credential store too; see _process_state_error.
_PROC_ROOT = "/proc"


def _data_dir_relative(path: Path) -> Optional[Path]:
    """``path`` relative to the data dir, or None if it is outside (or unknown).

    The one place the data-dir scoping of every file-tool policy below is
    stated. That scoping is itself a filed limitation (audit B4-01: these rules
    cannot express anything about a file outside the data dir, such as `.env`,
    BY CONSTRUCTION), so it is worth having exactly one site to point at rather
    than a copy per policy.

    Returns None on a settings or resolution failure, which means the policies
    FAIL OPEN on an unreadable environment (audit C2-02, LOW, inherited). That
    is a foot-gun guard degrading, not a boundary opening; SECURITY.md 2.2 is
    the boundary.
    """
    try:
        data_dir = get_settings().data_dir.resolve()
    except Exception:
        logger.debug("Failed to resolve data dir for a file-tool policy", exc_info=True)
        return None
    try:
        return path.resolve().relative_to(data_dir)
    except (ValueError, OSError):
        return None


def _store_key(name: str) -> str:
    """Normalize a data-dir child name for comparison against a policy table.

    Case-folded, and trailing dots and spaces stripped. Both matter because the
    CONSUMER of these files is the filesystem, not this table: APFS and NTFS
    fold case, and the Win32 layer strips trailing dots and spaces, so
    ``System_Prompt.md`` and ``system_prompt.md.`` are the same file to
    ``Settings.load_soul()`` on two of the three shipped platforms while an
    exact-case lookup would see three different names.
    """
    return name.rstrip(". ").casefold()


def _process_state_error(path: Path) -> Optional[str]:
    """Refuse ``/proc``, except the machine-wide files that hold no secrets.

    The same target as the exec sandbox's ``/proc`` denial, in the other
    channel (C1-01). ``/proc/1/environ`` is the deployment's entire
    environment: the credential vault's master key, every provider key and the
    service token, in the clear, for any caller. Landlock closes that for a
    spawned command, and a file tool runs IN the API process where Landlock
    cannot reach, so without this the sandbox would be one tool call wide.

    Scoped to ``/proc`` rather than to a list of interesting files because the
    interesting ones are per-pid (``environ``, ``cmdline``, ``maps``, ``fd/``,
    ``mem``) and the pid is not knowable in advance; ``cwd`` alone would leak
    less but ``fd/`` would still hand over open descriptors. The allowlist is
    the same one the sandbox grants back, so the two channels agree on what is
    readable rather than each having an opinion.
    """
    try:
        real = os.path.realpath(path)
    except OSError:
        return None
    if real != _PROC_ROOT and not real.startswith(_PROC_ROOT + os.sep):
        return None
    if real in PROC_READABLE_FILES:
        return None
    logger.warning("Blocked file-tool access to process state: %s", real)
    return (
        f"Cannot access {real}: /proc exposes the running processes' own "
        "state, including the environment the deployment's secrets live in, "
        "so the file tools are limited to the machine-wide entries "
        f"({', '.join(PROC_READABLE_FILES)}). Commands run through "
        "bash_execute are held to the same boundary."
    )


def secrets_path_error(path: Path) -> Optional[str]:
    """Return an error message if ``path`` targets a credential store,
    else ``None``.

    Shared by ``file_read`` (which has no other denylist), ``file_write``,
    and ``file_edit``. The message is returned without an ``[Error]:``
    prefix; callers format it for their own contract.
    """
    process_error = _process_state_error(path)
    if process_error:
        return process_error

    rel = _data_dir_relative(path)
    if rel is None:
        return None  # outside the data dir; not a credential store
    parts = rel.parts
    if not parts:
        return None
    head = _store_key(parts[0])
    if head in _SECRET_STORE_DIRS or head.startswith(_SECRET_STORE_FILE_PREFIXES):
        logger.warning("Blocked file-tool access to credential store: %s", rel)
        return (
            f"Cannot access credential storage: {rel} holds Nymeria account "
            "credentials and is excluded from the file tools. Credential "
            "values are write-only by design: use auth_write to store or "
            "rotate a secret and auth_test to verify it (the "
            "credential-management skill has the full flow)."
        )
    return None


# Data-dir files whose WRITE is an admin-only action, mirroring the admin gate
# their settings routes already carry (P4-02). Nothing here is secret, so unlike
# the credential stores above these stay READABLE by anyone: the agent
# inspecting the prompt that governs it is useful and harmless.
#
# Why a role check rather than the content-hash gate its sibling stores got, and
# why role-scoped rather than an outright refusal: the rule-2 paragraph of
# `docs/private/security/control-store-matrix.md`, which owns that argument.
#
# Same tool-layer caveat as the credential denylist: `bash_execute` is not
# path-checkable, so this is policy, not a boundary (SECURITY.md 2.2).
#
# The names are literals rather than derived from `Settings`, which owns them
# (`system_prompt_override_path` and its two dream siblings). Deliberate: the
# loop is closed by tests instead, and more tightly than a lookup would close
# it. Renaming one there produces an unclassified data-dir child, the AST
# discovery gate in `tests/test_resource_layout.py` fails, the register needs a
# row, and the reconcile test then fails until this table matches it.
_ADMIN_ONLY_WRITE_FILES = {
    "system_prompt.md": "PUT /settings/system-prompt (Settings > System Prompt)",
    "dream_prompt.md": "PUT /settings/dream-prompts (Settings > Dreaming)",
    "dream_kickoff.md": "PUT /settings/dream-prompts (Settings > Dreaming)",
}


def config_principal(config: Optional[RunnableConfig]) -> Optional[str]:
    """The user the run config NAMES, or None if it names nobody.

    Deliberately not ``utils.get_user_id``, which substitutes the literal
    ``"default"`` for an absent principal. That substitution is fine for
    scoping a store by user, and wrong for a role check: ``"default"`` is also
    ``core/accounts.BOOTSTRAP_USER_ID``, the id of the account first-run
    bootstrap creates AS AN ADMIN. So on every bootstrapped deployment a caller
    that named nobody resolved to the admin and was waved through, which is the
    opposite of the intended contract. Returning None here keeps the two cases
    apart: a real solo-admin turn carries an explicit ``user_id="default"`` and
    still resolves to admin, while a caller with no run config resolves to
    nothing and ``is_admin`` fails it closed.
    """
    if config is None:
        return None
    return (config.get("configurable") or {}).get("user_id") or None


def admin_only_write_error(path: Path, user_id: Optional[str]) -> Optional[str]:
    """Error message if ``user_id`` may not write this global prompt override.

    Write-side only: ``file_read`` is deliberately not a caller. Fails closed
    through ``is_admin``, so an unresolvable principal (no agent, or a caller
    that named nobody, see ``config_principal``) is refused rather than waved
    through.

    Matching is on the FIRST path component under the data dir, not on the whole
    relative path, so a path THROUGH the guarded name is caught as well.
    """
    rel = _data_dir_relative(path)
    if rel is None or not rel.parts:
        return None
    route = _ADMIN_ONLY_WRITE_FILES.get(_store_key(rel.parts[0]))
    if route is None:
        return None

    if len(rel.parts) > 1:
        # A path through the guarded name rather than the file itself. Writing
        # it creates a DIRECTORY where `Settings.load_soul()` expects a file,
        # after which reading the prompt raises an unhandled IsADirectoryError
        # and takes agent construction and the settings route with it. Refused
        # for EVERYONE, outside the role branch, because that is an
        # availability foot-gun rather than a role question and no caller of
        # any role has a use for the path.
        logger.warning("Blocked file-tool write through a prompt-override name: %s", rel)
        return (
            f"Cannot write {rel}: {rel.parts[0]} is a global prompt override, "
            "a FILE, and writing through it would replace it with a directory "
            "that no longer loads. Pick another path."
        )

    if is_admin(user_id):
        return None
    logger.warning(
        "Blocked non-admin file-tool write to a global prompt override: %s", rel
    )
    return (
        f"Cannot write {rel}: it replaces a GLOBAL prompt for every user of "
        "this deployment, so changing it is an admin-only action (the "
        f"sanctioned surface, {route}, is admin-gated for the same reason). "
        "Reading it is still allowed. To steer one thread instead, use its "
        "per-thread instructions or dreaming config, which need no admin."
    )


def get_workspace_dir() -> Path:
    """Return the only filesystem root where mutating file tools may write."""
    return Path(os.environ.get("NYMERIA_WORKSPACE_DIR", "/workspace")).resolve()


def confine_file_tools_to_workspace() -> bool:
    """Return whether mutating file tools should reject paths outside workspace."""
    try:
        return bool(getattr(get_settings(), "nymeria_confine_file_to_workspace", False))
    except Exception:
        logger.debug("Failed to read file-tool confinement setting", exc_info=True)
        return False


def resolve_workspace_write_path(file_path: str) -> tuple[Optional[Path], Optional[str]]:
    """Resolve a requested write target, optionally enforcing workspace confinement."""
    path = resolve_tool_path(file_path)
    if not confine_file_tools_to_workspace():
        return path, None

    workspace_dir = get_workspace_dir()
    if not path.is_relative_to(workspace_dir):
        return None, (
            f"Path outside workspace: {path}. Mutating file tools are confined "
            f"to {workspace_dir}. Relative paths resolve from Nymeria's "
            f"default tool cwd; set NYMERIA_WORKSPACE_DIR or pass an absolute "
            f"path inside the workspace to change the writable root."
        )
    return path, None


def _resolve_active_llm_config(config: Optional[RunnableConfig]):
    """Resolve the current thread's LLMConfig via the agent singleton, or None."""
    try:
        from ..core.agent import get_current_agent

        agent = get_current_agent()
        if agent is None:
            return None
        return agent._get_llm_config_for_thread(get_thread_id(config))
    except Exception:
        logger.debug("file_read: could not resolve active llm_config", exc_info=True)
        return None


def _image_unsupported_note(path: Path, reason: str) -> str:
    name = path.name
    if reason == "non_vision_model":
        why = "the current model does not support image input"
        advice = "Ask the user to switch this thread to a vision-capable model"
    elif reason == "chat_completions_route":
        why = (
            "the current provider route (OpenAI-compatible chat/completions) "
            "cannot carry images in tool results"
        )
        advice = (
            "Ask the user to attach the image directly to their next message "
            "(this route accepts images in a user message, just not in a tool "
            "result), or switch to a responses-mode route or an Anthropic model"
        )
    else:  # no_active_agent / unknown / unsupported_provider
        why = "the active model or provider could not surface images"
        advice = "Ask the user to attach the image directly to their next message"
    return (
        f"[Note]: Read the image '{name}' but could not show it to you because "
        f"{why}. {advice}. The file is at {path}."
    )


def _persist_prepared_image(data: bytes, mime: str, config: Optional[RunnableConfig]) -> Optional[Path]:
    """Write downscaled image bytes to the per-thread sandbox; return the path."""
    try:
        from ..core.attachment_sandbox import get_thread_fetch_dir

        ext = _IMAGE_MIME_EXTENSIONS.get(mime, ".img")
        target = get_thread_fetch_dir(get_thread_id(config)) / f"file_read_{uuid.uuid4().hex[:12]}{ext}"
        target.write_bytes(data)
        return target
    except Exception:
        logger.warning("file_read: failed to persist prepared image", exc_info=True)
        return None


def _read_image(path: Path, file_size: int, config: Optional[RunnableConfig]):
    """Handle an image read. Returns a (content, artifact) tuple, or None to
    signal the caller should fall back to the text-read path."""
    from ..config.model_capabilities import get_attachment_limits
    from ..core.generated_image_context import (
        build_native_image_artifact,
        explain_image_context_support,
    )

    if file_size > _IMAGE_READ_CEILING_BYTES:
        return (
            f"[Error]: Image too large ({file_size} bytes). Maximum is "
            f"{_IMAGE_READ_CEILING_BYTES // (1024 * 1024)} MB.",
            {},
        )

    llm_config = _resolve_active_llm_config(config)
    if llm_config is None:
        return _image_unsupported_note(path, "no_active_agent"), {}

    supported, reason = explain_image_context_support(llm_config)
    if not supported:
        return _image_unsupported_note(path, reason), {}

    cap = get_attachment_limits(llm_config.model or "").get("max_image_bytes")
    max_bytes = cap if isinstance(cap, int) and cap > 0 else _DEFAULT_IMAGE_CAP_BYTES

    out_bytes, mime, error = prepare_image_for_native_context(path, max_image_bytes=max_bytes)
    if error is not None:
        return f"[Error]: Cannot read image: {error}.", {}
    if mime is None:
        # Pillow could not identify it as an image: read it as text instead.
        return None

    if out_bytes is None:
        artifact_path: Path = path  # fast path: original is already in budget
        downscaled = False
    else:
        persisted = _persist_prepared_image(out_bytes, mime, config)
        if persisted is None:
            return "[Error]: Could not store the prepared image for viewing.", {}
        artifact_path = persisted
        downscaled = True

    note = f"Loaded image '{path.name}' ({mime}). It is now visible to you below."
    if downscaled:
        note += " (Downscaled to fit this model's image size limit.)"
    artifact = build_native_image_artifact(
        artifact_path, mime, source="file_read", original_path=str(path)
    )
    return note, artifact


@tool(response_format="content_and_artifact")
def file_read(
    file_path: str,
    encoding: str = "utf-8",
    max_lines: Optional[int] = None,
    extraction_prompt: str = "",
    config: Annotated[RunnableConfig, InjectedToolArg] = None,
) -> tuple[str, dict]:
    """
    Read the contents of a file, including images.

    Use this tool to read text files, and to VIEW image files (png, jpeg, webp,
    gif, bmp, tiff): the image is surfaced to you directly so you can see it.
    Large images are downscaled to fit the model automatically. If the current
    model or provider route cannot receive images, you get a "[Note]: ..."
    explaining how the user can attach the image instead.

    Args:
        file_path: Absolute or relative path to the file. Relative paths
            resolve from Nymeria's detected default tool cwd.
        encoding: File encoding for text files (default utf-8)
        max_lines: Maximum number of lines to read for text files (optional,
            reads all if not specified)
        extraction_prompt: Leave empty to return the file contents as-is.
            Provide a prompt (e.g. "the failed requests and their timestamps")
            and a secondary LLM reads the file and returns only what the prompt
            asks for, instead of the full text. Best for large files where you
            want a few specific facts; skip it for small files (just read them).
            The LLM sees the file up to ~30k tokens, so for very large files
            grep/sed to the relevant section first; the result is tagged with
            the model that produced it. Ignored for images, and leave max_lines
            unset when extracting from a whole file.

    Returns:
        File contents as plain text, or a loaded-image note (with the image
        attached for you to view). Truncated text ends with
        "[Truncated after N lines]". When extraction_prompt is used, the
        extracted text ends with "[Extracted by <model>]". Errors:
        "[Error]: <reason>".
    """
    logger.info(f"Reading file: {file_path}")

    try:
        path = resolve_tool_path(file_path)

        secrets_error = secrets_path_error(path)
        if secrets_error:
            return f"[Error]: {secrets_error}", {}

        if not path.exists():
            return f"[Error]: File not found: {file_path}", {}

        if not path.is_file():
            return f"[Error]: Not a file: {file_path}", {}

        file_size = path.stat().st_size

        # Cheap magic-byte peek: route image files to the vision path before
        # attempting a text decode.
        head = b""
        try:
            with open(path, "rb") as fb:
                head = fb.read(16)
        except OSError:
            head = b""
        if sniff_image_mime(head, str(path)) is not None:
            image_result = _read_image(path, file_size, config)
            if image_result is not None:
                return image_result
            # else: not actually a decodable image, fall through to text read.

        # Text read
        max_size = 10 * 1024 * 1024  # 10 MB
        if file_size > max_size:
            return f"[Error]: File too large ({file_size} bytes). Max size is {max_size} bytes.", {}

        with open(path, "r", encoding=encoding) as f:
            if max_lines:
                lines = []
                for i, line in enumerate(f):
                    if i >= max_lines:
                        lines.append(f"\n[Truncated after {max_lines} lines]")
                        break
                    lines.append(line)
                content = "".join(lines)
            else:
                content = f.read()

        logger.debug(f"Read {len(content)} characters from {file_path}")

        if extraction_prompt.strip():
            from .llm_extract import run_extraction

            extracted, model = run_extraction(content, extraction_prompt)
            if extracted.startswith("[Error]:"):
                return extracted, {}
            return f"{extracted}\n\n[Extracted by {model}]", {}

        return content, {}

    except UnicodeDecodeError:
        return f"[Error]: Cannot decode file as {encoding}. Try a different encoding.", {}

    except PermissionError:
        return f"[Error]: Permission denied reading: {file_path}", {}

    except Exception as e:
        error_msg = f"Failed to read file: {str(e)}"
        logger.error(error_msg, exc_info=True)
        return f"[Error]: {error_msg}", {}


@tool
def file_write(
    file_path: str,
    content: str,
    encoding: str = "utf-8",
    create_directories: bool = True,
    append: bool = False,
    attach: bool = False,
    config: Annotated[RunnableConfig, InjectedToolArg] = None,
) -> str:
    """
    Write content to a file.

    Use this tool to create or modify text files on the filesystem.
    Set attach=True to send the file to the user in chat (Telegram/Discord) after writing.

    Args:
        file_path: Absolute or relative path to the file. Relative paths
            resolve from Nymeria's detected default tool cwd.
        content: Content to write to the file
        encoding: File encoding (default utf-8)
        create_directories: Create parent directories if they don't exist (default True)
        append: Append to file instead of overwriting (default False)
        attach: Send the written file to the user as a downloadable attachment (default False)

    Returns:
        "[Success]: Wrote N characters to <path>" (or "Appended").
        If attach=True, includes "[attach:<path>]" tag for chat delivery.
        Errors: "[Error]: <reason>".
    """
    logger.info(f"Writing to file: {file_path} (append={append})")

    try:
        path, workspace_error = resolve_workspace_write_path(file_path)
        if workspace_error:
            return f"[Error]: {workspace_error}"
        assert path is not None

        # Check if this is a protected Nymeria system file
        protected_error = protected_path_error(path)
        if protected_error:
            return f"[Error]: {protected_error}"

        # Credential stores are excluded from the file tools entirely.
        secrets_error = secrets_path_error(path)
        if secrets_error:
            return f"[Error]: {secrets_error}"

        # Global prompt overrides are readable, but changing one is admin-only.
        admin_only_error = admin_only_write_error(path, config_principal(config))
        if admin_only_error:
            return f"[Error]: {admin_only_error}"

        # Create parent directories if requested
        if create_directories:
            path.parent.mkdir(parents=True, exist_ok=True)

        if not path.parent.exists():
            return f"[Error]: Directory does not exist: {path.parent}"

        if append:
            with open(path, "a", encoding=encoding) as f:
                f.write(content)
        else:
            # Overwrites go through the atomic temp-plus-rename path. This tool
            # is the sanctioned way to hand-edit Nymeria's own JSON stores under
            # the resource root, and a bare truncating write that dies mid-way
            # leaves a torn file the store loaders then QUARANTINE, losing the
            # prior good bytes. ``file_edit`` already writes via rename; this
            # closes the same gap here. Appends stay in place: there is no
            # atomic append, and read-modify-write would corrupt concurrent
            # appenders rather than protect them.
            #
            def _write_in_place() -> None:
                with open(path, "w", encoding=encoding) as f:  # type: ignore[arg-type]
                    f.write(content)

            if path.exists() and not os.access(path, os.W_OK):
                # A rename only needs permission on the DIRECTORY, so the
                # atomic path would happily replace a file the user made
                # read-only. Writing in place keeps `chmod 444` meaning what it
                # has always meant here: PermissionError, same refusal as
                # before the write became atomic.
                _write_in_place()
            else:
                try:
                    write_text_atomic(path, content, encoding=encoding)
                except OSError as exc:
                    # The rename ALSO needs write permission on the directory,
                    # which a plain overwrite does not. Rather than lose the
                    # ability to write a file in a directory the user cannot
                    # create temps in, fall back. The atomic attempt cleans up
                    # its own temp and leaves the target untouched, so this
                    # cannot write twice.
                    #
                    # ONLY for permission errors. A full disk or a failing
                    # device also raises OSError, and there the atomic attempt
                    # failed SAFELY with the target intact; falling back would
                    # open it "w", truncate it, and then fail too, turning a
                    # clean failure into data loss on the very store files this
                    # path exists to protect.
                    if exc.errno not in _FALLBACK_ERRNOS:
                        raise
                    _write_in_place()

        action = "Appended to" if append else "Wrote"
        logger.debug(f"{action} {len(content)} characters to {file_path}")
        result = f"[Success]: {action} {len(content)} characters to {file_path}"
        if attach:
            workspace_dir = get_workspace_dir()
            if path.is_relative_to(workspace_dir):
                result += f"\n[attach:{path}]"
            else:
                result += (
                    f"\n[Info]: Attachment skipped. Only files inside "
                    f"{workspace_dir} can be delivered to chat clients."
                )
        return result

    except PermissionError:
        return f"[Error]: Permission denied writing to: {file_path}"

    except Exception as e:
        error_msg = f"Failed to write file: {str(e)}"
        logger.error(error_msg, exc_info=True)
        return f"[Error]: {error_msg}"
