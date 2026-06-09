"""
WCAG 2.1 contrast audit across all Nymeria themes.

Reads the semantic color tokens for Midnight, Light, and Platinum and reports
contrast ratios for every pairing the UI actually uses, against AA thresholds:
  - Normal text: 4.5:1
  - Large text (>=24px or >=19px bold) and UI components / borders: 3:1

This is a one-shot static audit. Run from the repo root:

    python3 scripts/audit_contrast.py

The token dictionaries are kept in sync with
nymeria-desktop/src/lib/themes.ts and nymeria-mobile/src/lib/themes.ts
(both files are byte-identical by design).
"""

from __future__ import annotations


# Tokens mirrored from nymeria-{desktop,mobile}/src/lib/themes.ts.
# Update both sides when these change.
THEMES = {
    "midnight": {
        "bgBase": "#121417",
        "bgElevated": "#1a1d21",
        "bgElevated2": "#22262b",
        "bgHover": "#2a2e33",
        "bgActive": "#32373d",
        "textPrimary": "#e8eaed",
        "textSecondary": "#a6afb3",
        "textMuted": "#9ba3a9",
        "accentPrimary": "#5fb8cc",
        "accentSecondary": "#5b8bbf",
        "accentHover": "#4ba3b8",
        "textOnAccent": "#0b1416",
        "success": "#34d399",
        "warning": "#fbbf24",
        "error": "#f87171",
        "info": "#818cf8",
        "bubbleUser": "#1a3036",
        "bubbleAi": "#1a1d21",
        "bubbleTool": "#1a1f2e",
        "borderSubtle": "#24282d",
        "borderDefault": "#343a40",
    },
    "light": {
        "bgBase": "#f1ead7",
        "bgElevated": "#fdfbf2",
        "bgElevated2": "#e7e0c7",
        "bgHover": "#dbd2b7",
        "bgActive": "#c9bf9d",
        "textPrimary": "#1a1612",
        "textSecondary": "#3d3830",
        "textMuted": "#544c3f",
        "accentPrimary": "#2d7d75",
        "accentSecondary": "#b07a3a",
        "accentHover": "#3a988e",
        "textOnAccent": "#fdfbf2",
        "success": "#2a8f4c",
        "warning": "#bc8715",
        "error": "#cc3333",
        "info": "#336bcc",
        "bubbleUser": "#c9e3df",
        "bubbleAi": "#fdfbf2",
        "bubbleTool": "#efe3c4",
        "borderSubtle": "#d4ccb1",
        "borderDefault": "#ab9f81",
    },
    "platinum": {
        "bgBase": "#0d0e12",
        "bgElevated": "#15171c",
        "bgElevated2": "#1d2026",
        "bgHover": "#282c33",
        "bgActive": "#333841",
        "textPrimary": "#f0f0f2",
        "textSecondary": "#a8a8b0",
        "textMuted": "#9fa0a5",
        "accentPrimary": "#e0f0ff",
        "accentSecondary": "#8291a8",
        "accentHover": "#f0f8ff",
        "textOnAccent": "#0c0e12",
        "success": "#86efac",
        "warning": "#fcd34d",
        "error": "#f87171",
        "info": "#a5b4fc",
        "bubbleUser": "#2a2d36",
        "bubbleAi": "#15171c",
        "bubbleTool": "#1c2027",
        "borderSubtle": "#232831",
        "borderDefault": "#333a44",
    },
}


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


def run_pair_set(theme_name: str, pair_set, label: str) -> list[dict]:
    results = []
    theme = THEMES[theme_name]
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
    all_results = []
    for theme_name in THEMES:
        all_results.extend(run_pair_set(theme_name, TEXT_BG_PAIRS, "text-bg"))
        all_results.extend(run_pair_set(theme_name, ACCENT_TEXT_PAIRS, "accent-text"))
        all_results.extend(run_pair_set(theme_name, UI_COMPONENT_PAIRS, "ui-component"))
        all_results.extend(run_pair_set(theme_name, SEMANTIC_TEXT_PAIRS, "semantic-text"))

    failures = [r for r in all_results if not r["passes"]]
    near_misses = [
        r
        for r in all_results
        if r["passes"] and r["ratio"] < r["threshold"] + 0.5
    ]

    # Group by theme
    for theme_name in THEMES:
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
