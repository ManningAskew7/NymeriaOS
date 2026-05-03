"""Static import hygiene checks for modules with known local import drift."""

from __future__ import annotations

import ast
from pathlib import Path


NYMERIA_ROOT = Path(__file__).resolve().parents[1]


def _nested_imports(path: Path, module_names: set[str]) -> list[tuple[int, str]]:
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    violations: list[tuple[int, str]] = []

    def visit(node: ast.AST, *, nested: bool = False) -> None:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            nested = True

        if nested and isinstance(node, ast.Import):
            for alias in node.names:
                imported = alias.name.split(".", 1)[0]
                if imported in module_names:
                    violations.append((node.lineno, imported))

        if nested and isinstance(node, ast.ImportFrom) and node.module:
            imported = node.module.split(".", 1)[0]
            if imported in module_names:
                violations.append((node.lineno, imported))

        for child in ast.iter_child_nodes(node):
            visit(child, nested=nested)

    visit(tree)
    return violations


def test_hot_path_stdlib_imports_are_module_level():
    targets = {
        NYMERIA_ROOT / "nymeria/core/agent.py": {"time"},
        NYMERIA_ROOT / "nymeria/vendor/react_agent/providers.py": {"os"},
        NYMERIA_ROOT / "nymeria/vendor/react_agent/graph.py": {"asyncio"},
    }

    violations = {
        str(path.relative_to(NYMERIA_ROOT)): nested
        for path, modules in targets.items()
        if (nested := _nested_imports(path, modules))
    }

    assert violations == {}
