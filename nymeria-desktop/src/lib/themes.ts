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
      accentPrimary: '#22d3ee',
      accentSecondary: '#3b82f6',
      accentHover: '#06b6d4',
      success: '#34d399',
      warning: '#fbbf24',
      error: '#f87171',
      info: '#818cf8',
      bubbleUser: '#1e3a5f',
      bubbleAi: '#1a1d21',
      bubbleTool: '#1a1f2e',
      borderSubtle: '#2a2e33',
      borderDefault: '#3a3f45',
    },
  },

  monokai: {
    name: 'Monokai',
    description: 'Classic editor theme with hot-pink accents',
    colors: {
      bgBase: '#272822',
      bgElevated: '#2f3028',
      bgElevated2: '#3a3b31',
      bgHover: '#45463c',
      bgActive: '#52534a',
      textPrimary: '#f8f8f2',
      textSecondary: '#d9d9c6',
      textMuted: '#8e8a6f',
      accentPrimary: '#f92672',
      accentSecondary: '#fd971f',
      accentHover: '#ff5b95',
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
    description: 'Velvet dark theme with purple and pink accents',
    colors: {
      bgBase: '#282a36',
      bgElevated: '#2e3140',
      bgElevated2: '#353849',
      bgHover: '#3f4254',
      bgActive: '#4a4d63',
      textPrimary: '#f8f8f2',
      textSecondary: '#d6d6c8',
      textMuted: '#7d85b3',
      accentPrimary: '#bd93f9',
      accentSecondary: '#ff79c6',
      accentHover: '#d5b5fc',
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
    description: 'Warm-paper editorial theme with teal and amber',
    colors: {
      bgBase: '#f7f5ef',
      bgElevated: '#ffffff',
      bgElevated2: '#eeebe1',
      bgHover: '#e3dfd1',
      bgActive: '#d4cfbf',
      textPrimary: '#1c1917',
      textSecondary: '#57534e',
      textMuted: '#8b857a',
      accentPrimary: '#0d9488',
      accentSecondary: '#d97706',
      accentHover: '#14b8a6',
      success: '#16a34a',
      warning: '#ca8a04',
      error: '#dc2626',
      info: '#2563eb',
      bubbleUser: '#cce6e3',
      bubbleAi: '#ffffff',
      bubbleTool: '#f5ebd9',
      borderSubtle: '#e5e2d6',
      borderDefault: '#c9c3b1',
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

  // Backgrounds
  root.style.setProperty('--bg-base', colors.bgBase);
  root.style.setProperty('--bg-elevated', colors.bgElevated);
  root.style.setProperty('--bg-elevated-2', colors.bgElevated2);
  root.style.setProperty('--bg-hover', colors.bgHover);
  root.style.setProperty('--bg-active', colors.bgActive);

  // Text
  root.style.setProperty('--text-primary', colors.textPrimary);
  root.style.setProperty('--text-secondary', colors.textSecondary);
  root.style.setProperty('--text-muted', colors.textMuted);

  // Accents
  root.style.setProperty('--accent-primary', colors.accentPrimary);
  root.style.setProperty('--accent-secondary', colors.accentSecondary);
  root.style.setProperty('--accent-hover', colors.accentHover);

  // Semantic
  root.style.setProperty('--success', colors.success);
  root.style.setProperty('--warning', colors.warning);
  root.style.setProperty('--error', colors.error);
  root.style.setProperty('--info', colors.info);

  // Message bubbles
  root.style.setProperty('--bubble-user', colors.bubbleUser);
  root.style.setProperty('--bubble-ai', colors.bubbleAi);
  root.style.setProperty('--bubble-tool', colors.bubbleTool);

  // Borders
  root.style.setProperty('--border-subtle', colors.borderSubtle);
  root.style.setProperty('--border-default', colors.borderDefault);
  root.style.setProperty('--border-focus', colors.accentPrimary);

  // Glassmorphism — computed dynamically per theme
  root.style.setProperty('--glass-bg', hexToRgba(colors.bgElevated, light ? 0.8 : 0.7));
  root.style.setProperty('--glass-bg-strong', hexToRgba(colors.bgElevated, light ? 0.92 : 0.85));
  root.style.setProperty('--glass-border', light ? 'rgba(0, 0, 0, 0.08)' : 'rgba(255, 255, 255, 0.08)');
  root.style.setProperty('--glass-shadow', light
    ? '0 8px 32px rgba(0, 0, 0, 0.1)'
    : '0 8px 32px rgba(0, 0, 0, 0.3)');

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
