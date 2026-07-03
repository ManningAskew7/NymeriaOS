"""Hardline guard against catastrophic shell commands.

This is a small, always-on blocklist that refuses a handful of commands that
would destroy the host or take down Nymeria itself: wiping the disk, formatting
filesystems, fork bombs, shutting the machine down, or killing the Nymeria
process/containers. It applies to every ``bash_execute`` call, admin or not, and
has no runtime off switch.

IMPORTANT: this is an accident-and-naive-injection guard, NOT a security
boundary. A determined caller can trivially bypass a textual blocklist
(``r''m -rf /``, ``$(printf rm) -rf /``, ``base64 -d <<<... | sh``, etc.). The
real isolation boundary is the OS/container the backend runs in. The value here
is catching a hallucinated or prompt-injected ``rm -rf /`` before it runs, at
near-zero false-positive cost, not sandboxing an adversary.

Matching model:

- Heredoc bodies are removed first, so writing a script whose CONTENT mentions
  ``reboot`` or ``dd of=/dev/sda`` is not blocked (nothing in a heredoc body is
  executed by the outer command).
- The command is split into shell "segments" on ``; && || | &`` and newlines,
  quote-aware, so operators inside quoted strings do not start a new segment
  (``git commit -m "fix; reboot loop"`` is one segment and safe).
- Common wrapper prefixes (``sudo``, ``nohup``, ``env``, ``exec``, ...) are
  stripped from each segment, including their option values (``sudo -u root
  reboot`` is seen as ``reboot``). A quoted wrapper option value containing
  spaces defeats the (whitespace-tokenized) stripper; accepted, this is an
  accident guard.
- Destructive-verb patterns are anchored at the start of a segment and must be
  the WHOLE leading word, so ``echo "run rm -rf / to wipe"``, ``git commit -m
  "reboot the box"``, and ``reboot-guard --status`` are safe while ``rm -rf /``
  and ``foo && reboot`` are blocked.
- ``bash -c "<payload>"`` (and sh/zsh/dash/ksh) recursively re-checks the
  payload, since that is a common idiom for the model itself to emit.
- A few patterns that are dangerous regardless of position (fork bombs, ``dd``
  or a redirect onto a raw block device) match anywhere OUTSIDE quoted text.
"""

from __future__ import annotations

import re
from typing import Optional, Pattern

_MAX_RECURSION_DEPTH = 3

# Leading subshell/group punctuation and env-var assignments (FOO=bar cmd)
# that can precede the real verb inside a segment.
_SEGMENT_LEAD = re.compile(r"^[\s(){]*(?:[A-Za-z_][A-Za-z0-9_]*=\S*\s+)*")

# Wrapper commands that just run another command. Stripping them (including
# their option values) lets the patterns anchor on the real verb. Short options
# listed here take a SEPARATE value token (``sudo -u root``); everything else
# dash-prefixed is consumed as a lone flag.
_WRAPPER_SHORT_VALUE_OPTS = {
    "sudo": set("ugpCDRTU"),
    "doas": set("uC"),
    "env": set("uCS"),
    "nice": set("n"),
    "ionice": set("cnp"),
    "stdbuf": set("ioe"),
    "timeout": set("ks"),
}
# Long options that take a separate value token (the ``--opt=value`` form is a
# single token and needs no entry).
_WRAPPER_LONG_VALUE_OPTS = {
    "sudo": {
        "--user", "--group", "--prompt", "--close-from", "--chdir", "--chroot",
        "--host", "--role", "--type", "--other-user", "--command-timeout",
    },
    "env": {"--unset", "--split-string", "--chdir"},
    "nice": {"--adjustment"},
    "ionice": {"--class", "--classdata", "--pid"},
    "stdbuf": {"--input", "--output", "--error"},
    "timeout": {"--kill-after", "--signal"},
}
_SIMPLE_WRAPPERS = {"nohup", "setsid", "exec", "command", "builtin", "time"}

_ASSIGNMENT_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*=")
_TIMEOUT_DURATION_RE = re.compile(r"^[\d.]+[smhd]?$")

# Heredoc opener: << or <<- plus an optionally quoted delimiter word. The
# lookarounds exclude the ``<<<`` here-string operator.
_HEREDOC_OPEN_RE = re.compile(r"(?<!<)<<(?!<)-?\s*(['\"]?)([A-Za-z_][A-Za-z0-9_]*)\1")

# Shells whose ``-c <payload>`` should be recursively checked.
_SHELL_DASH_C_RE = re.compile(r"^(?:bash|sh|zsh|dash|ksh)\s+(.*)$", re.IGNORECASE)


def _seg(pattern: str) -> Pattern[str]:
    return re.compile(pattern, re.IGNORECASE)


# End-of-word boundary for a leading destructive verb: whitespace or end of
# segment. Deliberately NOT ``\b``, which would also match before ``-`` or
# ``.`` and block innocent names like ``reboot-guard`` or ``shutdown.sh``.
_END = r"(?=\s|$)"

