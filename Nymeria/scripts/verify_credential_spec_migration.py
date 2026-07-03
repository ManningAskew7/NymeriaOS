#!/usr/bin/env python3
"""One-time verifier for the provider credential-spec migration (2026-07).

The migration moved inline provider/field literals out of ~440
``_credential_value(...)`` / ~169 ``_setup_hint(...)`` call sites in the
``nymeria/tools`` integration modules into per-module ``ProviderCredentialSpec``
declarations registered in ``nymeria/tools/credential_registry.py``.

This script machine-checks that the migration was behavior-preserving:

  extract   AST-parse the PRE-migration source (a git ref) and write the
            per-provider ground truth (aliases, every ordered field tuple,
            hint fields, env vars, display names, settings attrs) to JSON.

  compare   Import the migrated package, read the live registry, and diff it
            against the ground truth. Exit 1 on any mismatch.

Usage (from the ``Nymeria/`` directory):

  python3 scripts/verify_credential_spec_migration.py extract \
      --ref <pre-migration-sha> --out ../tmp/credential-spec-ground-truth.json
  python3 scripts/verify_credential_spec_migration.py compare \
      --truth ../tmp/credential-spec-ground-truth.json

It is NOT a CI gate: after the migration commit it remains here as the record
of what was verified. Registry invariants are permanently guarded by
``tests/test_credential_registry.py`` instead.
"""

from __future__ import annotations

import argparse
import ast
import itertools
import json
import subprocess
import sys
from collections import defaultdict
from pathlib import Path

TOOLS_DIR = "Nymeria/nymeria/tools"

# Meta-modules that legitimately pass dynamic provider/field arguments (they
# implement the credential machinery itself or take providers as tool args).
EXCLUDED_MODULES = {
    "__init__.py",
    "auth_manager.py",
    "credential_prompt.py",
    "credential_registry.py",
    "metadata.py",
    "native_credentials.py",
    "service_integration_base.py",
}

LOOKUP_CALLS = {"credential_value", "_credential_value", "get_native_credential_value"}
HINT_CALLS = {"setup_hint", "_setup_hint", "native_credential_setup_hint"}
RESOLVE_CALLS = {"resolve_native_credential"}
ALL_CALLS = LOOKUP_CALLS | HINT_CALLS | RESOLVE_CALLS

# resolve_native_credential's documented default field trio.
RESOLVE_DEFAULT_FIELDS = ("api_key", "token", "value")


class Unresolvable(Exception):
    pass


_MISSING = object()  # a wrapper parameter bound to a non-literal argument


class _Multi:
    """A local name that holds different literals on different branches."""

    __slots__ = ("values",)

    def __init__(self, values: tuple):
        # Deduplicate while preserving insertion order.
        seen: list = []
        for value in values:
            if value not in seen:
                seen.append(value)
        self.values = tuple(seen)


