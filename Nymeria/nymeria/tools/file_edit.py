"""Precise file editing tool for Nymeria."""

from __future__ import annotations

import difflib
import hashlib
import json
import logging
import os
import stat
import tempfile
from pathlib import Path
from typing import Any, Literal, Optional

from langchain_core.tools import tool
from pydantic import BaseModel, Field, ValidationError

from .filesystem import NYMERIA_PROTECTED_DIRS, _NYMERIA_ROOT, resolve_workspace_write_path

logger = logging.getLogger(__name__)

TOOL_VERSION = "2026-05-01.1"
MAX_FILE_SIZE_BYTES = 10 * 1024 * 1024


class FileEditOperation(BaseModel):
    """One exact edit operation for ``file_edit``."""

    operation: Literal[
        "replace",
        "delete",
        "insert_before",
        "insert_after",
        "replace_range",
    ] = Field(description="Edit operation to apply.")
    old_text: Optional[str] = Field(
        default=None,
        description=(
            "Exact text to match. Required for every operation. For "
            "replace_range it must equal the selected line range exactly."
        ),
    )
    new_text: str = Field(
        default="",
        description="Replacement or inserted text. Ignored for delete.",
    )
    occurrence: Optional[int] = Field(
        default=None,
        ge=1,
        description=(
            "1-based occurrence to edit. Omit only when old_text appears "
            "exactly once."
        ),
    )
    start_line: Optional[int] = Field(
        default=None,
        ge=1,
        description="1-based inclusive start line for replace_range.",
    )
    end_line: Optional[int] = Field(
        default=None,
        ge=1,
        description="1-based inclusive end line for replace_range.",
    )


class FileEditInput(BaseModel):
    """Input schema for ``file_edit``."""

    file_path: str = Field(
        description=(
            "Absolute or relative path to an existing file. Relative paths "
            "resolve from Nymeria's detected default tool cwd."
        ),
    )
    edits: list[FileEditOperation] = Field(
        description="Ordered exact edit operations to apply all-or-nothing.",
    )
    encoding: str = Field(default="utf-8", description="File encoding.")
    dry_run: bool = Field(default=False, description="Return diff without writing.")
    expected_sha256: Optional[str] = Field(
        default=None,
        description="Optional SHA-256 of the current file bytes.",
    )
    max_diff_chars: int = Field(
        default=20000,
        ge=0,
        description="Maximum unified diff characters to return.",
    )


def _json_result(**payload: Any) -> str:
    return json.dumps({"tool_version": TOOL_VERSION, **payload}, indent=2, default=str)


def _error_result(
    error_type: str,
    message: str,
    *,
    file_path: Optional[str] = None,
    dry_run: bool = False,
    edit_index: Optional[int] = None,
    original_sha256: Optional[str] = None,
    details: Optional[dict[str, Any]] = None,
) -> str:
    error: dict[str, Any] = {"type": error_type, "message": message}
    if edit_index is not None:
        error["edit_index"] = edit_index
    if details:
        error["details"] = details
    result: dict[str, Any] = {
        "ok": False,
        "dry_run": dry_run,
        "error": error,
    }
    if file_path is not None:
        result["file_path"] = file_path
    if original_sha256 is not None:
        result["original_sha256"] = original_sha256
    return _json_result(**result)


def _protected_write_error(path: Path) -> Optional[str]:
    try:
        rel_path = path.relative_to(_NYMERIA_ROOT)
        rel_path_str = str(rel_path).replace("\\", "/")

        for protected in NYMERIA_PROTECTED_DIRS:
            if rel_path_str.startswith(protected) or rel_path_str == protected:
                logger.warning("Blocked edit to protected path: %s", rel_path_str)
                return (
                    f"Cannot modify protected system file: {rel_path_str}\n"
                    f"Protected directories: {', '.join(NYMERIA_PROTECTED_DIRS)}\n"
                    f"Use self_modify() to modify tools or agents instead."
                )
    except ValueError:
        pass  # not a valid integer, skip
    return None


