"""
WCAG 2.1 contrast audit across all Nymeria themes.

Reads the semantic color tokens for Midnight, Light, and Platinum and reports
contrast ratios for every pairing the UI actually uses, against AA thresholds:
  - Normal text: 4.5:1
  - Large text (>=24px or >=19px bold) and UI components / borders: 3:1

This is a one-shot static audit. Run from the repo root:

    python3 scripts/audit_contrast.py

The color tokens are parsed at runtime from
nymeria-desktop/src/lib/themes.ts (the UI source of truth). The desktop and
mobile copies are byte-identical by design, enforced by
scripts/check_cross_app_drift.py, so reading the desktop copy is sufficient.
Parsing rather than hand-mirroring keeps this audit honest: a color change in
themes.ts is always audited against the value that actually ships.
"""

from __future__ import annotations

import re
from pathlib import Path


# themes.ts lives at the desktop client; this script sits in repo-root scripts/.
_THEMES_TS = (
    Path(__file__).resolve().parents[1]
    / "nymeria-desktop"
    / "src"
    / "lib"
    / "themes.ts"
)


def _theme_names(text: str) -> list[str]:
    """Pull the theme ids from the `ThemeName` union in themes.ts."""
    match = re.search(r"export type ThemeName\s*=\s*([^;]+);", text)
    if not match:
        raise ValueError("could not find the ThemeName union in themes.ts")
    return re.findall(r"'([^']+)'", match.group(1))


def load_themes(path: Path | None = None) -> dict[str, dict[str, str]]:
    """Parse the semantic color tokens for every theme out of themes.ts.

    Returns ``{theme_name: {token: "#rrggbb"}}``. Raises ``ValueError`` when the
    file is not shaped as expected (a renamed/removed theme or colors block), so
    structural drift surfaces loudly instead of silently auditing stale values.
    """
    path = path or _THEMES_TS
    text = path.read_text(encoding="utf-8")

    # Bound parsing to the `export const themes = { ... };` object so nothing
    # before it (the css-variable map) or after it (helper functions) can match.
    start = text.find("export const themes")
    if start == -1:
        raise ValueError("could not find the `themes` object in themes.ts")
    end = text.find("\n};", start)
    if end == -1:
        raise ValueError("could not find the end of the `themes` object in themes.ts")
    body = text[start:end]

    themes: dict[str, dict[str, str]] = {}
    for name in _theme_names(text):
        block = re.search(
            re.escape(name) + r":\s*\{.*?colors:\s*\{(.*?)\n\s*\},",
            body,
            re.DOTALL,
        )
        if not block:
            raise ValueError(
                f"could not find the colors block for theme {name!r} in themes.ts"
            )
        # `token: '#rrggbb'` lines only: 6-digit hex, the only form themes.ts
        # ships and the only form hex_to_rgb() supports. Interspersed comments
        # and the name/description string fields carry no such value and are
        # skipped. Accept either quote style via the \2 backreference so a
        # formatter flipping the file to double quotes surfaces as a loud
        # validation failure, not a silently dropped token.
        tokens = re.findall(r"""(\w+):\s*(['"])(#[0-9a-fA-F]{6})\2""", block.group(1))
        if not tokens:
            raise ValueError(f"no color tokens parsed for theme {name!r} in themes.ts")
        themes[name] = {token: value for token, _quote, value in tokens}
    return themes


def referenced_tokens() -> set[str]:
    """Every token name used by any pairing in this audit."""
    tokens: set[str] = set()
    for pair_set in (
        TEXT_BG_PAIRS,
        ACCENT_TEXT_PAIRS,
        UI_COMPONENT_PAIRS,
        SEMANTIC_TEXT_PAIRS,
    ):
        for fg_token, bg_token, _threshold, _role in pair_set:
            tokens.add(fg_token)
            tokens.add(bg_token)
    return tokens


def validate_themes(themes: dict[str, dict[str, str]]) -> None:
    """Fail loudly if any token a pairing references is missing from a theme.

    Catches a token renamed in themes.ts that the audit still asks for, which
    would otherwise raise an opaque KeyError mid-run.
    """
    if not themes:
        raise ValueError("no themes parsed from themes.ts")
    required = referenced_tokens()
    for name, tokens in themes.items():
        missing = sorted(required - set(tokens))
        if missing:
            raise ValueError(
                f"theme {name!r} is missing token(s) referenced by the audit: {missing}"
            )


