#!/usr/bin/env python3
"""Gate for the per-directory agent guide family (CLAUDE.md/AGENTS.md).

Checks, all cheap and offline:

1. Path liveness: every path-shaped token a guide names (inline code spans
   AND fenced command blocks) must exist in the GIT INDEX, resolved against
   that guide's declared base dirs (GUIDE_BASES, mirroring each guide's own
   path conventions). The index is the oracle, not the working tree, so a
   path that only exists as a gitignored or untracked file on one machine
   still fails: the check means "a fresh clone has this". The `shipped/NN`
   shorthand resolves against the shipped/ filenames. Bare filenames (no
   slash), globs, placeholders, URLs, schemeless domains, versions, and
   env-var shorthands are not checked. `pkg/module.symbol` spellings retry
   as `pkg/module.py` only when the trailing segment is not a known file
   extension, so a dead `foo.md` is never rescued by a live `foo.py`.
2. Word budgets: each guide stays under its BUDGETS entry (words, not
   lines: long table rows make line counts meaningless) so density cannot
   silently regrow; always-loaded context reduces adherence and the
   2026-08-02 split exists to bound per-session cost. A guide with no
   BUDGETS or GUIDE_BASES entry FAILS: classify new guides deliberately.
   Raise a budget in a reviewed commit, or trim the guide.
3. Twin integrity: every CLAUDE.md must have a byte-identical AGENTS.md
   beside it, and every AGENTS.md a CLAUDE.md. A MISSING twin fails (the
   operator PostToolUse hook only syncs existing pairs, so a deleted twin
   can never re-converge on its own).
4. Mirror fail-safe: every guide's first line must start with
   "# CLAUDE.md" so the publish-prep blob callback swaps a nested guide
   (harmless) rather than shipping it verbatim if the paths_to_remove.txt
   globs are ever lost. See the root guide's Publish-Prep section.

Run from anywhere: python3 scripts/check_claude_md.py
Exit 0 when clean, 1 with findings listed.
"""
from __future__ import annotations

import re
import subprocess
import sys
from pathlib import Path, PurePosixPath

REPO_ROOT = Path(__file__).resolve().parent.parent

# Word budgets per guide (whitespace-delimited words; only the CLAUDE.md
# side is budgeted, twins are byte-identical).
BUDGETS = {
    "CLAUDE.md": 2200,
    "Nymeria/CLAUDE.md": 3175,
    "Nymeria/nymeria/CLAUDE.md": 450,
    "nymeria-desktop/CLAUDE.md": 600,
    "nymeria-mobile/CLAUDE.md": 300,
    "Nymeria/docs/private/security/CLAUDE.md": 1200,
    "Nymeria/tests/CLAUDE.md": 300,
}

# Path-resolution bases per guide (index-relative prefixes, "" = repo root),
# mirroring the path conventions each guide states in its own preamble.
GUIDE_BASES = {
    "CLAUDE.md": ["", "Nymeria/docs"],
    "Nymeria/CLAUDE.md": ["", "Nymeria", "Nymeria/nymeria", "Nymeria/docs"],
    "Nymeria/nymeria/CLAUDE.md": ["", "Nymeria", "Nymeria/nymeria", "Nymeria/docs"],
    "nymeria-desktop/CLAUDE.md": ["", "nymeria-desktop", "nymeria-desktop/src/lib"],
    "nymeria-mobile/CLAUDE.md": ["", "nymeria-mobile", "nymeria-mobile/src/lib"],
    "Nymeria/docs/private/security/CLAUDE.md": [
        "", "Nymeria", "Nymeria/nymeria", "Nymeria/docs",
        "Nymeria/docs/private/security",  # its refs are dir-relative
    ],
    "Nymeria/tests/CLAUDE.md": ["", "Nymeria", "Nymeria/tests", "Nymeria/docs"],
}

# Intentionally untracked or host-local paths that guides legitimately name.
SKIP_EXACT = {
    "shipped/NN",        # the shorthand placeholder itself
    "temp/latest/",      # CLIProxy operator deployment dir (host-local)
    "nymeria-browser/",  # separate gitignored checkout
    "Nymeria/build/",    # gitignored build artifact (named to warn about it)
    "origin/main",       # git ref, not a path
}
SKIP_PREFIXES = (
    "data/",             # runtime data dir contents
    "Nymeria/data/",
)

# Trailing segments with these suffixes are real file references and must
# never be rescued by the module.symbol -> module.py fallback.
KNOWN_EXTS = {
    ".md", ".py", ".ts", ".js", ".svelte", ".css", ".sh", ".yml", ".yaml",
    ".json", ".toml", ".kt", ".xml", ".txt", ".rs", ".cmd", ".ps1", ".html",
    ".sql", ".env", ".lock", ".cfg", ".ini", ".gradle",
}

FENCE = re.compile(r"^```.*?^```[ \t]*$", re.MULTILINE | re.DOTALL)
INLINE_CODE = re.compile(r"`([^`]+)`")
SHIPPED_SHORTHAND = re.compile(r"^(?:private/plans/)?shipped/(\d{2})$")
REJECT_CHARS = set("*{}<>|[]()$\"'`\\")
SEGMENT_OK = re.compile(r"^[A-Za-z0-9_.@+~=-]+$")
ENV_SHORTHAND = re.compile(r"^[A-Z0-9_]+$")
VERSIONISH = re.compile(r"^[0-9][0-9.]*$")


