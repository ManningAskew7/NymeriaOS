/**
 * Theme definitions for Nymeria Desktop
 *
 * Each theme defines all the CSS custom properties used throughout the app.
 * Themes are applied instantly by updating CSS variables on the document root.
 */

export type ThemeName = 'midnight' | 'monokai' | 'dracula' | 'light' | 'high-contrast' | 'platinum';

export interface ThemeColors {
  // Backgrounds
  bgBase: string;
  bgElevated: string;
  bgElevated2: string;
  bgHover: string;
  bgActive: string;

  // Text
  textPrimary: string;
  textSecondary: string;
  textMuted: string;

  // Accents
  accentPrimary: string;
  accentSecondary: string;
  accentHover: string;

  // Semantic
  success: string;
  warning: string;
  error: string;
  info: string;

  // Message bubbles
  bubbleUser: string;
  bubbleAi: string;
  bubbleTool: string;

  // Borders
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
      textMuted: '#6b7280',
      accentPrimary: '#5fb8cc',
      accentSecondary: '#5b8bbf',
      accentHover: '#4ba3b8',
      success: '#34d399',
      warning: '#fbbf24',
      error: '#f87171',
      info: '#818cf8',
      bubbleUser: '#1e3a5f',
      bubbleAi: '#1a1d21',
      bubbleTool: '#1a1f2e',
      // Bumped from #2a2e33 (matched bgHover, swallowed by elevated bg) to
      // a clearly distinguishable steel-gray. The right panel's effective
      // bg over bg-base lands around rgb(25, 28, 32); these border values
      // sit a solid ~25-35pt above that, so the framing reads cleanly.
      borderSubtle: '#4a5058',
      borderDefault: '#5a6068',
    },
  },

  monokai: {
    name: 'Monokai',
    description: 'Classic editor theme with calm hot-pink accents',
    colors: {
      bgBase: '#272822',
      bgElevated: '#2f3028',
      bgElevated2: '#3a3b31',
      bgHover: '#45463c',
      bgActive: '#52534a',
      textPrimary: '#f8f8f2',
      textSecondary: '#d9d9c6',
      textMuted: '#8e8a6f',
      // Muted to ~47% HSL saturation to match the calmer-accent design.
      // Original was '#f92672' / '#fd971f' / '#ff5b95'.
      accentPrimary: '#cc5478',
      accentSecondary: '#cc8746',
      accentHover: '#d97aa3',
      success: '#a6e22e',
      warning: '#fd971f',
      error: '#ff6e6e',
      info: '#66d9ef',
      bubbleUser: '#5c1e3d',
      bubbleAi: '#2f3028',
      bubbleTool: '#3d3424',
      borderSubtle: '#35362d',
      borderDefault: '#49483e',
    },
  },

  dracula: {
    name: 'Dracula',
    description: 'Velvet dark theme with calm purple and pink accents',
    colors: {
      bgBase: '#282a36',
      bgElevated: '#2e3140',
      bgElevated2: '#353849',
      bgHover: '#3f4254',
      bgActive: '#4a4d63',
      textPrimary: '#f8f8f2',
      textSecondary: '#d6d6c8',
      textMuted: '#7d85b3',
      // Muted to ~47% HSL saturation to match the calmer-accent design.
      // Original was '#bd93f9' / '#ff79c6' / '#d5b5fc'.
      accentPrimary: '#a796cc',
      accentSecondary: '#c98ab1',
      accentHover: '#bdadd9',
      success: '#50fa7b',
      warning: '#f1fa8c',
      error: '#ff5555',
      info: '#8be9fd',
      bubbleUser: '#4a3a7a',
      bubbleAi: '#2e3140',
      bubbleTool: '#3d3449',
      borderSubtle: '#34374a',
      borderDefault: '#44475a',
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
      success: '#16a34a',
      warning: '#ca8a04',
      error: '#dc2626',
      info: '#2563eb',
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

  'high-contrast': {
    name: 'High Contrast',
    description: 'Pure black background with amber accents for accessibility',
    colors: {
      bgBase: '#000000',
      bgElevated: '#0f0f10',
      bgElevated2: '#1a1a1c',
      bgHover: '#26262a',
      bgActive: '#34343a',
      textPrimary: '#ffffff',
      textSecondary: '#e5e5e5',
      textMuted: '#a3a3a3',
      accentPrimary: '#fbbf24',
      accentSecondary: '#22d3ee',
      accentHover: '#fcd34d',
      success: '#4ade80',
      warning: '#fb923c',
      error: '#f87171',
      info: '#60a5fa',
      bubbleUser: '#78350f',
      bubbleAi: '#0f0f10',
      bubbleTool: '#292524',
      borderSubtle: '#2a2a2a',
      borderDefault: '#525252',
    },
  },

  platinum: {
    name: 'Platinum',
    description: 'Premium monochrome with platinum silver accents',
    colors: {
      bgBase: '#0a0a0c',
      bgElevated: '#141418',
      bgElevated2: '#1e1e23',
      bgHover: '#2a2a30',
      bgActive: '#36363d',
      textPrimary: '#f0f0f2',
      textSecondary: '#a8a8b0',
      textMuted: '#6e6e75',
      accentPrimary: '#e0f0ff',
      accentSecondary: '#8291a8',
      accentHover: '#f0f8ff',
      success: '#86efac',
      warning: '#fcd34d',
      error: '#f87171',
      info: '#a5b4fc',
      bubbleUser: '#2a2d36',
      bubbleAi: '#141418',
      bubbleTool: '#1c1e24',
      borderSubtle: '#1e1e23',
      borderDefault: '#3a3a40',
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
  if (light) {
    root.style.setProperty('--shadow-sm', '0 1px 2px rgba(0, 0, 0, 0.04)');
    root.style.setProperty('--shadow-md', '0 2px 6px rgba(0, 0, 0, 0.06)');
    root.style.setProperty('--shadow-lg', '0 8px 20px rgba(0, 0, 0, 0.08)');
  } else {
    root.style.setProperty('--shadow-sm', '0 1px 2px rgba(0, 0, 0, 0.3)');
    root.style.setProperty('--shadow-md', '0 4px 6px rgba(0, 0, 0, 0.4)');
    root.style.setProperty('--shadow-lg', '0 10px 15px rgba(0, 0, 0, 0.5)');
  }

  // Accent derived — RGB triplet and alpha variant used by many components
  const accent = hexToRgb(colors.accentPrimary);
  root.style.setProperty('--accent-primary-rgb', `${accent.r}, ${accent.g}, ${accent.b}`);
  root.style.setProperty('--accent-primary-alpha', `rgba(${accent.r}, ${accent.g}, ${accent.b}, 0.15)`);

  // Accent glow — derived from theme accent
  root.style.setProperty('--accent-glow-sm', `0 0 12px rgba(${accent.r}, ${accent.g}, ${accent.b}, 0.15)`);
  root.style.setProperty('--accent-glow-md', `0 0 20px rgba(${accent.r}, ${accent.g}, ${accent.b}, 0.2)`);
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
