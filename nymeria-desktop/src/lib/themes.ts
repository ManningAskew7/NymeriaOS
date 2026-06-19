/**
 * Theme definitions for Nymeria Desktop
 *
 * Each theme defines all the CSS custom properties used throughout the app.
 * Themes are applied instantly by updating CSS variables on the document root.
 *
 * COLOR TOKEN MODEL
 * -----------------
 * `ThemeColors` is the app's semantic color layer. Values are hand-picked per
 * theme, but each token maps to a fixed role on the Radix Colors 12-step scale,
 * so the same token does the same job in every theme. Author new values by step
 * role, not by eye:
 *
 *   step 1  -> bgBase        app background
 *   step 2  -> bgElevated    subtle surface (sidebar, panels)
 *   step 3  -> bgElevated2   UI component background, rest
 *   step 4  -> bgHover       UI component background, hover
 *   step 5  -> bgActive      UI component background, active / selected
 *   step 6  -> borderSubtle  non-interactive hairline (cards, dividers)
 *   step 7  -> borderDefault interactive UI border (inputs, buttons)
 *   step 8  -> (--border-focus, derived = accentPrimary) focus ring
 *   step 9  -> accentPrimary solid accent fill (primary actions)
 *   step 10 -> accentHover   solid accent fill, hover
 *   step 11 -> textSecondary low-contrast text (meta, labels)
 *   step 12 -> textPrimary   highest-contrast text (body, headings)
 *
 * `textOnAccent` is the paired-role token (Material 3 convention): the legible
 * text/icon color to place ON a solid `accentPrimary` fill. Pick it for contrast
 * against the accent, not against the background.
 *
 * House rule: accents stay calm (~47% HSL saturation); bright color is reserved
 * for primary actions. Platinum is the documented exception that runs a brighter
 * (near-white silver) accent.
 */

export type ThemeName = 'midnight' | 'light' | 'platinum';

export interface ThemeColors {
  // Backgrounds — Radix steps 1-5 (app bg -> active component bg)
  bgBase: string;
  bgElevated: string;
  bgElevated2: string;
  bgHover: string;
  bgActive: string;

  // Text — Radix steps 11-12 (+ a muted tier below 11)
  textPrimary: string;
  textSecondary: string;
  textMuted: string;

  // Accents — solid fill at steps 9-10, plus the on-accent paired role
  accentPrimary: string;
  accentSecondary: string;
  accentHover: string;
  textOnAccent: string;

  // Semantic
  success: string;
  warning: string;
  error: string;
  info: string;

  // Message bubbles
  bubbleUser: string;
  bubbleAi: string;
  bubbleTool: string;

  // Borders — Radix steps 6 (subtle hairline) and 7 (interactive UI border)
  borderSubtle: string;
  borderDefault: string;
}

export interface ThemeMetadata {
  name: string;
  description: string;
  colors: ThemeColors;
}

const themeColorCssVariables = {
  bgBase: '--bg-base',
  bgElevated: '--bg-elevated',
  bgElevated2: '--bg-elevated-2',
  bgHover: '--bg-hover',
  bgActive: '--bg-active',
  textPrimary: '--text-primary',
  textSecondary: '--text-secondary',
  textMuted: '--text-muted',
  accentPrimary: '--accent-primary',
  accentSecondary: '--accent-secondary',
  accentHover: '--accent-hover',
  textOnAccent: '--text-on-accent',
  success: '--success',
  warning: '--warning',
  error: '--error',
  info: '--info',
  bubbleUser: '--bubble-user',
  bubbleAi: '--bubble-ai',
  bubbleTool: '--bubble-tool',
  borderSubtle: '--border-subtle',
  borderDefault: '--border-default',
} as const satisfies Record<keyof ThemeColors, `--${string}`>;

