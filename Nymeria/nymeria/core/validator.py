"""Code validation utilities for Nymeria self-modification system."""

import ast
import logging
import subprocess
import sys
from pathlib import Path
from typing import Tuple, List, Optional

from ..exec_sandbox import SandboxError, shim_refusal
from ..oom import oom_score_preexec
from ..subprocess_env import scrubbed_subprocess_env
from .exec_policy import sandbox_argv_launch

logger = logging.getLogger(__name__)


class CodeValidator:
    """
    Validates Python code before allowing self-modifications.

    Performs syntax checking, tool definition validation, and
    integration testing to ensure modifications don't break Nymeria.
    """

    def __init__(self, project_root: Path):
        """
        Initialize the code validator.

        Args:
            project_root: Root directory of the Nymeria project
        """
        self.project_root = project_root
        self.tools_dir = project_root / "nymeria" / "tools"

    def validate_python_syntax(self, code: str) -> Tuple[bool, str]:
        """
        Check Python code for syntax errors using AST.

        Args:
            code: Python source code to validate

        Returns:
            Tuple of (is_valid, message)
        """
        try:
            ast.parse(code)
            return True, "Syntax is valid"
        except SyntaxError as e:
            return False, f"Syntax error at line {e.lineno}: {e.msg}"
        except Exception as e:
            return False, f"Validation error: {str(e)}"

    def validate_file_syntax(self, file_path: Path) -> Tuple[bool, str]:
        """
        Check a Python file for syntax errors.

        Args:
            file_path: Path to the Python file

        Returns:
            Tuple of (is_valid, message)
        """
        try:
            with open(file_path, "r", encoding="utf-8") as f:
                code = f.read()
            return self.validate_python_syntax(code)
        except FileNotFoundError:
            return False, f"File not found: {file_path}"
        except Exception as e:
            return False, f"Failed to read file: {str(e)}"

    def validate_tool_definition(self, code: str) -> Tuple[bool, str]:
        """
        Verify code contains a valid @tool decorated function.

        Args:
            code: Python source code to validate

        Returns:
            Tuple of (is_valid, message with tool name if found)
        """
        try:
            tree = ast.parse(code)
        except SyntaxError as e:
            return False, f"Syntax error: {e.msg}"

        tool_functions = []

        for node in ast.walk(tree):
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                for decorator in node.decorator_list:
                    # Check for @tool decorator
                    is_tool = False
                    if isinstance(decorator, ast.Name) and decorator.id == "tool":
                        is_tool = True
                    elif isinstance(decorator, ast.Call):
                        if isinstance(decorator.func, ast.Name) and decorator.func.id == "tool":
                            is_tool = True

                    if is_tool:
                        tool_functions.append(node.name)

        if not tool_functions:
            return False, "No @tool decorated function found"

        if len(tool_functions) == 1:
            return True, f"Found tool: {tool_functions[0]}"
        else:
            return True, f"Found {len(tool_functions)} tools: {', '.join(tool_functions)}"

    def check_imports(self, code: str) -> Tuple[bool, List[str]]:
        """
        Extract and validate import statements from code.

        Args:
            code: Python source code

        Returns:
            Tuple of (has_required_imports, list of imports)
        """
        try:
            tree = ast.parse(code)
        except SyntaxError:
            return False, []

        imports = []
        has_tool_import = False

        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                for alias in node.names:
                    imports.append(alias.name)
            elif isinstance(node, ast.ImportFrom):
                module = node.module or ""
                for alias in node.names:
                    imports.append(f"{module}.{alias.name}")
                    if "tool" in alias.name.lower() or "langchain" in module.lower():
                        has_tool_import = True

        return has_tool_import, imports

    def test_tool_import(self) -> Tuple[bool, str]:
        """
        Test that all tools can be imported successfully.

        This is the quick validation test run after modifications.

        Returns:
            Tuple of (success, output message)
        """
        test_code = "from nymeria.tools import SEED_TOOLS; print(f'Loaded {len(SEED_TOOLS)} tools')"

        # This LOOKS like Nymeria importing itself, and its exemption marker
        # used to say so. It is not. `nymeria/tools/` is absent from
        # `NYMERIA_PROTECTED_DIRS` (tools/filesystem.py), so `file_write` and
        # `file_edit` can rewrite any module `nymeria/tools/__init__.py` pulls
        # in, and `self_test_import` carries no `self_edit_allowed()` gate, so
        # the setting that bounds the self-edit tools does not bound this. The
        # import below therefore executes module-level code an agent authored,
        # which is the category C1-02 ranked highest, not a fixed self-check.
        spawn_kwargs = dict(
            cwd=str(self.project_root),
            capture_output=True,
            text=True,
            timeout=30,
            preexec_fn=oom_score_preexec(),
            # The child only imports a module and counts tools, so it needs
            # no secret from this process. The two extras are path
            # resolution, not credentials: NYMERIA_PROJECT_ROOT is how the
            # package finds its root when launched outside the source tree,
            # and PYTHONPATH keeps a non-standard install layout importable.
            env=scrubbed_subprocess_env(("NYMERIA_PROJECT_ROOT", "PYTHONPATH")),
        )
        try:
            launch = sandbox_argv_launch([sys.executable, "-c", test_code], spawn_kwargs)
        except SandboxError as exc:
            # Caught separately, and BEFORE the generic arm below, because the
            # old marker's objection to confining this was right about the
            # symptom even though it was wrong about the category: a policy
            # that cannot be built must not report as a broken modification.
            # It gets its own message instead, so an operator reads "the
            # sandbox refused" rather than "your edit broke the tools".
            return False, f"Import test could not be confined, so it was not run: {exc}"

        try:
            result = subprocess.run(launch, **spawn_kwargs)

            if result.returncode == 0:
                return True, result.stdout.strip()
            # The `except SandboxError` arm above covers a policy that could not
            # be BUILT. The shim also fails closed in the CHILD, after the fork,
            # where no exception can reach back: that arrives as a plain nonzero
            # exit and would otherwise be reported as `Import failed`, which is
            # the same "your edit broke the tools" misreading through a
            # different door. Measured, with the policy deliberately withheld:
            # `Import failed: exec_sandbox shim: no policy set`.
            refusal = shim_refusal(result.returncode, result.stderr)
            if refusal:
                return False, f"Import test could not be confined, so it was not run: {refusal}"
            return False, f"Import failed:\n{result.stderr}"

        except subprocess.TimeoutExpired:
            return False, "Import test timed out (30s)"
        except Exception as e:
            return False, f"Import test error: {str(e)}"

    def validate_path_allowed(self, file_path: Path, allowed_paths: List[Path]) -> Tuple[bool, str]:
        """
        Check if a file path is within allowed directories.

        Args:
            file_path: Path to check
            allowed_paths: List of allowed directories

        Returns:
            Tuple of (is_allowed, message)
        """
        file_path = Path(file_path).resolve()

        for allowed in allowed_paths:
            allowed = Path(allowed).resolve()
            try:
                file_path.relative_to(allowed)
                return True, f"Path is within allowed directory: {allowed}"
            except ValueError:
                continue

        return False, f"Path {file_path} is not in any allowed directory"

    def validate_modification(
        self,
        file_path: Path,
        new_content: str,
        allowed_paths: Optional[List[Path]] = None
    ) -> Tuple[bool, str]:
        """
        Perform full validation of a proposed file modification.

        Args:
            file_path: Path to the file being modified
            new_content: Proposed new content
            allowed_paths: List of allowed directories (defaults to tools/)

        Returns:
            Tuple of (is_valid, detailed message)
        """
        file_path = Path(file_path).resolve()

        if allowed_paths is None:
            allowed_paths = [self.tools_dir]

        # Step 1: Check path is allowed
        path_valid, path_msg = self.validate_path_allowed(file_path, allowed_paths)
        if not path_valid:
            return False, f"Path not allowed: {path_msg}"

        # Step 2: Check syntax
        syntax_valid, syntax_msg = self.validate_python_syntax(new_content)
        if not syntax_valid:
            return False, f"Syntax invalid: {syntax_msg}"

        # Step 3: If it's a tool file, check for @tool decorator
        if file_path.suffix == ".py" and "tools" in str(file_path):
            tool_valid, tool_msg = self.validate_tool_definition(new_content)
            if not tool_valid:
                return False, f"Tool validation failed: {tool_msg}"

        return True, "Validation passed"
