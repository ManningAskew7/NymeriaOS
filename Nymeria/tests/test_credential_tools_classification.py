"""Classification tests for the credential-management tools.

``auth_inspect``, ``auth_cleanup``, ``auth_bindings``, and ``request_credential``
were demoted from ``SEED_TOOLS`` (always-on default) into the opt-in
``CATALOG_TOOLS``. They are now delivered just-in-time by the bundled,
default-on ``credential-management`` Skill Kit (which binds them with a TTL on
activation), the same way ``tool-management`` / ``skill-management`` /
``mcp-management`` deliver their tools. They must stay registered in the catalog
so the kit can bind them by name and the agent can still enable them directly.
"""

CREDENTIAL_TOOL_NAMES = (
    "auth_inspect",
    "auth_cleanup",
    "auth_bindings",
    "request_credential",
)


def test_credential_tools_are_opt_in_catalog_tools():
    from nymeria.tools import SEED_TOOLS, CATALOG_TOOLS

    seed_names = {t.name for t in SEED_TOOLS}
    for name in CREDENTIAL_TOOL_NAMES:
        # No longer seeded into every user's default_thread_tools.
        assert name not in seed_names, f"{name} should no longer be a seed tool"
        # Still bindable via the opt-in catalog.
        assert name in CATALOG_TOOLS, f"{name} should be in CATALOG_TOOLS"


def test_credential_tools_resolve_via_static_catalog():
    """The kit binds required tools by name at graph build via static_tool_catalog().

    Demoting them out of the seed must not make them unresolvable, or the
    default-on credential-management kit would fail to activate.
    """
    from nymeria.tools import static_tool_catalog

    catalog = static_tool_catalog()
    for name in CREDENTIAL_TOOL_NAMES:
        assert name in catalog, f"{name} must resolve in the merged tool catalog"


def test_credential_tools_match_kit_required_tools():
    """The catalog must cover exactly what the credential-management kit declares."""
    from pathlib import Path

    import nymeria
    from nymeria.skills import load_skill_directory
    from nymeria.tools import static_tool_catalog

    bundled = Path(nymeria.__file__).resolve().parent / "skills_bundled"
    skill = load_skill_directory(bundled / "credential-management", "bundled")
    assert skill is not None
    assert skill.is_skill_kit is True

    catalog = static_tool_catalog()
    for name in skill.required_tools:
        assert name in catalog, f"kit requires {name} but it is not in the catalog"


def test_credential_tools_not_force_stripped():
    """They are plain opt-in catalog tools, not capability-expansion tools.

    Membership in CAPABILITY_EXPANSION_TOOL_NAMES would force them out of every
    profile's default_thread_tools on each sync; we deliberately leave them out
    so an existing profile that had them seeded keeps them.
    """
    from nymeria.tools import CAPABILITY_EXPANSION_TOOL_NAMES

    for name in CREDENTIAL_TOOL_NAMES:
        assert name not in CAPABILITY_EXPANSION_TOOL_NAMES


def test_credential_tools_still_exported():
    import nymeria.tools as tools

    for name in CREDENTIAL_TOOL_NAMES:
        assert name in tools.__all__, f"{name} should stay exported"