def _sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _detect_newline(content: str) -> str:
    crlf = content.count("\r\n")
    without_crlf = content.replace("\r\n", "")
    lf = without_crlf.count("\n")
    cr = without_crlf.count("\r")
    if crlf >= lf and crlf >= cr and crlf > 0:
        return "\r\n"
    if cr > lf and cr > 0:
        return "\r"
    return "\n"


def _normalize_newlines(text: str, newline: str) -> str:
    if newline == "\n":
        return text
    normalized = text.replace("\r\n", "\n").replace("\r", "\n")
    return normalized.replace("\n", newline)


def _find_occurrence(
    content: str,
    old_text: str,
    occurrence: Optional[int],
) -> tuple[Optional[tuple[int, int]], Optional[dict[str, Any]]]:
    if old_text == "":
        return None, {
            "type": "empty_match",
            "message": "old_text must not be empty",
        }

    starts: list[int] = []
    start = 0
    while True:
        index = content.find(old_text, start)
        if index == -1:
            break
        starts.append(index)
        start = index + len(old_text)

    if occurrence is None:
        if len(starts) != 1:
            return None, {
                "type": "match_count",
                "message": (
                    f"old_text matched {len(starts)} time(s); omit occurrence "
                    "only when it matches exactly once"
                ),
                "match_count": len(starts),
            }
        selected = starts[0]
        return (selected, selected + len(old_text)), None

    if occurrence > len(starts):
        return None, {
            "type": "occurrence_not_found",
            "message": (
                f"Requested occurrence {occurrence}, but old_text matched "
                f"{len(starts)} time(s)"
            ),
            "match_count": len(starts),
            "occurrence": occurrence,
        }

    selected = starts[occurrence - 1]
    return (selected, selected + len(old_text)), None


def _replace_line_range(
    content: str,
    op: FileEditOperation,
    newline: str,
) -> tuple[Optional[str], Optional[dict[str, Any]]]:
    if op.start_line is None or op.end_line is None:
        return None, {
            "type": "invalid_range",
            "message": "replace_range requires start_line and end_line",
        }
    if op.end_line < op.start_line:
        return None, {
            "type": "invalid_range",
            "message": "end_line must be greater than or equal to start_line",
        }
    if op.old_text is None or op.old_text == "":
        return None, {
            "type": "missing_old_text",
            "message": "replace_range requires non-empty old_text",
        }

    lines = content.splitlines(keepends=True)
    if not lines:
        return None, {
            "type": "invalid_range",
            "message": "Cannot replace a line range in an empty file",
            "line_count": 0,
        }
    if op.end_line > len(lines):
        return None, {
            "type": "invalid_range",
            "message": (
                f"Requested lines {op.start_line}-{op.end_line}, but file "
                f"has {len(lines)} line(s)"
            ),
            "line_count": len(lines),
        }

    prefix = "".join(lines[: op.start_line - 1])
    selected = "".join(lines[op.start_line - 1 : op.end_line])
    suffix = "".join(lines[op.end_line :])
    if selected != op.old_text:
        return None, {
            "type": "range_context_mismatch",
            "message": "old_text does not exactly match the selected line range",
        }

    return prefix + _normalize_newlines(op.new_text, newline) + suffix, None


def _apply_operation(
    content: str,
    op: FileEditOperation,
    newline: str,
) -> tuple[Optional[str], Optional[dict[str, Any]]]:
    if op.operation == "replace_range":
        return _replace_line_range(content, op, newline)

    if op.old_text is None or op.old_text == "":
        return None, {
            "type": "missing_old_text",
            "message": f"{op.operation} requires non-empty old_text",
        }

    span, error = _find_occurrence(content, op.old_text, op.occurrence)
    if error is not None:
        return None, error
    assert span is not None
    start, end = span

    if op.operation == "replace":
        replacement = _normalize_newlines(op.new_text, newline)
        return content[:start] + replacement + content[end:], None
    if op.operation == "delete":
        return content[:start] + content[end:], None
    if op.operation == "insert_before":
        insertion = _normalize_newlines(op.new_text, newline)
        return content[:start] + insertion + content[start:], None
    if op.operation == "insert_after":
        insertion = _normalize_newlines(op.new_text, newline)
        return content[:end] + insertion + content[end:], None

    return None, {
        "type": "invalid_operation",
        "message": f"Unsupported operation: {op.operation}",
    }


