"""Filesystem tools for Nymeria."""

import logging
import os
import uuid
from pathlib import Path
from typing import Annotated, Optional

from langchain_core.runnables import RunnableConfig
from langchain_core.tools import InjectedToolArg, tool

from ..config import get_settings
from .execution_environment import resolve_tool_path
from .image_read import prepare_image_for_native_context, sniff_image_mime
from .utils import get_thread_id

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

    Returns:
        File contents as plain text, or a loaded-image note (with the image
        attached for you to view). Truncated text ends with
        "[Truncated after N lines]". Errors: "[Error]: <reason>".
    """
    logger.info(f"Reading file: {file_path}")

    try:
        path = resolve_tool_path(file_path)

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
        try:
            rel_path = path.relative_to(_NYMERIA_ROOT)
            rel_path_str = str(rel_path).replace("\\", "/")

            for protected in NYMERIA_PROTECTED_DIRS:
                if rel_path_str.startswith(protected) or rel_path_str == protected:
                    logger.warning(f"Blocked write to protected path: {rel_path_str}")
                    return (
                        f"[Error]: Cannot modify protected system file: {rel_path_str}\n"
                        f"Protected directories: {', '.join(NYMERIA_PROTECTED_DIRS)}\n"
                        f"Use self_modify() to modify tools or agents instead."
                    )
        except ValueError:
            # Path is outside Nymeria project - allow it
            pass

        # Create parent directories if requested
        if create_directories:
            path.parent.mkdir(parents=True, exist_ok=True)

        if not path.parent.exists():
            return f"[Error]: Directory does not exist: {path.parent}"

        mode = "a" if append else "w"
        with open(path, mode, encoding=encoding) as f:
            f.write(content)

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