export const themes: Record<ThemeName, ThemeMetadata> = {
  midnight: {
    name: 'Midnight',
    description: 'Default dark theme with cyan accents',
    colors: {
      bgBase: '#121417',
      bgElevated: '#1a1d21',
      bgElevated2: '#22262b',
      bgHover: '#2a2e33',
      bgActive: '#32373d',
      textPrimary: '#e8eaed',
      // Low-contrast meta/label tier (Radix step 11). Lifted from #a6afb3
      // (5.37:1 on bgActive) to clear the dim-text complaint with headroom:
      // now ~6.5:1 on bgActive worst-case, brighter on every darker surface.
      // Paired with the textMuted lift below so the step-down stays visible
      // (secondary sits a clear step above muted, not flattened into it).
      textSecondary: '#b9c0c5',
      // Dimmest text tier (timestamps, hints, placeholder, metadata). Lifted
      // from #9ba3a9 (4.69:1 on bgActive, right at the AA floor) to ~5.5:1 on
      // bgActive worst-case, so low-emphasis text reads cleanly rather than
      // unfinished. Stays a clear step below textSecondary (about 0.09 L gap).
      textMuted: '#a9b0b6',
      // Bright cornflower blue: a brightened step up from the reference
      // mockup's periwinkle, per the design owner. One coherent blue across
      // buttons, links, focus rings and active states (primary == secondary).
      // Light, so dark on-accent ink reads on the fill (~6.9:1) and the accent
      // clears AA as text on bgBase (~6.8:1). accentHover keeps a
      // darker-than-primary hover offset. Give secondary a distinct sibling if
      // differentiation is needed.
      accentPrimary: '#6fa0db',
      accentSecondary: '#6fa0db',
      accentHover: '#5b8bc7',
      // Dark ink for text/icons placed on the light blue accent fill.
      textOnAccent: '#0b1416',
      success: '#34d399',
      warning: '#fbbf24',
      error: '#f87171',
      info: '#818cf8',
      // Calm blue-tinted slate so the user-action surface pairs with the blue
      // accent (the same "bgBase + accent direction" pattern Light uses with
      // #c9e3df). Re-tinted from the old cyan #1a3036 when the accent moved to
      // blue; same low tint strength (just rotated hue) to avoid a saturated navy.
      bubbleUser: '#1a2636',
      bubbleAi: '#1a1d21',
      bubbleTool: '#1a1f2e',
      // Hairline borders (steps 6/7). Surface separation comes from the
      // elevation ladder above, not from heavy lines — these sit just enough
      // above the panel bg (~rgb(25,28,32)) to frame cleanly without shouting.
      // Intentional WCAG SC 1.4.11 non-compliance: a WCAG-compliant
      // borderDefault (3:1 on every surface) lands at ~#707275, which read
      // as too heavy in the running app. The hairline aesthetic is preserved
      // and the failure is documented; the focus ring (--border-focus =
      // accentPrimary) gives high contrast on interaction.
      borderSubtle: '#24282d',
      borderDefault: '#343a40',
    },
  },

  light: {
    name: 'Light',
    description: 'Warm-paper editorial theme with calm teal and amber',
    colors: {
      // Deeper, more saturated warm paper for the main canvas — gives the
      // theme a real "aged page" character instead of near-white wash.
      bgBase: '#f1ead7',
      // Bright off-white for elevated surfaces (sidebar, dashboard panel,
      // cards) — sits cleanly *above* the warm base so layout depth reads.
      bgElevated: '#fdfbf2',
      bgElevated2: '#e7e0c7',
      bgHover: '#dbd2b7',
      bgActive: '#c9bf9d',
      textPrimary: '#1a1612',
      // Ink-on-paper greys: secondary and muted both shifted darker so the
      // typography (timestamps, meta, hint copy) stays comfortably legible.
      textSecondary: '#3d3830',
      textMuted: '#544c3f',
      // Teal accent. Previously #2d7d75 (4.07:1 as text on bgBase, sub-AA;
      // 3.69:1 on bgElevated2). Darkened just enough to clear 4.5:1 as text
      // on every Light bg, while keeping the same teal hue family (G ~= B
      // with both >> R, characteristic blue-leaning teal). Hover stays in the
      // lift-on-hover convention -- accentHover is perceptibly lighter than
      // accentPrimary, but darkened from #3a988e (was textOnAccent 3.34:1,
      // sub-AA) to give textOnAccent #fdfbf2 a clear 4.55:1 against it.
      // Worst-case ratios:
      //   accentPrimary #2d6b69:  text on bgElevated2 4.65:1
      //                           textOnAccent on it  5.93:1
      //   accentHover   #3a7e7c:  textOnAccent on it  4.55:1
      accentPrimary: '#2d6b69',
      accentSecondary: '#b07a3a',
      accentHover: '#3a7e7c',
      // Off-white for text/icons on the teal/amber solid fills.
      textOnAccent: '#fdfbf2',
      // Semantic colors. The previous tier (#2a8f4c / #bc8715 / #cc3333 /
      // #336bcc) mused toward "calm-paper character" but failed WCAG AA on
      // every Light background — warning was 2.40:1 on bgElevated2 (need 4.5
      // as text). Walked back to clear 4.5:1 on bgBase/bgElevated/bgElevated2
      // for both text (4.5:1) and indicator (3:1) roles while keeping each
      // hue family. These darker values stay calmer than raw Tailwind-600 by
      // preserving the moderate-saturation character; the contrast comes
      // from luminance, not loudness.
      //   success: #2a8f4c -> #2a6b4c  (4.80:1 worst-case on bgElevated2)
      //   warning: #bc8715 -> #885705  (4.66:1 worst-case)
      //   error:   #cc3333 -> #b03333  (4.72:1 worst-case)
      //   info:    #336bcc -> #3357cc  (4.71:1 worst-case)
      success: '#2a6b4c',
      warning: '#885705',
      error: '#b03333',
      info: '#3357cc',
      // Bubbles tuned to the new palette so they sit on the page coherently
      // instead of looking pasted on.
      bubbleUser: '#c9e3df',
      bubbleAi: '#fdfbf2',
      bubbleTool: '#efe3c4',
      // Borders given a touch more warmth and visibility so card outlines
      // actually delineate the surfaces. Same intentional WCAG SC 1.4.11
      // non-compliance as Midnight: the 3:1-compliant value lands at ~#8a7871
      // (a brown-mauve) which felt too heavy on the warm paper, so the
      // hairline aesthetic is preserved here too.
      borderSubtle: '#d4ccb1',
      borderDefault: '#ab9f81',
    },
  },

  platinum: {
    name: 'Platinum',
    description: 'Premium monochrome with platinum silver accents',
    colors: {
      // Lifted off near-black to a desaturated cool-dark surface (still darker
      // than Midnight) with a faint blue cast, then a genuine elevation ladder
      // so depth reads instead of flat black.
      bgBase: '#0d0e12',
      bgElevated: '#15171c',
      bgElevated2: '#1d2026',
      bgHover: '#282c33',
      bgActive: '#333841',
      textPrimary: '#f0f0f2',
      // Low-contrast meta/label tier (Radix step 11). Lifted from #a8a8b0
      // (4.99:1 on bgActive) to ~6.5:1 on bgActive worst-case, paired with the
      // textMuted lift below so the step-down stays visible.
      textSecondary: '#c0c0c7',
      // Dimmest text tier. Lifted from #9fa0a5 (4.51:1 on bgActive, right at
      // the AA floor) to ~5.4:1 on bgActive worst-case, so low-emphasis text
      // reads cleanly. Stays a clear step below textSecondary (about 0.09 L gap).
      textMuted: '#afb0b5',
      // Documented exception: Platinum runs a brighter near-white silver accent.
      accentPrimary: '#e0f0ff',
      accentSecondary: '#8291a8',
      accentHover: '#f0f8ff',
      // Dark ink for text/icons on the near-white accent fill.
      textOnAccent: '#0c0e12',
      success: '#86efac',
      warning: '#fcd34d',
      error: '#f87171',
      info: '#a5b4fc',
      bubbleUser: '#2a2d36',
      bubbleAi: '#15171c',
      bubbleTool: '#1c2027',
      borderSubtle: '#232831',
      // Same intentional WCAG SC 1.4.11 non-compliance as Midnight: the
      // 3:1-compliant Platinum borderDefault lands at ~#6c6d70 (a clearly
      // visible neutral gray) which felt too heavy in the running app, so
      // the hairline aesthetic is preserved here too.
      borderDefault: '#333a44',
    },
  },
};