def git_index() -> tuple[set[str], set[str]]:
    """Tracked file paths and every ancestor directory of them."""
    try:
        out = subprocess.run(
            ["git", "ls-files", "-z"],
            cwd=REPO_ROOT, capture_output=True, check=True,
        ).stdout.decode("utf-8", errors="replace")
    except (subprocess.CalledProcessError, FileNotFoundError) as e:
        print(f"check_claude_md: cannot read the git index ({e})")
        sys.exit(1)
    files = {p for p in out.split("\0") if p}
    dirs: set[str] = set()
    for f in files:
        for parent in PurePosixPath(f).parents:
            if str(parent) != ".":
                dirs.add(str(parent))
    return files, dirs


def guide_paths(files: set[str]) -> list[str]:
    return sorted(
        p for p in files
        if PurePosixPath(p).name in ("CLAUDE.md", "AGENTS.md")
    )


def candidate(token: str) -> str | None:
    """Return the checkable path part of a token, or None to skip it."""
    token = token.strip().strip(",;")
    if not token or any(ch.isspace() for ch in token):
        return None
    token = token.split("::")[0]
    if ":" in token:  # single-colon symbol form, e.g. file.py:_helper
        token = token.split(":", 1)[0]
    if "/" not in token:
        return None
    if any(c in REJECT_CHARS for c in token):
        return None
    if token.startswith(("http", "/", "~", "-", "glob:", "#", "./", "../")):
        return None
    if token in SKIP_EXACT or token.startswith(SKIP_PREFIXES):
        return None
    segments = token.rstrip("/").split("/")
    if not all(SEGMENT_OK.match(s) for s in segments):
        return None
    first, last = segments[0], segments[-1]
    first_suffix = "." + first.rsplit(".", 1)[-1] if "." in first else ""
    if first_suffix and first_suffix not in KNOWN_EXTS:
        return None  # schemeless URL: github.com/..., pypi.nymeriaos.com/...
    if ENV_SHORTHAND.match(last) or VERSIONISH.match(last):
        return None  # DISCORD_/TELEGRAM_* shorthand, claude-cli/2.1.113
    return token


def extract_tokens(text: str) -> set[str]:
    """Path candidates from fenced command blocks and inline code spans."""
    tokens: set[str] = set()
    fences = FENCE.findall(text)
    prose = FENCE.sub("", text)
    for body in fences:
        for line in body.splitlines():
            if line.startswith("```"):
                continue
            for raw in line.split():
                tok = candidate(raw)
                if tok:
                    tokens.add(tok)
    for raw in INLINE_CODE.findall(prose):
        tok = candidate(raw)
        if tok:
            tokens.add(tok)
    return tokens


def resolves(token: str, bases: list[str], files: set[str], dirs: set[str]) -> bool:
    m = SHIPPED_SHORTHAND.match(token)
    if m:
        prefix = f"Nymeria/docs/private/plans/shipped/{m.group(1)}-"
        return any(f.startswith(prefix) for f in files)
    want_dir = token.endswith("/")
    clean = token.rstrip("/")
    for base in bases:
        full = f"{base}/{clean}" if base else clean
        if want_dir:
            if full in dirs:
                return True
        elif full in files or full in dirs:
            return True
    return False


def with_py_fallback(token: str) -> str | None:
    """`pkg/module.symbol` -> `pkg/module.py`, never for real extensions."""
    last = token.rsplit("/", 1)[-1]
    if "." not in last:
        return None
    suffix = "." + last.rsplit(".", 1)[-1]
    if suffix in KNOWN_EXTS:
        return None
    return re.sub(r"\.\w+$", ".py", token)


def main() -> int:
    findings: list[str] = []
    files, dirs = git_index()
    guides = guide_paths(files)

    by_dir: dict[str, set[str]] = {}
    for g in guides:
        p = PurePosixPath(g)
        by_dir.setdefault(str(p.parent), set()).add(p.name)

    for gdir, names in sorted(by_dir.items()):
        rel_dir = "" if gdir == "." else gdir + "/"
        if names != {"CLAUDE.md", "AGENTS.md"}:
            missing = ({"CLAUDE.md", "AGENTS.md"} - names).pop()
            findings.append(f"{rel_dir}{names.copy().pop()}: missing its {missing} twin")

    checked = 0
    for rel in guides:
        if PurePosixPath(rel).name != "CLAUDE.md":
            continue
        guide = REPO_ROOT / rel
        if not guide.exists():
            findings.append(f"{rel}: in the git index but missing from the worktree")
            continue
        checked += 1
        text = guide.read_text(encoding="utf-8", errors="replace")

        if not text.lstrip("\ufeff").startswith("# CLAUDE.md"):
            findings.append(
                f"{rel}: first line must start with '# CLAUDE.md' (publish-prep fail-safe)"
            )

        budget = BUDGETS.get(rel)
        if budget is None:
            findings.append(f"{rel}: no BUDGETS entry; classify new guides deliberately")
        else:
            words = len(text.split())
            if words > budget:
                findings.append(f"{rel}: {words} words exceeds its budget of {budget}")

        twin = guide.with_name("AGENTS.md")
        if twin.exists() and guide.read_bytes() != twin.read_bytes():
            findings.append(f"{rel}: not byte-identical to its AGENTS.md twin")

        bases = GUIDE_BASES.get(rel)
        if bases is None:
            findings.append(f"{rel}: no GUIDE_BASES entry; declare its path conventions")
            continue
        for tok in sorted(extract_tokens(text)):
            if resolves(tok, bases, files, dirs):
                continue
            variant = with_py_fallback(tok)
            if variant and resolves(variant, bases, files, dirs):
                continue
            findings.append(f"{rel}: dead path `{tok}`")

    if findings:
        print(f"check_claude_md: {len(findings)} finding(s)")
        for f in findings:
            print(f"  - {f}")
        return 1
    print(f"check_claude_md: OK ({checked} guides checked)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
