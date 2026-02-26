"""
A simple test tool to verify the self-modification pipeline works end-to-end.
"""

from langchain_core.tools import tool


@tool
def hello_test(name: str = "world") -> str:
    """
    A basic test tool. Returns a greeting with some system info.

    Args:
        name: Name to greet (default: "world")

    Returns:
        A greeting string confirming the tool is alive.
    """
    import platform
    import datetime

    now = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    system = platform.system()
    python = platform.python_version()

    return (
        f"Hello, {name}! "
        f"Tool is alive and running. "
        f"Time: {now} | OS: {system} | Python: {python}"
    )
