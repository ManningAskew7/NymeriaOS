"""General-purpose utility integration tools.

These cover small read-only utility surfaces that map cleanly to
LangChain/community utilities, plus a local safe calculator.
"""

from __future__ import annotations

import ast
import json
import logging
import math
import operator
from typing import Annotated, Any, Optional

from langchain_core.runnables import RunnableConfig
from langchain_core.tools import InjectedToolArg, tool

logger = logging.getLogger(__name__)

_MAX_EXPRESSION_CHARS = 500
_MAX_POWER_ABS_EXPONENT = 100
_MAX_POWER_ABS_BASE = 1_000_000

_BINARY_OPERATORS = {
    ast.Add: operator.add,
    ast.Sub: operator.sub,
    ast.Mult: operator.mul,
    ast.Div: operator.truediv,
    ast.FloorDiv: operator.floordiv,
    ast.Mod: operator.mod,
    ast.Pow: operator.pow,
}

_UNARY_OPERATORS = {
    ast.UAdd: operator.pos,
    ast.USub: operator.neg,
}

_SAFE_FUNCTIONS = {
    "abs": abs,
    "acos": math.acos,
    "asin": math.asin,
    "atan": math.atan,
    "ceil": math.ceil,
    "cos": math.cos,
    "degrees": math.degrees,
    "exp": math.exp,
    "floor": math.floor,
    "log": math.log,
    "log10": math.log10,
    "max": max,
    "min": min,
    "radians": math.radians,
    "round": round,
    "sin": math.sin,
    "sqrt": math.sqrt,
    "tan": math.tan,
}

_SAFE_CONSTANTS = {
    "e": math.e,
    "pi": math.pi,
    "tau": math.tau,
}


def _format_value(value: Any) -> str:
    if isinstance(value, float) and value.is_integer():
        return str(int(value))
    return str(value)


def _evaluate_ast(node: ast.AST) -> int | float:
    if isinstance(node, ast.Expression):
        return _evaluate_ast(node.body)

    if isinstance(node, ast.Constant):
        if isinstance(node.value, bool) or not isinstance(node.value, (int, float)):
            raise ValueError("only numeric constants are allowed")
        return node.value

    if isinstance(node, ast.Name):
        if node.id in _SAFE_CONSTANTS:
            return _SAFE_CONSTANTS[node.id]
        raise ValueError(f"unknown name '{node.id}'")

    if isinstance(node, ast.UnaryOp):
        op = _UNARY_OPERATORS.get(type(node.op))
        if op is None:
            raise ValueError("unsupported unary operator")
        return op(_evaluate_ast(node.operand))

    if isinstance(node, ast.BinOp):
        op = _BINARY_OPERATORS.get(type(node.op))
        if op is None:
            raise ValueError("unsupported binary operator")
        left = _evaluate_ast(node.left)
        right = _evaluate_ast(node.right)
        if isinstance(node.op, ast.Pow):
            if abs(left) > _MAX_POWER_ABS_BASE or abs(right) > _MAX_POWER_ABS_EXPONENT:
                raise ValueError("power expression is too large")
        return op(left, right)

    if isinstance(node, ast.Call):
        if not isinstance(node.func, ast.Name):
            raise ValueError("only simple math function calls are allowed")
        func = _SAFE_FUNCTIONS.get(node.func.id)
        if func is None:
            raise ValueError(f"function '{node.func.id}' is not allowed")
        if node.keywords:
            raise ValueError("keyword arguments are not allowed")
        args = [_evaluate_ast(arg) for arg in node.args]
        return func(*args)

    raise ValueError(f"unsupported expression element: {type(node).__name__}")


def _split_csv(value: str) -> list[str]:
    return [part.strip() for part in value.split(",") if part.strip()]


def _require_langchain_community(tool_name: str, extra: str = "") -> Optional[str]:
    try:
        import langchain_community  # noqa: F401
    except ImportError:
        packages = "langchain-community"
        if extra:
            packages += f" and {extra}"
        return f"[Error]: {tool_name} requires {packages}. Install Nymeria requirements and restart the backend."
    return None


@tool
def calculator(expression: str) -> str:
    """Evaluate a safe arithmetic expression.

    Use this for deterministic arithmetic, unit conversions expressed as math,
    and quick calculations. Supported operators are +, -, *, /, //, %, **, and
    parentheses. Supported functions include sqrt, sin, cos, tan, log, round,
    min, and max. Constants: pi, e, tau.

    Args:
        expression: Arithmetic expression to evaluate.
    """
    expression = expression.strip()
    if not expression:
        return "[Error]: expression is required."
    if len(expression) > _MAX_EXPRESSION_CHARS:
        return f"[Error]: expression is too long; max {_MAX_EXPRESSION_CHARS} characters."

    try:
        parsed = ast.parse(expression, mode="eval")
        result = _evaluate_ast(parsed)
    except ZeroDivisionError:
        return "[Error]: division by zero."
    except Exception as e:
        logger.debug("calculator failed for expression %r: %s", expression, e)
        return f"[Error]: Invalid calculation: {e}"

    return _format_value(result)


