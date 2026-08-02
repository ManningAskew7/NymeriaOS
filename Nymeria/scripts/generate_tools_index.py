#!/usr/bin/env python3
"""Walk Nymeria/nymeria/tools/*.py using AST and extract @tool-decorated functions."""

import ast
import sys
from pathlib import Path


def extract_tools(filepath: Path) -> list[tuple[str, str, str]]:
    """Return (name, relative_path, first_docstring_line) for each @tool function."""
    try:
        source = filepath.read_text()
        tree = ast.parse(source, filename=str(filepath))
    except (SyntaxError, UnicodeDecodeError) as e:
        print(f"WARNING: could not parse {filepath}: {e}", file=sys.stderr)
        return []

    tools = []
    for node in ast.walk(tree):
        if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        is_tool = any(
            (isinstance(d, ast.Name) and d.id == "tool")
            or (isinstance(d, ast.Call) and isinstance(d.func, ast.Name) and d.func.id == "tool")
            or (isinstance(d, ast.Attribute) and d.attr == "tool")
            or (isinstance(d, ast.Call) and isinstance(d.func, ast.Attribute) and d.func.attr == "tool")
            for d in node.decorator_list
        )
        if not is_tool:
            continue

        docstring = ast.get_docstring(node)
        desc = docstring.split("\n")[0].strip() if docstring else "(no description)"
        rel_path = filepath.relative_to(NYMERIA_ROOT)
        tools.append((node.name, str(rel_path), desc))

    return tools


NYMERIA_ROOT = Path(__file__).resolve().parent.parent
TOOLS_DIR = NYMERIA_ROOT / "nymeria" / "tools"

# Tool source files to exclude from the public index. The corresponding tool
# modules still exist in the codebase (kept for an eventual standalone rewrite),
# but they are not part of the publicly documented surface area. Mirrors the
# `_`-prefix convention used for private/internal tool modules; we use an explicit list here
# because `twitch.py` cannot be renamed without churning live runtime imports.
EXCLUDED_FILES = {"twitch.py"}


def main():
    all_tools = []
    for py_file in sorted(TOOLS_DIR.glob("*.py")):
        if py_file.name.startswith("_"):
            continue
        if py_file.name in EXCLUDED_FILES:
            continue
        all_tools.extend(extract_tools(py_file))

    all_tools.sort(key=lambda t: t[0].lower())

    print("# Tools Index")
    print()
    print("Auto-generated from `@tool`-decorated functions in `Nymeria/nymeria/tools/`.")
    print("Regenerate from `Nymeria/` with:")
    print("`python3 scripts/generate_tools_index.py > docs/agent-systems/tools-index.md`")
    print()
    print("This is an AST walk over statically defined tools, so it cannot list")
    print("dynamically constructed ones: the per-thread `Skill` meta-tool,")
    print("callable-thread and kit-template tools, custom HTTP/Python tools,")
    print("`mcp__*` tools, or workflow tools.")
    print()
    print(f"**{len(all_tools)} tools found.**")
    print()
    print("| Tool | File | Description |")
    print("|------|------|-------------|")
    for name, path, desc in all_tools:
        print(f"| `{name}` | `{path}` | {desc} |")


if __name__ == "__main__":
    main()
