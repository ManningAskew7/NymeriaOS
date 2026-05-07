from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def test_python_package_includes_frontend_bundle_pattern() -> None:
    pyproject = (ROOT / "pyproject.toml").read_text(encoding="utf-8")

    assert '[tool.setuptools.package-data]' in pyproject
    assert '"frontend/**/*"' in pyproject


def test_frontend_bundle_exists_inside_python_package() -> None:
    frontend_dir = ROOT / "nymeria" / "frontend"

    assert (frontend_dir / "index.html").is_file()
    assert (frontend_dir / "_app").is_dir()
    assert (frontend_dir / "wolfhead-transparent.png").is_file()