/**
 * Convert a hex color to rgba string.
 */
function hexToRgba(hex: string, alpha: number): string {
  const r = parseInt(hex.slice(1, 3), 16);
  const g = parseInt(hex.slice(3, 5), 16);
  const b = parseInt(hex.slice(5, 7), 16);
  return `rgba(${r}, ${g}, ${b}, ${alpha})`;
}

/**
 * Extract RGB components from a hex color string.
 */
function hexToRgb(hex: string): { r: number; g: number; b: number } {
  return {
    r: parseInt(hex.slice(1, 3), 16),
    g: parseInt(hex.slice(3, 5), 16),
    b: parseInt(hex.slice(5, 7), 16),
  };
}

/**
 * Determine if a theme is light-toned by checking its base background luminance.
 */
function isLightTheme(bgBase: string): boolean {
  const { r, g, b } = hexToRgb(bgBase);
  const luminance = (0.299 * r + 0.587 * g + 0.114 * b) / 255;
  return luminance > 0.5;
}

function applyThemeColorVariables(root: HTMLElement, colors: ThemeColors): void {
  for (const [token, cssVariable] of Object.entries(themeColorCssVariables) as Array<
    [keyof ThemeColors, (typeof themeColorCssVariables)[keyof ThemeColors]]
  >) {
    root.style.setProperty(cssVariable, colors[token]);
  }
}

/**
 * Apply a theme by updating CSS custom properties on the document root.
 * Changes take effect immediately without requiring a page reload.
 */