def _diff_text(
    before: str,
    after: str,
    path: Path,
    max_diff_chars: int,
) -> tuple[str, bool]:
    diff = "".join(
        difflib.unified_diff(
            before.splitlines(keepends=True),
            after.splitlines(keepends=True),
            fromfile=f"{path} (before)",
            tofile=f"{path} (after)",
        )
    )
    if max_diff_chars == 0:
        return "", bool(diff)
    if len(diff) <= max_diff_chars:
        return diff, False
    marker = "\n[Diff truncated]\n"
    return diff[: max(0, max_diff_chars - len(marker))] + marker, True


def _atomic_write(path: Path, content: str, encoding: str, mode: int) -> None:
    tmp_name: Optional[str] = None
    fd = -1
    try:
        fd, tmp_name = tempfile.mkstemp(
            prefix=f".{path.name}.",
            suffix=".tmp",
            dir=str(path.parent),
        )
        with os.fdopen(fd, "w", encoding=encoding, newline="") as handle:
            fd = -1
            handle.write(content)
            handle.flush()
            os.fsync(handle.fileno())
        tmp_path = Path(tmp_name)
        tmp_path.chmod(stat.S_IMODE(mode))
        os.replace(tmp_path, path)
    finally:
        if fd != -1:
            os.close(fd)
        if tmp_name:
            tmp_path = Path(tmp_name)
            if tmp_path.exists():
                try:
                    tmp_path.unlink()
                except OSError:
                    logger.warning("Failed to remove temp file: %s", tmp_path)


def _parse_edits(edits: list[Any]) -> tuple[Optional[list[FileEditOperation]], Optional[str]]:
    parsed: list[FileEditOperation] = []
    for edit in edits:
        try:
            parsed.append(
                edit
                if isinstance(edit, FileEditOperation)
                else FileEditOperation.model_validate(edit)
            )
        except ValidationError as exc:
            return None, str(exc)
    return parsed, None