# (compiled pattern, human reason). Anchored at the start of a command segment
# after wrapper stripping.
_SEGMENT_PATTERNS: list[tuple[Pattern[str], str]] = [
    # Shutting the box down / rebooting.
    (
        _seg(r"^(?:shutdown|reboot|halt|poweroff)" + _END),
        "shuts down or reboots the host",
    ),
    (_seg(r"^init\s+[06]" + _END), "changes runlevel to halt/reboot"),
    (
        _seg(r"^systemctl\s+(?:poweroff|reboot|halt|suspend|hibernate)" + _END),
        "powers off or reboots the host via systemctl",
    ),
    # Formatting a filesystem / wiping a disk's partition signatures.
    (_seg(r"^mkfs(?:\.\w+)?" + _END), "formats a filesystem"),
    (
        _seg(r"^wipefs(?=\s).*\s(?:-a|--all)" + _END),
        "wipes filesystem signatures from a disk",
    ),
    # Killing Nymeria itself (self-termination) or its containers.
    (
        _seg(r"^(?:pkill|killall)(?=\s).*\bnymeria\b"),
        "kills the Nymeria process (self-termination)",
    ),
    (
        _seg(r"^systemctl\s+(?:stop|kill|disable)\s+.*nymeria"),
        "stops the Nymeria service (self-termination)",
    ),
    (
        _seg(r"^docker\s+(?:kill|stop|rm)\s+.*nymeria"),
        "kills Nymeria's own container (self-termination)",
    ),
]

# Dangerous anywhere in the command line (outside quoted text).
_ANYWHERE_PATTERNS: list[tuple[Pattern[str], str]] = [
    # Classic fork bomb ``:(){ :|:& };:`` and light spacing variants.
    (
        re.compile(r":\s*\(\s*\)\s*\{\s*:\s*\|\s*:\s*&\s*\}\s*;\s*:"),
        "fork bomb",
    ),
    # dd or a redirect writing onto a raw block device.
    (
        re.compile(
            r"\bdd\b[^\n]*\bof=\s*['\"]?/dev/(?:sd|nvme|hd|vd|disk|mmcblk|xvd)",
            re.IGNORECASE,
        ),
        "writes a raw image onto a block device",
    ),
    (
        re.compile(r">\s*/dev/(?:sd|nvme|hd|vd|disk|mmcblk|xvd)\w*", re.IGNORECASE),
        "redirects output onto a raw block device",
    ),
]

# rm targets that mean "the filesystem root" or "the whole home dir".
_RM_ROOT_TARGETS = {"/", "~", "~/", "$HOME", "${HOME}", '"$HOME"', '"${HOME}"'}
_RM_ROOT_GLOB = re.compile(r"^/(?:\*+/?)+$")  # /* /*/ /** ...


def _strip_heredoc_bodies(command: str) -> str:
    """Drop heredoc body lines (they are data, not commands).

    Best-effort and line-based: after a ``<<DELIM`` opener, lines up to (and
    including) the delimiter line are removed. The opener line itself is kept
    and still scanned. An unterminated heredoc drops the rest of the command.
    """
    if "<<" not in command or "\n" not in command:
        return command
    lines = command.split("\n")
    out: list[str] = []
    i = 0
    while i < len(lines):
        line = lines[i]
        out.append(line)
        match = _HEREDOC_OPEN_RE.search(line)
        i += 1
        if match:
            delimiter = match.group(2)
            while i < len(lines):
                candidate = lines[i]
                i += 1
                if candidate.rstrip() == delimiter or candidate.lstrip("\t").rstrip() == delimiter:
                    break
    return "\n".join(out)


def _split_segments(command: str) -> list[str]:
    """Split a command line into shell segments, quote-aware.

    Splits on ``;``, newlines, ``|``, ``||``, ``&``, ``&&`` occurring outside
    single/double quotes. This is still deliberately coarse (no subshell
    awareness): it only feeds position anchoring for the patterns.
    """
    segments: list[str] = []
    current: list[str] = []
    quote: Optional[str] = None
    escaped = False
    i, n = 0, len(command)
    while i < n:
        ch = command[i]
        if escaped:
            current.append(ch)
            escaped = False
        elif ch == "\\" and quote != "'":
            current.append(ch)
            escaped = True
        elif quote is not None:
            if ch == quote:
                quote = None
            current.append(ch)
        elif ch in "'\"":
            quote = ch
            current.append(ch)
        elif ch in ";\n":
            segments.append("".join(current))
            current = []
        elif ch in "&|":
            if i + 1 < n and command[i + 1] == ch:
                i += 1
            segments.append("".join(current))
            current = []
        else:
            current.append(ch)
        i += 1
    segments.append("".join(current))
    return segments


