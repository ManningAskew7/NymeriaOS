import importlib.util
from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]
SCRIPT = ROOT / "scripts" / "verify_desktop_bundle_contract.py"


def _load_contract_module():
    spec = importlib.util.spec_from_file_location("verify_desktop_bundle_contract", SCRIPT)
    assert spec is not None
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


def test_desktop_bundle_contract_matches_runtime_lookup() -> None:
    module = _load_contract_module()

    results = module.verify_contract(ROOT, require_built_backend=False)

    assert results
    assert not any(line.startswith("[desktop-bundle-contract]") for line in results)
    assert any("Tauri resource target" in line for line in results)
    assert any("Process manager probes resource root path" in line for line in results)