@tool(args_schema=FileEditInput)
def file_edit(
    file_path: str,
    edits: list[dict[str, Any]],
    encoding: str = "utf-8",
    dry_run: bool = False,
    expected_sha256: Optional[str] = None,
    max_diff_chars: int = 20000,
) -> str:
    """
    Precisely edit an existing text file with exact, all-or-nothing operations.

    Use this instead of rewriting a full file when you need surgical edits.
    Each edit is applied in order to the in-memory result of previous edits.
    Nothing is written unless every edit validates successfully.

    Args:
        file_path: Absolute or relative path to an existing file. Relative
            paths resolve from Nymeria's detected default tool cwd.
        edits: Ordered edit objects. operation is replace, delete,
            insert_before, insert_after, or replace_range.
        encoding: File encoding (default utf-8)
        dry_run: If true, return the diff without writing
        expected_sha256: Optional SHA-256 of the current file bytes. If set,
            the edit fails unless the file still matches this hash.
        max_diff_chars: Maximum characters of unified diff to return

    Returns:
        JSON string with success/error details and a unified diff.
    """
    logger.info("Editing file: %s (dry_run=%s)", file_path, dry_run)

    if max_diff_chars < 0:
        return _error_result(
            "invalid_argument",
            "max_diff_chars must be 0 or greater",
            file_path=file_path,
            dry_run=dry_run,
        )
    if not edits:
        return _error_result(
            "invalid_argument",
            "edits must contain at least one operation",
            file_path=file_path,
            dry_run=dry_run,
        )

    try:
        path, workspace_error = resolve_workspace_write_path(file_path)
        if workspace_error:
            return _error_result(
                "path_outside_workspace",
                workspace_error,
                file_path=file_path,
                dry_run=dry_run,
            )
        assert path is not None

        protected_error = _protected_write_error(path)
        if protected_error:
            return _error_result(
                "protected_path",
                protected_error,
                file_path=str(path),
                dry_run=dry_run,
            )

        if not path.exists():
            return _error_result(
                "file_not_found",
                f"File not found: {file_path}",
                file_path=str(path),
                dry_run=dry_run,
            )
        if not path.is_file():
            return _error_result(
                "not_a_file",
                f"Not a file: {file_path}",
                file_path=str(path),
                dry_run=dry_run,
            )

        stat_result = path.stat()
        if stat_result.st_size > MAX_FILE_SIZE_BYTES:
            return _error_result(
                "file_too_large",
                (
                    f"File too large ({stat_result.st_size} bytes). "
                    f"Max size is {MAX_FILE_SIZE_BYTES} bytes."
                ),
                file_path=str(path),
                dry_run=dry_run,
            )

        original_bytes = path.read_bytes()
        original_sha256 = _sha256_bytes(original_bytes)
        if expected_sha256 and expected_sha256.lower() != original_sha256:
            return _error_result(
                "hash_mismatch",
                "expected_sha256 does not match the current file",
                file_path=str(path),
                dry_run=dry_run,
                original_sha256=original_sha256,
                details={"expected_sha256": expected_sha256.lower()},
            )

        try:
            original_content = original_bytes.decode(encoding)
        except UnicodeDecodeError:
            return _error_result(
                "decode_error",
                f"Cannot decode file as {encoding}. Try a different encoding.",
                file_path=str(path),
                dry_run=dry_run,
                original_sha256=original_sha256,
            )

        parsed_edits, parse_error = _parse_edits(edits)
        if parse_error:
            return _error_result(
                "invalid_edit",
                parse_error,
                file_path=str(path),
                dry_run=dry_run,
                original_sha256=original_sha256,
            )
        assert parsed_edits is not None

        newline = _detect_newline(original_content)
        current_content = original_content
        for index, op in enumerate(parsed_edits):
            current_content, edit_error = _apply_operation(current_content, op, newline)
            if edit_error is not None:
                return _error_result(
                    edit_error.get("type", "edit_failed"),
                    edit_error.get("message", "Edit failed"),
                    file_path=str(path),
                    dry_run=dry_run,
                    edit_index=index,
                    original_sha256=original_sha256,
                    details={
                        k: v
                        for k, v in edit_error.items()
                        if k not in {"type", "message"}
                    },
                )
            assert current_content is not None

        new_bytes = current_content.encode(encoding)
        new_sha256 = _sha256_bytes(new_bytes)
        diff, diff_truncated = _diff_text(
            original_content,
            current_content,
            path,
            max_diff_chars,
        )

        if not dry_run:
            _atomic_write(path, current_content, encoding, stat_result.st_mode)

        return _json_result(
            ok=True,
            dry_run=dry_run,
            file_path=str(path),
            edits_requested=len(parsed_edits),
            edits_applied=len(parsed_edits),
            original_sha256=original_sha256,
            new_sha256=new_sha256,
            bytes_written=0 if dry_run else len(new_bytes),
            changed=original_sha256 != new_sha256,
            diff=diff,
            diff_truncated=diff_truncated,
        )

    except PermissionError:
        return _error_result(
            "permission_denied",
            f"Permission denied editing: {file_path}",
            file_path=file_path,
            dry_run=dry_run,
        )
    except Exception as exc:
        logger.error("file_edit failed: %s", exc, exc_info=True)
        return _error_result(
            "unexpected_error",
            f"Failed to edit file: {exc}",
            file_path=file_path,
            dry_run=dry_run,
        )


FILE_EDIT_TOOLS = [file_edit]
