from pathlib import Path


def test_backend_spec_bundles_runtime_package_data() -> None:
    spec = (Path(__file__).resolve().parents[1] / "nymeria-backend.spec").read_text(
        encoding="utf-8"
    )

    assert "collect_submodules(\"nymeria\"" in spec
    assert "\"config/*.md\"" in spec
    assert "\"frontend/**/*\"" in spec
    assert "\"skills_bundled/**/*\"" in spec
    assert "\"vendor/react_agent/*.md\"" in spec
    assert "\"google.genai\"" in spec
    assert "\"telegram.ext\"" in spec
    assert "collect_all(\"mcp\")" in spec