class _Visitor:
    """Per-module abstract evaluator.

    Integration modules hide provider literals behind two wrapper shapes: a
    generic config helper taking ``provider=...`` per call site
    (business/commerce/data_table style) and module-local shadows of
    ``_credential_value`` / ``_setup_hint`` that hardcode the provider inside
    (aws style). This evaluator starts at the module's call-graph roots
    (top-level code plus functions never called locally), binds literal
    arguments into wrapper parameters at each call site, and recurses so every
    target call is extracted with its true literals. Coverage is tracked per
    target-call line; an unreached target call is an error.
    """

    MAX_DEPTH = 8

    def __init__(self, module: str, tree: ast.Module, base_env: dict[str, object]):
        self.module = module
        self.errors: list[str] = []
        self._seen_errors: set[str] = set()
        self.facts: list[tuple[str, str, dict[str, object]]] = []  # (kind, provider, data)
        self.env_module = dict(base_env)
        self.env_module.update(_module_constants(tree))
        self.funcs: dict[str, ast.AST] = {
            node.name: node
            for node in tree.body
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
        }
        self.tree = tree
        self.called_names: set[str] = set()
        self.target_linenos: set[int] = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.Call):
                name = _call_name(node)
                if name:
                    self.called_names.add(name)
                    if name in ALL_CALLS and name not in self.funcs:
                        self.target_linenos.add(node.lineno)
        self.covered: set[int] = set()
        self.interesting = self._interesting_funcs()

    def _interesting_funcs(self) -> set[str]:
        has_target: dict[str, bool] = {}
        calls_local: dict[str, set[str]] = {}
        for name, func in self.funcs.items():
            targets = False
            local: set[str] = set()
            for node in ast.walk(func):
                if isinstance(node, ast.Call):
                    callee = _call_name(node)
                    if callee in ALL_CALLS and callee not in self.funcs:
                        targets = True
                    elif callee in self.funcs:
                        local.add(callee)
            has_target[name] = targets
            calls_local[name] = local
        interesting = {name for name, flag in has_target.items() if flag}
        changed = True
        while changed:
            changed = False
            for name, local in calls_local.items():
                if name not in interesting and local & interesting:
                    interesting.add(name)
                    changed = True
        return interesting

    def run(self) -> None:
        roots = [name for name in self.funcs if name not in self.called_names]
        top_level = [
            node
            for node in self.tree.body
            if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
        ]
        self._exec_block(top_level, {}, (), 0)
        for name in roots:
            self._exec_fn(self.funcs[name], {}, (name,), 0)
        for lineno in sorted(self.target_linenos - self.covered):
            self._error(
                f"{self.module}:{lineno}: target call never reached from any "
                "call-graph root (dead code or cross-module helper)"
            )

    def _error(self, message: str) -> None:
        if message not in self._seen_errors:
            self._seen_errors.add(message)
            self.errors.append(message)

    def _exec_fn(self, func: ast.AST, bound: dict, stack: tuple, depth: int) -> None:
        if depth > self.MAX_DEPTH:
            self._error(f"{self.module}: wrapper recursion exceeded {self.MAX_DEPTH}")
            return
        self._exec_block(func.body, dict(bound), stack, depth)

    def _exec_block(self, stmts: list, env: dict, stack: tuple, depth: int) -> None:
        for stmt in stmts:
            if isinstance(stmt, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
                continue
            if isinstance(stmt, ast.If):
                for call in self._shallow_calls(stmt.test):
                    self._process_call(call, env, stack, depth)
                body_env, else_env = dict(env), dict(env)
                self._exec_block(stmt.body, body_env, stack, depth)
                self._exec_block(stmt.orelse, else_env, stack, depth)
                for name in set(body_env) | set(else_env):
                    first = body_env.get(name, _MISSING)
                    second = else_env.get(name, _MISSING)
                    env[name] = first if self._same(first, second) else self._merge(first, second)
                continue
            for call in self._shallow_calls(stmt):
                self._process_call(call, env, stack, depth)
            if (
                isinstance(stmt, ast.Assign)
                and len(stmt.targets) == 1
                and isinstance(stmt.targets[0], ast.Name)
            ):
                env[stmt.targets[0].id] = self._try(stmt.value, env)
            elif (
                isinstance(stmt, ast.AnnAssign)
                and isinstance(stmt.target, ast.Name)
                and stmt.value is not None
            ):
                env[stmt.target.id] = self._try(stmt.value, env)
            elif isinstance(stmt, ast.AugAssign) and isinstance(stmt.target, ast.Name):
                env[stmt.target.id] = _MISSING
            elif isinstance(stmt, (ast.For, ast.AsyncFor)):
                if isinstance(stmt.target, ast.Name):
                    env[stmt.target.id] = _MISSING
                self._exec_block(stmt.body, env, stack, depth)
                self._exec_block(stmt.orelse, env, stack, depth)
            else:
                for _field, value in ast.iter_fields(stmt):
                    if isinstance(value, list) and value and isinstance(value[0], ast.stmt):
                        self._exec_block(value, env, stack, depth)
                    elif isinstance(value, list):
                        for item in value:
                            if isinstance(item, ast.excepthandler):
                                self._exec_block(item.body, env, stack, depth)

    @staticmethod
    def _same(first: object, second: object) -> bool:
        if isinstance(first, _Multi) or isinstance(second, _Multi):
            return (
                isinstance(first, _Multi)
                and isinstance(second, _Multi)
                and first.values == second.values
            )
        return first is second or first == second

    @staticmethod
    def _merge(first: object, second: object) -> object:
        if first is _MISSING or second is _MISSING:
            return _MISSING
        values: list = []
        for item in (first, second):
            values.extend(item.values if isinstance(item, _Multi) else (item,))
        return _Multi(tuple(values))

    def _shallow_calls(self, node: ast.AST):
        """Call nodes reachable without crossing a statement or lambda boundary."""
        for child in ast.iter_child_nodes(node):
            if isinstance(child, (ast.stmt, ast.Lambda)):
                continue
            yield from self._shallow_calls(child)
            if isinstance(child, ast.Call):
                yield child

    def _process_call(self, node: ast.Call, env: dict, stack: tuple, depth: int) -> None:
        name = _call_name(node)
        if name in self.funcs:
            if name in self.interesting and name not in stack:
                bound = self._bind_call(self.funcs[name], node, env)
                self._exec_fn(self.funcs[name], bound, stack + (name,), depth + 1)
        elif name in ALL_CALLS:
            self._extract_target(name, node, env)

    def _bind_call(self, func: ast.AST, call: ast.Call, env: dict) -> dict[str, object]:
        args = func.args
        names = [a.arg for a in args.args]
        bound: dict[str, object] = {}
        for pname, default in zip(names[len(names) - len(args.defaults):], args.defaults):
            bound[pname] = self._try(default, env)
        for arg, default in zip(args.kwonlyargs, args.kw_defaults):
            if default is not None:
                bound[arg.arg] = self._try(default, env)
        for index, value in enumerate(call.args):
            if isinstance(value, ast.Starred):
                continue
            if index < len(names):
                bound[names[index]] = self._try(value, env)
        for keyword in call.keywords:
            if keyword.arg is not None:
                bound[keyword.arg] = self._try(keyword.value, env)
        return bound

    def _try(self, node: ast.expr, env: dict) -> object:
        try:
            return _resolve(node, {**self.env_module, **env})
        except Unresolvable:
            return _MISSING

    def _extract_target(self, name: str, node: ast.Call, env: dict) -> None:
        where = f"{self.module}:{node.lineno}"
        if node.args:
            self._error(f"{where}: positional args on {name}")
            return
        kwargs = {kw.arg: kw.value for kw in node.keywords if kw.arg}
        merged = {**self.env_module, **env}

        def possibilities(key: str, default: object = None) -> list:
            if key not in kwargs:
                if default is not None:
                    return [default]
                raise Unresolvable(f"missing kwarg {key}")
            value = _resolve(kwargs[key], merged)
            if isinstance(value, _Multi):
                return list(value.values)
            return [value]

        try:
            if name in LOOKUP_CALLS:
                keys = {
                    "provider": possibilities("provider"),
                    "aliases": possibilities("provider_aliases", default=()),
                    "fields": possibilities("field_names"),
                }
                kind = "lookup"
            elif name in HINT_CALLS:
                keys = {
                    "provider": possibilities("provider"),
                    "fields": possibilities("field_names"),
                    "env_var": possibilities("env_var", default=""),
                    "display_name": possibilities("display_name", default=""),
                }
                kind = "hint"
            else:
                keys = {
                    "provider": possibilities("provider"),
                    "aliases": possibilities("aliases", default=()),
                    "fields": possibilities("field_names", default=RESOLVE_DEFAULT_FIELDS),
                    "settings_attr": possibilities("settings_attr", default=""),
                    "env_vars": possibilities("env_vars", default=()),
                }
                kind = "resolve"
        except Unresolvable as exc:
            self._error(f"{where}: {name}: {exc}")
            return

        combos = 1
        for values in keys.values():
            combos *= len(values)
        if combos > 16:
            self._error(f"{where}: {name}: too many literal combinations ({combos})")
            return

        names_order = list(keys)
        for combo in itertools.product(*(keys[k] for k in names_order)):
            data = dict(zip(names_order, combo))
            provider = data.pop("provider")
            if not isinstance(provider, str):
                self._error(f"{where}: {name}: provider is not a string literal")
                return
            for tuple_key in ("aliases", "fields", "env_vars"):
                if tuple_key in data:
                    data[tuple_key] = tuple(data[tuple_key])
            for str_key in ("env_var", "display_name", "settings_attr"):
                if str_key in data:
                    data[str_key] = str(data[str_key])
            self.facts.append((kind, provider, data))
        self.covered.add(node.lineno)


def _git_show(repo_root: Path, ref: str, path: str) -> str:
    result = subprocess.run(
        ["git", "-C", str(repo_root), "show", f"{ref}:{path}"],
        capture_output=True,
        text=True,
        check=True,
    )
    return result.stdout


def _git_ls_tools(repo_root: Path, ref: str) -> list[str]:
    result = subprocess.run(
        ["git", "-C", str(repo_root), "ls-tree", "--name-only", ref, f"{TOOLS_DIR}/"],
        capture_output=True,
        text=True,
        check=True,
    )
    return [
        line.strip()
        for line in result.stdout.splitlines()
        if line.strip().endswith(".py")
    ]


def _module_constants(tree: ast.Module) -> dict[str, object]:
    """Module-level NAME = <literal> assignments (strings and tuples)."""
    env: dict[str, object] = {}
    for node in tree.body:
        if isinstance(node, ast.Assign) and len(node.targets) == 1:
            target = node.targets[0]
            if isinstance(target, ast.Name):
                try:
                    value = ast.literal_eval(node.value)
                except (ValueError, SyntaxError):
                    continue
                if isinstance(value, (str, tuple, list)):
                    env[target.id] = tuple(value) if isinstance(value, list) else value
    return env


def _resolve(node: ast.expr, env: dict[str, object]) -> object:
    """Resolve an expression to a literal (or a _Multi of possible literals)."""
    if isinstance(node, ast.Constant):
        return node.value
    if isinstance(node, (ast.Tuple, ast.List)):
        elements = [_resolve(elt, env) for elt in node.elts]
        options = [
            list(element.values) if isinstance(element, _Multi) else [element]
            for element in elements
        ]
        total = 1
        for values in options:
            total *= len(values)
        if total > 16:
            raise Unresolvable(f"too many literal combinations in composite ({total})")
        combos = [tuple(combo) for combo in itertools.product(*options)]
        return combos[0] if len(combos) == 1 else _Multi(tuple(combos))
    if isinstance(node, ast.IfExp):
        body = _resolve(node.body, env)
        orelse = _resolve(node.orelse, env)
        if _Visitor._same(body, orelse):
            return body
        return _Visitor._merge(body, orelse)
    if isinstance(node, ast.Name):
        if node.id in env:
            value = env[node.id]
            if value is _MISSING:
                raise Unresolvable(f"local {node.id} bound to a non-literal value")
            return value
        raise Unresolvable(f"name {node.id}")
    if isinstance(node, ast.BinOp) and isinstance(node.op, ast.Add):
        left = _resolve(node.left, env)
        right = _resolve(node.right, env)
        if isinstance(left, tuple) and isinstance(right, tuple):
            return left + right
        raise Unresolvable("non-tuple concatenation")
    raise Unresolvable(f"node {type(node).__name__}")


def _call_name(node: ast.Call) -> str | None:
    if isinstance(node.func, ast.Name):
        return node.func.id
    if isinstance(node.func, ast.Attribute):
        return node.func.attr
    return None


def extract_ground_truth(repo_root: Path, ref: str) -> tuple[dict, list[str]]:
    """Return (per-provider facts, errors) extracted from the tools at ref."""
    errors: list[str] = []
    providers: dict[str, dict] = defaultdict(
        lambda: {
            "aliases": set(),
            "lookup_tuples": set(),
            "hint_fields": set(),
            "env_vars_hint": set(),
            "display_names": set(),
            "settings_attrs": set(),
            "resolve_env_vars": set(),
            "modules": set(),
        }
    )

    base_env: dict[str, object] = {}
    try:
        base_src = _git_show(repo_root, ref, f"{TOOLS_DIR}/service_integration_base.py")
        base_env = _module_constants(ast.parse(base_src))
    except subprocess.CalledProcessError:
        pass

    for path in _git_ls_tools(repo_root, ref):
        module = path.rsplit("/", 1)[-1]
        if module in EXCLUDED_MODULES:
            continue
        source = _git_show(repo_root, ref, path)
        visitor = _Visitor(module, ast.parse(source), base_env)
        visitor.run()
        errors.extend(visitor.errors)
        for kind, provider, data in visitor.facts:
            entry = providers[provider]
            entry["modules"].add(module)
            if kind == "lookup":
                entry["aliases"] |= set(data["aliases"])
                entry["lookup_tuples"].add(data["fields"])
            elif kind == "hint":
                entry["hint_fields"].add(data["fields"])
                entry["env_vars_hint"].add(data["env_var"])
                entry["display_names"].add(data["display_name"])
            else:
                entry["aliases"] |= set(data["aliases"])
                entry["lookup_tuples"].add(data["fields"])
                if data["settings_attr"]:
                    entry["settings_attrs"].add(data["settings_attr"])
                if data["env_vars"]:
                    entry["resolve_env_vars"].add(data["env_vars"])

    serializable = {
        provider: {
            "aliases": sorted(entry["aliases"]),
            "lookup_tuples": sorted(list(t) for t in entry["lookup_tuples"]),
            "hint_fields": sorted(list(t) for t in entry["hint_fields"]),
            "env_vars_hint": sorted(v for v in entry["env_vars_hint"] if v),
            "display_names": sorted(v for v in entry["display_names"] if v),
            "settings_attrs": sorted(entry["settings_attrs"]),
            "resolve_env_vars": sorted(list(t) for t in entry["resolve_env_vars"]),
            "modules": sorted(entry["modules"]),
        }
        for provider, entry in sorted(providers.items())
    }
    return serializable, errors


def compare_with_registry(truth: dict) -> list[str]:
    """Diff the live registry against the extracted ground truth.

    The caller must have imported the module(s) whose specs should be present
    (importing ``nymeria.tools`` loads everything; importing a single
    integration module registers just its specs for a partial check).
    """
    from nymeria.tools import credential_registry as reg

    problems: list[str] = []
    seen_canonicals: set[str] = set()

    for provider, facts in truth.items():
        spec = reg.get_provider_spec(provider)
        if spec is None:
            problems.append(f"{provider}: no registered spec")
            continue
        seen_canonicals.add(spec.provider)
        if spec.provider != provider:
            problems.append(
                f"{provider}: canonical key changed to {spec.provider} (must stay verbatim)"
            )
        if set(spec.aliases) != set(facts["aliases"]):
            problems.append(
                f"{provider}: aliases {sorted(spec.aliases)} != truth {facts['aliases']}"
            )
        spec_tuples = {tuple(grp.names) for grp in spec.groups}
        truth_tuples = {tuple(t) for t in facts["lookup_tuples"]}
        missing = truth_tuples - spec_tuples
        invented = spec_tuples - truth_tuples
        if missing:
            problems.append(f"{provider}: lookup tuples missing from spec: {sorted(missing)}")
        if invented:
            problems.append(f"{provider}: spec has tuples not in truth: {sorted(invented)}")
        truth_hints = {tuple(t) for t in facts["hint_fields"]}
        if truth_hints:
            hint = tuple(spec.hint_fields)
            union = {name for variant in truth_hints for name in variant}
            # Multi-variant providers (two token kinds etc.): the spec may pick
            # one variant verbatim or cover the union (order uncheckable then).
            if hint not in truth_hints and set(hint) != union:
                problems.append(
                    f"{provider}: hint_fields {hint} matches no truth variant "
                    f"{sorted(truth_hints)} nor their union"
                )
        for key, spec_value in (
            ("env_vars_hint", spec.env_var),
            ("display_names", spec.display_name),
        ):
            values = facts[key]
            if values and spec_value not in values:
                problems.append(f"{provider}: {key} {spec_value!r} not in truth {values}")
            elif not values and spec_value:
                problems.append(f"{provider}: spec sets {key} {spec_value!r}, truth has none")
        if facts["settings_attrs"]:
            if len(facts["settings_attrs"]) > 1:
                problems.append(
                    f"{provider}: ambiguous settings_attrs: {facts['settings_attrs']}"
                )
            elif spec.settings_attr != facts["settings_attrs"][0]:
                problems.append(
                    f"{provider}: settings_attr {spec.settings_attr!r} != "
                    f"truth {facts['settings_attrs'][0]!r}"
                )
        if facts["resolve_env_vars"]:
            truth_env = {tuple(t) for t in facts["resolve_env_vars"]}
            if len(truth_env) > 1:
                problems.append(f"{provider}: ambiguous env_vars: {sorted(truth_env)}")
            elif tuple(spec.env_vars) not in truth_env:
                problems.append(
                    f"{provider}: env_vars {spec.env_vars} != truth {sorted(truth_env)}"
                )

    for spec in reg.iter_provider_specs():
        if spec.provider not in seen_canonicals and spec.provider not in truth:
            problems.append(f"{spec.provider}: registered spec absent from ground truth")
    return problems


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="mode", required=True)
    p_extract = sub.add_parser("extract")
    p_extract.add_argument("--ref", default="HEAD")
    p_extract.add_argument("--out", required=True)
    p_compare = sub.add_parser("compare")
    p_compare.add_argument("--truth", required=True)
    args = parser.parse_args()

    repo_root = Path(__file__).resolve().parents[2]

    if args.mode == "extract":
        truth, errors = extract_ground_truth(repo_root, args.ref)
        if errors:
            print(f"EXTRACTION ERRORS ({len(errors)}):")
            for line in errors:
                print(f"  {line}")
            return 1
        out = Path(args.out)
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(json.dumps({"ref": args.ref, "providers": truth}, indent=1))
        modules = {m for facts in truth.values() for m in facts["modules"]}
        print(
            f"Extracted {len(truth)} providers across {len(modules)} modules "
            f"from {args.ref} -> {out}"
        )
        return 0

    truth = json.loads(Path(args.truth).read_text())["providers"]
    import nymeria.tools  # noqa: F401  (imports every module -> registers all specs)

    problems = compare_with_registry(truth)
    if problems:
        print(f"MIGRATION DIFFS ({len(problems)}):")
        for line in problems:
            print(f"  {line}")
        return 1
    print(f"Zero diff: registry matches ground truth for {len(truth)} providers.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
