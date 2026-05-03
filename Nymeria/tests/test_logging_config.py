import importlib.util

from nymeria.config.logging_config import LOG_PROFILES


def test_compactor_profile_targets_live_compaction_module():
    logger_names = [name for name, _level in LOG_PROFILES["compactor"]]

    assert importlib.util.find_spec("nymeria.core.compactor") is None
    assert "nymeria.core.compactor" not in logger_names
    assert "nymeria.core.agent_compaction" in logger_names