export function applyTheme(themeName: ThemeName): void {
  const theme = themes[themeName];
  if (!theme) {
    console.warn(`Unknown theme: ${themeName}, falling back to midnight`);
    applyTheme('midnight');
    return;
  }

  const colors = theme.colors;
  const root = document.documentElement;
  const light = isLightTheme(colors.bgBase);

  root.setAttribute('data-theme', themeName);

  applyThemeColorVariables(root, colors);
  root.style.setProperty('--border-focus', colors.accentPrimary);

  // Glassmorphism — computed dynamically per theme
  root.style.setProperty('--glass-bg', hexToRgba(colors.bgElevated, light ? 0.8 : 0.7));
  root.style.setProperty('--glass-bg-strong', hexToRgba(colors.bgElevated, light ? 0.92 : 0.85));
  root.style.setProperty('--glass-border', light ? 'rgba(0, 0, 0, 0.08)' : 'rgba(255, 255, 255, 0.08)');
  root.style.setProperty('--glass-shadow', light
    ? '0 8px 32px rgba(0, 0, 0, 0.08)'
    : '0 8px 32px rgba(0, 0, 0, 0.3)');

  // Card shadows — the defaults in app.css use rgba(0,0,0,0.3-0.5) which is
  // appropriate for dark themes but creates a heavy dark edge on a paper
  // background. Light themes get softer, lower-opacity shadows.
  //
  // --shadow-xl was previously left at the static value from app.css
  // (rgba(0,0,0,0.5)), which reads as a harsh 50% black edge on the
  // warm-paper Light theme. Override it here so it stays in step with
  // the rest of the ladder.
  if (light) {
    root.style.setProperty('--shadow-sm', '0 1px 2px rgba(0, 0, 0, 0.04)');
    root.style.setProperty('--shadow-md', '0 2px 6px rgba(0, 0, 0, 0.06)');
    root.style.setProperty('--shadow-lg', '0 8px 20px rgba(0, 0, 0, 0.08)');
    root.style.setProperty('--shadow-xl', '0 20px 40px rgba(0, 0, 0, 0.12)');
  } else {
    root.style.setProperty('--shadow-sm', '0 1px 2px rgba(0, 0, 0, 0.3)');
    root.style.setProperty('--shadow-md', '0 4px 6px rgba(0, 0, 0, 0.4)');
    root.style.setProperty('--shadow-lg', '0 10px 15px rgba(0, 0, 0, 0.5)');
    root.style.setProperty('--shadow-xl', '0 24px 48px rgba(0, 0, 0, 0.5)');
  }

  // Accent derived — RGB triplet and alpha variant used by many components
  const accent = hexToRgb(colors.accentPrimary);
  root.style.setProperty('--accent-primary-rgb', `${accent.r}, ${accent.g}, ${accent.b}`);
  root.style.setProperty('--accent-tint-bg', `rgba(${accent.r}, ${accent.g}, ${accent.b}, 0.15)`);
  root.style.setProperty('--accent-tint-border', `rgba(${accent.r}, ${accent.g}, ${accent.b}, 0.30)`);

  // Accent glow — derived from theme accent
  root.style.setProperty('--accent-glow-sm', `0 0 12px rgba(${accent.r}, ${accent.g}, ${accent.b}, 0.15)`);
  root.style.setProperty('--accent-glow-md', `0 0 20px rgba(${accent.r}, ${accent.g}, ${accent.b}, 0.2)`);

  // Semantic RGB triplets — derived from theme semantic colors so any
  // `rgba(var(--error-rgb), 0.15)` style alpha variant tracks the theme.
  // Before this, every alpha-error / alpha-warning / alpha-success bg in
  // the app hardcoded a literal `rgba(239, 68, 68, …)` triplet, which is
  // why §2 audit found 40+ raw rgba uses with the same three colors.
  const error = hexToRgb(colors.error);
  const warning = hexToRgb(colors.warning);
  const success = hexToRgb(colors.success);
  const info = hexToRgb(colors.info);
  root.style.setProperty('--error-rgb', `${error.r}, ${error.g}, ${error.b}`);
  root.style.setProperty('--warning-rgb', `${warning.r}, ${warning.g}, ${warning.b}`);
  root.style.setProperty('--success-rgb', `${success.r}, ${success.g}, ${success.b}`);
  root.style.setProperty('--info-rgb', `${info.r}, ${info.g}, ${info.b}`);
}

/**
 * Get list of all available themes with metadata
 */
export function getThemeList(): Array<{ id: ThemeName; name: string; description: string }> {
  return (Object.entries(themes) as [ThemeName, ThemeMetadata][]).map(([id, meta]) => ({
    id,
    name: meta.name,
    description: meta.description,
  }));
}

/**
 * Get a single color swatch preview for a theme (for UI display)
 */
export function getThemePreviewColors(themeName: ThemeName): {
  bg: string;
  accent: string;
  text: string;
} {
  const theme = themes[themeName];
  return {
    bg: theme.colors.bgBase,
    accent: theme.colors.accentPrimary,
    text: theme.colors.textPrimary,
  };
}
