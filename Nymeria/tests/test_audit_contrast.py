from __future__ import annotations

import importlib.util
from pathlib import Path

import pytest


PROJECT_ROOT = Path(__file__).resolve().parents[2]
SCRIPT_PATH = PROJECT_ROOT / "scripts" / "audit_contrast.py"

spec = importlib.util.spec_from_file_location("audit_contrast", SCRIPT_PATH)
assert spec is not None and spec.loader is not None
audit_contrast = importlib.util.module_from_spec(spec)
spec.loader.exec_module(audit_contrast)


# A synthetic themes.ts that mirrors the real structure: a css-variable map
# (whose '--token' values must NOT be parsed as colors), the ThemeName union,
# the themes object with interspersed comments and name/description strings,
# and trailing helper code with `light ? ... : ...` ternaries that must not be
# mistaken for a theme key.
SYNTHETIC_THEMES_TS = """\
const themeColorCssVariables = {
  bgBase: '--bg-base',
  accentPrimary: '--accent-primary',
} as const;

export type ThemeName = 'alpha' | 'beta';

export const themes = {
  alpha: {
    name: 'Alpha',
    description: 'first theme, light ? not a key',
    colors: {
      // a comment with a stray #abcdef inside prose
      bgBase: '#111111',
      accentPrimary: '#222222',
      textOnAccent: "#abccba",
    },
  },

  beta: {
    name: 'Beta',
    description: 'second theme',
    colors: {
      bgBase: '#333333',
      accentPrimary: '#444444',
      textOnAccent: '#fedcba',
    },
  },
};

function applyTheme() {
  const light = isLight();
  return light ? '#ffffff' : '#000000';
}
"""


def _write(path: Path, text: str) -> Path:
    path.write_text(text, encoding="utf-8")
    return path


def test_load_themes_parses_synthetic_structure(tmp_path: Path) -> None:
    ts = _write(tmp_path / "themes.ts", SYNTHETIC_THEMES_TS)
    parsed = audit_contrast.load_themes(ts)

    # Exactly the two themes, exactly the color tokens (no css-var map values,
    # no name/description strings, no prose-comment hex). The double-quoted
    # `textOnAccent` in alpha also proves both quote styles are accepted.
    assert parsed == {
        "alpha": {
            "bgBase": "#111111",
            "accentPrimary": "#222222",
            "textOnAccent": "#abccba",
        },
        "beta": {
            "bgBase": "#333333",
            "accentPrimary": "#444444",
            "textOnAccent": "#fedcba",
        },
    }


def test_load_themes_parses_live_themes_ts() -> None:
    parsed = audit_contrast.load_themes()

    assert set(parsed) == {"midnight", "light", "platinum"}
    required = audit_contrast.referenced_tokens()
    for name, tokens in parsed.items():
        # every token the audit asks for is present and a 6-digit hex value
        assert required <= set(tokens), f"{name} missing {required - set(tokens)}"
        for value in tokens.values():
            assert value.startswith("#")
            assert len(value) == 7

    # Lock a few known values so a parser that grabbed a comment's stale hex
    # (e.g. light.accentPrimary's commented #2d7d75) would fail loudly. These
    # come straight from themes.ts and double as a comment-immunity regression.
    assert parsed["midnight"]["bgBase"] == "#121417"
    assert parsed["light"]["accentPrimary"] == "#2d6b69"
    assert parsed["platinum"]["textOnAccent"] == "#0c0e12"


def test_validate_themes_passes_for_live() -> None:
    audit_contrast.validate_themes(audit_contrast.load_themes())


def test_validate_themes_raises_on_missing_referenced_token() -> None:
    # Drop a token the audit references; validation must fail loudly.
    themes = audit_contrast.load_themes()
    a_required = next(iter(audit_contrast.referenced_tokens()))
    del themes["midnight"][a_required]

    with pytest.raises(ValueError, match=a_required):
        audit_contrast.validate_themes(themes)


def test_validate_themes_raises_on_empty() -> None:
    with pytest.raises(ValueError, match="no themes"):
        audit_contrast.validate_themes({})


def test_load_themes_raises_when_colors_block_missing(tmp_path: Path) -> None:
    # ThemeName advertises a theme with no matching colors block in the object.
    broken = SYNTHETIC_THEMES_TS.replace(
        "export type ThemeName = 'alpha' | 'beta';",
        "export type ThemeName = 'alpha' | 'beta' | 'gamma';",
    )
    ts = _write(tmp_path / "themes.ts", broken)

    with pytest.raises(ValueError, match="gamma"):
        audit_contrast.load_themes(ts)


def test_load_themes_raises_when_themes_object_missing(tmp_path: Path) -> None:
    ts = _write(tmp_path / "themes.ts", "export type ThemeName = 'alpha';\n")

    with pytest.raises(ValueError, match="themes` object"):
        audit_contrast.load_themes(ts)