def hex_to_rgb(hex_color: str) -> tuple[int, int, int]:
    hex_color = hex_color.lstrip("#")
    return (
        int(hex_color[0:2], 16),
        int(hex_color[2:4], 16),
        int(hex_color[4:6], 16),
    )


def linearize(channel_8bit: int) -> float:
    c = channel_8bit / 255.0
    return c / 12.92 if c <= 0.03928 else ((c + 0.055) / 1.055) ** 2.4


def relative_luminance(hex_color: str) -> float:
    r, g, b = hex_to_rgb(hex_color)
    return (
        0.2126 * linearize(r)
        + 0.7152 * linearize(g)
        + 0.0722 * linearize(b)
    )


def contrast_ratio(a: str, b: str) -> float:
    la = relative_luminance(a)
    lb = relative_luminance(b)
    lighter = max(la, lb)
    darker = min(la, lb)
    return (lighter + 0.05) / (darker + 0.05)


# Pairings to test, with the AA threshold for each.
#
# Each row: (foreground_token, background_token, required_ratio, role_label)
#
# Backgrounds the text actually lands on. bgHover/bgActive are short-lived
# states but text remains readable on them, so we still check.
TEXT_BG_PAIRS = [
    ("textPrimary", "bgBase", 4.5, "body text on app bg"),
    ("textPrimary", "bgElevated", 4.5, "body text on sidebar/panel"),
    ("textPrimary", "bgElevated2", 4.5, "body text on component bg"),
    ("textPrimary", "bgHover", 4.5, "body text on hover state"),
    ("textPrimary", "bgActive", 4.5, "body text on active state"),
    ("textPrimary", "bubbleUser", 4.5, "user message text"),
    ("textPrimary", "bubbleAi", 4.5, "assistant message text"),
    ("textPrimary", "bubbleTool", 4.5, "tool message text"),
    ("textSecondary", "bgBase", 4.5, "meta text on app bg"),
    ("textSecondary", "bgElevated", 4.5, "meta text on sidebar/panel"),
    ("textSecondary", "bgElevated2", 4.5, "meta text on component bg"),
    ("textSecondary", "bgHover", 4.5, "meta text on hover state"),
    ("textSecondary", "bgActive", 4.5, "meta text on active state"),
    ("textSecondary", "bubbleAi", 4.5, "meta text in assistant bubble"),
    ("textSecondary", "bubbleTool", 4.5, "meta text in tool bubble"),
    ("textMuted", "bgBase", 4.5, "muted text on app bg"),
    ("textMuted", "bgElevated", 4.5, "muted text on sidebar/panel"),
    ("textMuted", "bgElevated2", 4.5, "muted text on component bg"),
    ("textMuted", "bgHover", 4.5, "muted text on hover state"),
    ("textMuted", "bgActive", 4.5, "muted text on active state"),
    # Paired role: text/icon on solid accent fill (primary CTA labels).
    ("textOnAccent", "accentPrimary", 4.5, "label on primary CTA"),
    ("textOnAccent", "accentHover", 4.5, "label on primary CTA hover"),
]


# Accent text on neutral backgrounds — common pattern for links and ghost
# buttons. WCAG allows 3:1 for large text and UI components; accent text is
# usually small body text in this codebase, so test against 4.5:1 too and
# call out anything that fails the stricter bar.
ACCENT_TEXT_PAIRS = [
    ("accentPrimary", "bgBase", 4.5, "accent text on app bg"),
    ("accentPrimary", "bgElevated", 4.5, "accent text on sidebar/panel"),
    ("accentPrimary", "bgElevated2", 4.5, "accent text on component bg"),
]


# UI components / non-text graphical objects against the surface they sit on.
# WCAG SC 1.4.11 (Non-text Contrast) requires 3:1.
UI_COMPONENT_PAIRS = [
    ("accentPrimary", "bgBase", 3.0, "focus ring on app bg"),
    ("accentPrimary", "bgElevated", 3.0, "focus ring on sidebar/panel"),
    ("accentPrimary", "bgElevated2", 3.0, "focus ring on component bg"),
    ("borderDefault", "bgBase", 3.0, "interactive border on app bg"),
    ("borderDefault", "bgElevated", 3.0, "interactive border on sidebar/panel"),
    ("borderDefault", "bgElevated2", 3.0, "interactive border on component bg"),
    # Semantic indicators used as fill / icon glyphs — borders, status dots.
    ("error", "bgBase", 3.0, "error indicator on app bg"),
    ("error", "bgElevated", 3.0, "error indicator on sidebar/panel"),
    ("error", "bgElevated2", 3.0, "error indicator on component bg"),
    ("warning", "bgBase", 3.0, "warning indicator on app bg"),
    ("warning", "bgElevated", 3.0, "warning indicator on sidebar/panel"),
    ("warning", "bgElevated2", 3.0, "warning indicator on component bg"),
    ("success", "bgBase", 3.0, "success indicator on app bg"),
    ("success", "bgElevated", 3.0, "success indicator on sidebar/panel"),
    ("success", "bgElevated2", 3.0, "success indicator on component bg"),
    ("info", "bgBase", 3.0, "info indicator on app bg"),
    ("info", "bgElevated", 3.0, "info indicator on sidebar/panel"),
    ("info", "bgElevated2", 3.0, "info indicator on component bg"),
]


