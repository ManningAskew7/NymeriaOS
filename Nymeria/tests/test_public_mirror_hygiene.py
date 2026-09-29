"""Shipped files cite none of the maintainer-only trees the mirror strips (#414).

Publish-prep rewrites this repo into the public NymeriaOS mirror, dropping every
path in ``scripts/publish_prep/paths_to_remove.txt``, the maintainer docs tree,
the review tree and the nested agent guides among them; the gitignored scratch
dir never ships at all. A shipped comment, docstring, message, or test that
cites one of those is a dead pointer there, and a test that READS one fails
every clone. The release skill's grep lane only checks the delta since the last
sync, so pre-existing citations never surfaced; this gate checks the whole tree.

Scope, deliberately narrow: the two docs trees, the scratch dir's ``keep``
subdir, and nested guides. NOT covered, and still the release grep lane's job:
citations of other stripped paths (the strip list also names ignore-pattern
data and files the publish-prep filename callback purges, which would drown a
derived rule in noise), gitignored ``tmp/`` plan files (backlog #351), and
``public/``-prefixed links.

On the mirror the strip list itself is absent (it is stripped too), so every
tracked file counts as shipped and the prefix check keeps guarding the public
tree; the guide check goes inert there, correctly, since no guide remains.
"""

from __future__ import annotations

import fnmatch
import re
import subprocess
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[2]
STRIP_LIST = REPO / "scripts" / "publish_prep" / "paths_to_remove.txt"

# Assembled from pieces so this file does not cite what it forbids.
FORBIDDEN = ("docs/" + "private/", "docs/" + "review/", "tmp/" + "keep/")

# A path ending in a guide filename. Only a citation that resolves to a guide
# the strip list removes counts ("hooks/CLAUDE.md" as a Claude Code concept
# does not): the release rule forbids pointing readers at nested guides too.
_GUIDE_CITATION = re.compile(r"(?<![\w/.-])((?:[\w.-]+/)+(?:CLAUDE|AGENTS)\.md)")

# Files that name those trees as DATA rather than as a pointer for a reader.
# Shrink-only: each entry must still need its exemption (see the liveness test).
ALLOWED = {
    # The guide-family gate: its inputs include the security guide, which lives
    # in the maintainer docs tree, and the shipped-note shorthand it resolves.
    "scripts/check_claude_md.py",
}

# The root guides: publish-prep's blob callback replaces their content with
# the public guide, so their private text never ships.
_SWAPPED_ROOT_GUIDES = {"CLAUDE.md", "AGENTS.md"}
# First lines that trigger that swap (the callback's own title markers).
_SWAP_TITLES = ("# CLAUDE.md", "# AGENTS.md", "# Repository Guidelines")


def strip_rules(text: str) -> list[str]:
    """git-filter-repo's ``get_paths_from_file`` line handling, exactly.

    Only ``\r\n`` is stripped and only a column-0 ``#`` is a comment, so a
    rule with stray whitespace matches nothing there and must match nothing
    here (normalising it would turn a shipped file into a false green).
    """
    return [
        line
        for line in (raw.rstrip("\r\n") for raw in text.splitlines())
        if line and not line.startswith("#")
    ]


def is_stripped(path: str, rules: list[str]) -> bool:
    """git-filter-repo ``--paths-from-file`` matching, as publish-prep uses it.

    A plain (or ``literal:``) line names a file or a directory (trailing slash
    optional) and matches it and everything under it; ``glob:`` lines match
    the whole path with fnmatch, where ``*`` also crosses ``/``; ``regex:``
    lines are searched in the path.
    """
    for rule in rules:
        if rule.startswith("glob:"):
            if fnmatch.fnmatchcase(path, rule[len("glob:"):]):
                return True
            continue
        if rule.startswith("regex:"):
            if re.search(rule[len("regex:"):], path):
                return True
            continue
        prefix = rule.removeprefix("literal:").rstrip("/")
        if path == prefix or path.startswith(prefix + "/"):
            return True
    return False


def _tracked_files() -> list[str]:
    try:
        out = subprocess.run(
            ["git", "ls-files", "-z"],
            cwd=REPO,
            capture_output=True,
            check=True,
            timeout=60,
        ).stdout
    except (OSError, subprocess.SubprocessError):
        pytest.skip("not a git checkout: nothing defines which files ship")
    return [name for name in out.decode("utf-8").split("\0") if name]


def _shipped_text(rel: str, rules: list[str]) -> str | None:
    if rel in ALLOWED or is_stripped(rel, rules):
        return None
    try:
        text = (REPO / rel).read_text(encoding="utf-8")
    except (UnicodeDecodeError, OSError):
        return None  # binary, or a submodule/symlink target not on disk
    if rel in _SWAPPED_ROOT_GUIDES:
        return None
    return text


def _stripped_guide(citation: str, tracked: set[str], rules: list[str]) -> bool:
    # Guide citations are written from the repo root or from the backend dir.
    return any(
        candidate in tracked and is_stripped(candidate, rules)
        for candidate in (citation, "Nymeria/" + citation)
    )


