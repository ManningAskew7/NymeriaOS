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
      textSecondary: '#9aa0a6',
      // Bumped from #6b7280 (was 3.12:1 on bg-elevated-2, sub-AA). Now 4.79:1
      // worst-case — clears WCAG AA Normal while preserving the blue-gray hue
      // family and staying clearly below textSecondary in the type scale.
      textMuted: '#888d96',
      accentPrimary: '#5fb8cc',
      accentSecondary: '#5b8bbf',
      accentHover: '#4ba3b8',
      // Dark ink for text/icons placed on the light cyan accent fill.
      textOnAccent: '#0b1416',
      success: '#34d399',
      warning: '#fbbf24',
      error: '#f87171',
      info: '#818cf8',
      // Calm cyan-tinted slate so the user-action surface pairs with the accent
      // (the same "bgBase + accent direction" pattern Light uses with #c9e3df).
      // Old #1e3a5f was a saturated iMessage-style navy that read disjoint
      // against the calm cyan accent.
      bubbleUser: '#1a3036',
      bubbleAi: '#1a1d21',
      bubbleTool: '#1a1f2e',
      // Hairline borders (steps 6/7). Surface separation comes from the
      // elevation ladder above, not from heavy lines — these sit just enough
      // above the panel bg (~rgb(25,28,32)) to frame cleanly without shouting.
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
      // Muted to ~47% HSL saturation to match the calmer-accent design.
      // Untouched per user request.
      accentPrimary: '#2d7d75',
      accentSecondary: '#b07a3a',
      accentHover: '#3a988e',
      // Off-white for text/icons on the teal/amber solid fills.
      textOnAccent: '#fdfbf2',
      // Semantic colors muted from Tailwind -600 (which assume bright-white bg)
      // toward the calm-paper character. accentPrimary sits at ~47% saturation;
      // semantics land between calm (47%) and Tailwind's alarm tier (75-95%) so
      // they still read as state indicators without shouting on warm paper.
      //   success: #16a34a (76% sat) -> #2a8f4c (55%) — calmer green
      //   warning: #ca8a04 (96% sat) -> #bc8715 (80%) — less neon amber
      //   error:   #dc2626 (72% sat) -> #cc3333 (60%) — softer red
      //   info:    #2563eb (83% sat) -> #336bcc (60%) — calmer blue
      success: '#2a8f4c',
      warning: '#bc8715',
      error: '#cc3333',
      info: '#336bcc',
      // Bubbles tuned to the new palette so they sit on the page coherently
      // instead of looking pasted on.
      bubbleUser: '#c9e3df',
      bubbleAi: '#fdfbf2',
      bubbleTool: '#efe3c4',
      // Borders given a touch more warmth and visibility so card outlines
      // actually delineate the surfaces.
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
      textSecondary: '#a8a8b0',
      // Bumped from #6e6e75 (was 3.11:1 on bg-elevated-2, sub-AA). Now 4.93:1
      // worst-case — clears WCAG AA Normal while preserving the neutral-cool
      // hue family and staying clearly below textSecondary in the type scale.
      textMuted: '#8b8b93',
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