# Semantic colors as text (error messages, warning copy, etc.) — 4.5:1 needed.
SEMANTIC_TEXT_PAIRS = [
    ("error", "bgBase", 4.5, "error text on app bg"),
    ("error", "bgElevated", 4.5, "error text on sidebar/panel"),
    ("error", "bgElevated2", 4.5, "error text on component bg"),
    ("warning", "bgBase", 4.5, "warning text on app bg"),
    ("warning", "bgElevated", 4.5, "warning text on sidebar/panel"),
    ("warning", "bgElevated2", 4.5, "warning text on component bg"),
    ("success", "bgBase", 4.5, "success text on app bg"),
    ("success", "bgElevated", 4.5, "success text on sidebar/panel"),
    ("success", "bgElevated2", 4.5, "success text on component bg"),
    ("info", "bgBase", 4.5, "info text on app bg"),
    ("info", "bgElevated", 4.5, "info text on sidebar/panel"),
    ("info", "bgElevated2", 4.5, "info text on component bg"),
]


def run_pair_set(
    themes: dict[str, dict[str, str]], theme_name: str, pair_set, label: str
) -> list[dict]:
    results = []
    theme = themes[theme_name]
    for fg_token, bg_token, threshold, role in pair_set:
        ratio = contrast_ratio(theme[fg_token], theme[bg_token])
        results.append(
            {
                "theme": theme_name,
                "category": label,
                "fg_token": fg_token,
                "bg_token": bg_token,
                "fg_hex": theme[fg_token],
                "bg_hex": theme[bg_token],
                "ratio": ratio,
                "threshold": threshold,
                "role": role,
                "passes": ratio >= threshold,
            }
        )
    return results


def main() -> None:
    themes = load_themes()
    validate_themes(themes)

    all_results = []
    for theme_name in themes:
        all_results.extend(run_pair_set(themes, theme_name, TEXT_BG_PAIRS, "text-bg"))
        all_results.extend(run_pair_set(themes, theme_name, ACCENT_TEXT_PAIRS, "accent-text"))
        all_results.extend(run_pair_set(themes, theme_name, UI_COMPONENT_PAIRS, "ui-component"))
        all_results.extend(run_pair_set(themes, theme_name, SEMANTIC_TEXT_PAIRS, "semantic-text"))

    failures = [r for r in all_results if not r["passes"]]
    near_misses = [
        r
        for r in all_results
        if r["passes"] and r["ratio"] < r["threshold"] + 0.5
    ]

    # Group by theme
    for theme_name in themes:
        theme_failures = [r for r in failures if r["theme"] == theme_name]
        theme_near = [r for r in near_misses if r["theme"] == theme_name]
        print()
        print(f"=== {theme_name.upper()} ===")
        if not theme_failures:
            print("  All pairings pass WCAG AA.")
        else:
            print(f"  {len(theme_failures)} pairing(s) FAIL WCAG AA:")
            for r in theme_failures:
                print(
                    f"    [FAIL {r['ratio']:.2f}:1, need {r['threshold']:.1f}:1] "
                    f"{r['fg_token']} ({r['fg_hex']}) on "
                    f"{r['bg_token']} ({r['bg_hex']}) :: {r['role']}"
                )
        if theme_near:
            print(f"  {len(theme_near)} near-miss(es) (pass, but within 0.5):")
            for r in theme_near:
                print(
                    f"    [near {r['ratio']:.2f}:1, need {r['threshold']:.1f}:1] "
                    f"{r['fg_token']} ({r['fg_hex']}) on "
                    f"{r['bg_token']} ({r['bg_hex']}) :: {r['role']}"
                )

    total = len(all_results)
    passing = total - len(failures)
    print()
    print(f"Total: {passing}/{total} pairings pass WCAG AA "
          f"({len(failures)} fail, {len(near_misses)} near-miss).")


if __name__ == "__main__":
    main()
