from pathlib import Path, PurePath
import tomllib


ROOT = Path(__file__).resolve().parents[1]


def _package_data_globs() -> list[str]:
    pyproject = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))
    return pyproject["tool"]["setuptools"]["package-data"]["nymeria"]


def test_python_package_includes_frontend_bundle_pattern() -> None:
    pyproject = (ROOT / "pyproject.toml").read_text(encoding="utf-8")

    assert '[tool.setuptools.package-data]' in pyproject
    assert '"frontend/**/*"' in pyproject


def test_python_package_reads_version_from_backend_init() -> None:
    pyproject = (ROOT / "pyproject.toml").read_text(encoding="utf-8")

    assert 'dynamic = ["version"]' in pyproject
    assert 'version = { attr = "nymeria.__version__" }' in pyproject


def test_python_package_requires_supported_python_version() -> None:
    pyproject = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))

    assert pyproject["project"]["requires-python"] == ">=3.11"


def test_bundled_catalog_data_files_are_packaged() -> None:
    """Every nymeria/*_bundled catalog data file must ship in the built wheel.

    The bundled catalogs (hooks, skills, workflows) are read from the INSTALLED
    package at runtime, and their loaders treat a missing directory as an empty
    catalog. So a dir absent from package-data degrades silently: it works from
    a source checkout and the feature simply does not exist on a pip install.
    This pins each catalog to a covering glob instead. READMEs are authoring
    docs, not runtime data, and are deliberately not shipped.
    """
    globs = _package_data_globs()
    package_root = ROOT / "nymeria"
    bundled_dirs = sorted(p for p in package_root.glob("*_bundled") if p.is_dir())
    assert bundled_dirs, "expected at least one bundled catalog directory"

    for directory in bundled_dirs:
        data_files = [
            p
            for p in directory.rglob("*")
            if p.is_file() and p.name != "README.md" and "__pycache__" not in p.parts
        ]
        assert data_files, f"{directory.name} ships no data files"
        for path in data_files:
            relative = path.relative_to(package_root)
            assert any(PurePath(relative).match(glob) for glob in globs), (
                f"{relative} is not covered by any [tool.setuptools.package-data] "
                "glob, so it would be missing from the built wheel"
            )


def test_frontend_bundle_exists_inside_python_package() -> None:
    frontend_dir = ROOT / "nymeria" / "frontend"

    assert (frontend_dir / "index.html").is_file()
    assert (frontend_dir / "_app").is_dir()
    assert (frontend_dir / "wolfhead-transparent.png").is_file()
