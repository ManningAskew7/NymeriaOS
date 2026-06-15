"""Classification tests for personality_set.

personality_set was demoted from SEED_TOOLS (always-on default) into the
opt-in CATALOG_TOOLS so a fresh profile no longer gets it bound by default.
It must stay registered in the catalog so it remains discoverable/bindable.
"""


def test_personality_set_is_opt_in_catalog_tool():
    from nymeria.tools import SEED_TOOLS, CATALOG_TOOLS

    # No longer seeded into every user's default_thread_tools.
    assert all(t.name != "personality_set" for t in SEED_TOOLS)

    # Still bindable via the opt-in catalog (so the agent can enable it).
    assert "personality_set" in CATALOG_TOOLS


def test_personality_set_still_exported():
    import nymeria.tools as tools

    # Tool definition stays exported even though it left the seed set.
    assert "personality_set" in tools.__all__
    assert tools.personality_set.name == "personality_set"