def test_shipped_files_cite_no_stripped_path() -> None:
    rules = strip_rules(STRIP_LIST.read_text(encoding="utf-8")) if STRIP_LIST.is_file() else []
    tracked = _tracked_files()
    tracked_set = set(tracked)
    offenders = []
    for rel in tracked:
        text = _shipped_text(rel, rules)
        if text is None:
            continue
        for number, line in enumerate(text.splitlines(), 1):
            if any(prefix in line for prefix in FORBIDDEN) or any(
                _stripped_guide(m, tracked_set, rules) for m in _GUIDE_CITATION.findall(line)
            ):
                offenders.append(f"{rel}:{number}: {line.strip()[:120]}")
    assert not offenders, (
        "Shipped files cite paths the public mirror strips. Name the concept or a "
        "non-path anchor (a backlog number, a shipped-note shorthand, a module) "
        "instead:\n" + "\n".join(offenders)
    )


# Built from pieces, like FORBIDDEN: written whole, these fixtures would be
# citations of real stripped guides and this file would fail its own gate.
_G = "CLAUDE" + ".md"


@pytest.mark.parametrize(
    ("line", "hits"),
    [
        (f"the backend guide (`Nymeria/{_G}`)", [f"Nymeria/{_G}"]),
        (f"see tests/{_G} first", [f"tests/{_G}"]),
        (f"skips hooks/{_G}", [f"hooks/{_G}"]),  # matched, then resolved
        (f"the root {_G}", []),
    ],
)
def test_guide_citations_are_found_then_resolved(line: str, hits: list[str]) -> None:
    assert _GUIDE_CITATION.findall(line) == hits
    tracked = {f"Nymeria/{_G}", f"Nymeria/tests/{_G}", _G}
    rules = ["glob:*/" + _G]
    resolved = [m for m in hits if _stripped_guide(m, tracked, rules)]
    assert resolved == [m for m in hits if m != f"hooks/{_G}"]


def test_only_the_root_guides_carry_a_swap_title() -> None:
    # The blob callback replaces ANY file whose first line carries one of these
    # titles with the public guide, so another shipped file titled that way
    # would silently lose its content on the mirror.
    rules = strip_rules(STRIP_LIST.read_text(encoding="utf-8")) if STRIP_LIST.is_file() else []
    swapped = []
    for rel in _tracked_files():
        if rel in _SWAPPED_ROOT_GUIDES or is_stripped(rel, rules):
            continue
        try:
            first = (REPO / rel).read_text(encoding="utf-8").lstrip().split("\n", 1)[0]
        except (UnicodeDecodeError, OSError):
            continue
        if first.startswith(_SWAP_TITLES):
            swapped.append(rel)
    assert not swapped, f"shipped files the mirror would overwrite with its guide: {swapped}"


def test_every_allowlisted_file_still_needs_its_exemption() -> None:
    for rel in ALLOWED:
        path = REPO / rel
        if not path.is_file():
            continue  # stripped on this tree
        text = path.read_text(encoding="utf-8")
        assert any(prefix in text for prefix in FORBIDDEN), (
            f"{rel} no longer cites a stripped path: drop it from ALLOWED"
        )


@pytest.mark.parametrize(
    ("path", "stripped"),
    [
        ("Nymeria/eval/runner.py", True),  # directory rule with a slash
        ("Nymeria/evaluation.py", False),  # a shared prefix is not a match
        ("Nymeria/nymeria/tools/_prv_b.py", True),  # file rule
        ("Nymeria/tests/" + "CLAUDE" + ".md", True),  # the glob crosses slashes
        ("CLAUDE" + ".md", False),  # the root guide is swapped, not stripped
        ("Nymeria/docs/configuration.md", False),
    ],
)
def test_the_strip_matcher_follows_filter_repo_semantics(path: str, stripped: bool) -> None:
    rules = ["Nymeria/eval/", "Nymeria/nymeria/tools/_prv_b.py", "glob:*/" + _G]
    assert is_stripped(path, rules) is stripped


def test_strip_lines_are_parsed_like_filter_repo() -> None:
    text = "# comment\n\nNymeria/eval/\r\n  # indented is a rule, not a comment\n Nymeria/x/\n"
    rules = strip_rules(text)
    assert rules == ["Nymeria/eval/", "  # indented is a rule, not a comment", " Nymeria/x/"]
    # A rule with stray whitespace matches nothing in filter-repo: the file ships.
    assert not is_stripped("Nymeria/x/a.py", rules)
    assert is_stripped("Nymeria/eval/a.py", rules)
    assert is_stripped("Nymeria/a/b.md", ["regex:^Nymeria/a/"])


def test_the_real_strip_list_removes_the_trees_this_gate_protects() -> None:
    # If publish-prep ever stopped stripping them, the gate's premise changes.
    if not STRIP_LIST.is_file():
        pytest.skip("the strip list is itself stripped on the mirror")
    rules = strip_rules(STRIP_LIST.read_text(encoding="utf-8"))
    assert is_stripped("Nymeria/" + FORBIDDEN[0] + "cliproxy.md", rules)
    assert is_stripped("Nymeria/" + FORBIDDEN[1] + "x.md", rules)