@tool
def wikipedia_search(
    query: str,
    top_k_results: int = 3,
    language: str = "en",
    max_chars: int = 4000,
) -> str:
    """Search Wikipedia and return article summaries.

    Args:
        query: Search query.
        top_k_results: Number of Wikipedia results to include, 1-10.
        language: Wikipedia language code, e.g. "en".
        max_chars: Maximum characters per fetched document, 500-12000.
    """
    query = query.strip()
    if not query:
        return "[Error]: query is required."

    missing = _require_langchain_community("wikipedia_search", "wikipedia")
    if missing:
        return missing

    top_k_results = max(1, min(10, int(top_k_results)))
    max_chars = max(500, min(12000, int(max_chars)))
    language = (language or "en").strip() or "en"

    try:
        from langchain_community.tools.wikipedia.tool import WikipediaQueryRun
        from langchain_community.utilities.wikipedia import WikipediaAPIWrapper

        wrapper = WikipediaAPIWrapper(
            top_k_results=top_k_results,
            lang=language,
            doc_content_chars_max=max_chars,
        )
        wiki_tool = WikipediaQueryRun(api_wrapper=wrapper)
        return str(wiki_tool.invoke(query))
    except Exception as e:
        logger.error("wikipedia_search failed", exc_info=True)
        return f"[Error]: Wikipedia search failed: {e}"


@tool
def wolfram_alpha_query(
    query: str,
    config: Annotated[RunnableConfig, InjectedToolArg] = None,
) -> str:
    """Query Wolfram|Alpha for computational facts and calculations.

    Args:
        query: Natural language or mathematical query for Wolfram|Alpha.
    """
    query = query.strip()
    if not query:
        return "[Error]: query is required."

    from ..config import get_settings
    from .native_credentials import get_native_credential_value, native_credential_setup_hint

    app_id_credential = get_native_credential_value(
        provider="wolfram_alpha",
        provider_aliases=("wolfram", "wolframalpha"),
        field_names=("app_id", "appid", "value"),
        tool_name="wolfram_alpha_query",
        config=config,
    )
    app_id = app_id_credential.value if app_id_credential else get_settings().wolfram_alpha_app_id
    if not app_id:
        return native_credential_setup_hint(
            provider="wolfram_alpha",
            field_names=("app_id", "value"),
            tool_name="wolfram_alpha_query",
            env_var="WOLFRAM_ALPHA_APP_ID",
            display_name="Wolfram|Alpha",
        )

    missing = _require_langchain_community("wolfram_alpha_query", "wolframalpha")
    if missing:
        return missing

    try:
        from langchain_community.tools.wolfram_alpha.tool import WolframAlphaQueryRun
        from langchain_community.utilities.wolfram_alpha import WolframAlphaAPIWrapper

        wrapper = WolframAlphaAPIWrapper(wolfram_alpha_appid=app_id)
        wolfram_tool = WolframAlphaQueryRun(api_wrapper=wrapper)
        return str(wolfram_tool.invoke(query))
    except Exception as e:
        logger.error("wolfram_alpha_query failed", exc_info=True)
        return f"[Error]: Wolfram|Alpha query failed: {e}"


@tool
def searxng_search(
    query: str,
    num_results: int = 10,
    page_number: int = 1,
    language: str = "en",
    safesearch: int = 0,
    categories: str = "",
    engines: str = "",
    config: Annotated[RunnableConfig, InjectedToolArg] = None,
) -> str:
    """Search a configured SearXNG instance and return JSON results.

    Args:
        query: Search query.
        num_results: Number of results to return, 1-20.
        page_number: Search page number, 1 or greater.
        language: SearXNG language code, e.g. "en".
        safesearch: Safe-search level: 0 none, 1 moderate, 2 strict.
        categories: Optional comma-separated SearXNG categories.
        engines: Optional comma-separated SearXNG engines.
    """
    query = query.strip()
    if not query:
        return "[Error]: query is required."

    from ..config import get_settings
    from .native_credentials import get_native_credential_value, native_credential_setup_hint

    base_url_credential = get_native_credential_value(
        provider="searxng",
        provider_aliases=("searx", "searx_ng"),
        field_names=("base_url", "url", "value"),
        tool_name="searxng_search",
        config=config,
    )
    searxng_base_url = base_url_credential.value if base_url_credential else get_settings().searxng_base_url
    if not searxng_base_url:
        return native_credential_setup_hint(
            provider="searxng",
            field_names=("base_url", "value"),
            tool_name="searxng_search",
            env_var="SEARXNG_BASE_URL",
            display_name="SearXNG",
        )

    missing = _require_langchain_community("searxng_search")
    if missing:
        return missing

    num_results = max(1, min(20, int(num_results)))
    page_number = max(1, int(page_number))
    safesearch = max(0, min(2, int(safesearch)))
    language = (language or "en").strip() or "en"

    try:
        from langchain_community.utilities.searx_search import SearxSearchWrapper

        wrapper = SearxSearchWrapper(
            searx_host=searxng_base_url,
            headers={"Accept": "application/json"},
            params={
                "language": language,
                "safesearch": safesearch,
            },
        )
        results = wrapper.results(
            query,
            num_results=num_results,
            categories=_split_csv(categories),
            engines=_split_csv(engines),
            pageno=page_number,
        )
        return json.dumps(results, indent=2, ensure_ascii=False)
    except Exception as e:
        logger.error("searxng_search failed", exc_info=True)
        return f"[Error]: SearXNG search failed: {e}"


UTILITY_INTEGRATION_TOOLS = [
    calculator,
    wikipedia_search,
    wolfram_alpha_query,
    searxng_search,
]