def _strip_quoted(command: str) -> str:
    """Return the command with quoted spans blanked out (quotes kept)."""
    out: list[str] = []
    quote: Optional[str] = None
    escaped = False
    for ch in command:
        if escaped:
            out.append(" " if quote else ch)
            escaped = False
        elif ch == "\\" and quote != "'":
            out.append(ch if quote is None else " ")
            escaped = True
        elif quote is not None:
            if ch == quote:
                quote = None
                out.append(ch)
            else:
                out.append(" " if ch != "\n" else "\n")
        elif ch in "'\"":
            quote = ch
            out.append(ch)
        else:
            out.append(ch)
    return "".join(out)


def _strip_wrappers(segment: str) -> str:
    """Strip leading env assignments and wrapper commands (with their option
    values) from a segment, leaving the real verb in the leading position."""
    seg = segment
    for _ in range(8):  # wrappers nest at most a few deep in practice
        seg = _SEGMENT_LEAD.sub("", seg).strip()
        tokens = seg.split()
        if not tokens:
            return ""
        head = tokens[0].lower()
        if head not in _SIMPLE_WRAPPERS and head not in _WRAPPER_SHORT_VALUE_OPTS:
            return seg
        short_value = _WRAPPER_SHORT_VALUE_OPTS.get(head, set())
        long_value = _WRAPPER_LONG_VALUE_OPTS.get(head, set())
        i = 1
        while i < len(tokens):
            token = tokens[i]
            if token == "--":
                i += 1
                break
            if token.startswith("--"):
                i += 2 if "=" not in token and token in long_value else 1
                continue
            if token.startswith("-") and len(token) > 1:
                i += 2 if len(token) == 2 and token[1] in short_value else 1
                continue
            if head == "timeout" and _TIMEOUT_DURATION_RE.match(token):
                i += 1
                continue
            if head == "env" and _ASSIGNMENT_RE.match(token):
                i += 1
                continue
            break
        seg = " ".join(tokens[i:])
    return seg.strip()


def _first_shell_word(text: str) -> str:
    """The first shell word of *text*, honouring a leading quote."""
    text = text.lstrip()
    if not text:
        return ""
    quote = text[0]
    if quote in "'\"":
        escaped = False
        for i in range(1, len(text)):
            ch = text[i]
            if escaped:
                escaped = False
            elif ch == "\\" and quote == '"':
                escaped = True
            elif ch == quote:
                return text[1:i]
        return text[1:]
    return text.split()[0]


def _shell_dash_c_payload(segment: str) -> Optional[str]:
    """The payload of a ``bash -c '<payload>'``-style segment, else None."""
    match = _SHELL_DASH_C_RE.match(segment)
    if not match:
        return None
    rest = match.group(1).lstrip()
    saw_c = False
    while rest.startswith("-"):
        word, _, remainder = rest.partition(" ")
        if word == "--":
            rest = remainder.lstrip()
            break
        if not word.startswith("--") and "c" in word[1:]:
            saw_c = True
        rest = remainder.lstrip()
    if not saw_c or not rest:
        return None
    return _first_shell_word(rest) or None


def _check_rm_segment(segment: str) -> Optional[str]:
    """Block ``rm`` with recursive+force flags against a root-ish target."""
    tokens = segment.split()
    if not tokens or tokens[0].lower() != "rm":
        return None
    recursive = force = False
    targets: list[str] = []
    for token in tokens[1:]:
        if token == "--no-preserve-root":
            return "rm with --no-preserve-root"
        if token.startswith("--"):
            if token == "--recursive":
                recursive = True
            elif token == "--force":
                force = True
        elif token.startswith("-") and len(token) > 1:
            flags = token[1:]
            if "r" in flags or "R" in flags:
                recursive = True
            if "f" in flags:
                force = True
        else:
            targets.append(token.strip("'\""))
    if not (recursive and force):
        return None
    for target in targets:
        if target in _RM_ROOT_TARGETS or _RM_ROOT_GLOB.match(target):
            return "recursive force-delete of a filesystem or home root"
    return None


def check_command_guard(command: str, _depth: int = 0) -> Optional[str]:
    """Return a human-readable block reason, or ``None`` if the command is allowed.

    ``None`` means "not on the hardline list" -- it is NOT an assertion that the
    command is safe (see the module docstring).
    """
    if not command or not command.strip() or _depth > _MAX_RECURSION_DEPTH:
        return None

    command = _strip_heredoc_bodies(command)

    unquoted = _strip_quoted(command)
    for pattern, reason in _ANYWHERE_PATTERNS:
        if pattern.search(unquoted):
            return reason

    for raw_segment in _split_segments(command):
        segment = _strip_wrappers(raw_segment)
        if not segment:
            continue
        reason = _check_rm_segment(segment)
        if reason:
            return reason
        for pattern, reason in _SEGMENT_PATTERNS:
            if pattern.search(segment):
                return reason
        payload = _shell_dash_c_payload(segment)
        if payload:
            reason = check_command_guard(payload, _depth + 1)
            if reason:
                return reason

    return None
