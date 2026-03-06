/**
 * Theme definitions for Nymeria Desktop
 *
 * Each theme defines all the CSS custom properties used throughout the app.
 * Themes are applied instantly by updating CSS variables on the document root.
 */

export type ThemeName = 'midnight' | 'monokai' | 'dracula' | 'light' | 'high-contrast';

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
    description: 'Classic editor theme with orange accents',
    colors: {
      bgBase: '#272822',
      bgElevated: '#2d2e27',
      bgElevated2: '#3e3d32',
      bgHover: '#49483e',
      bgActive: '#55544a',
      textPrimary: '#f8f8f2',
      textSecondary: '#cfcfc2',
      textMuted: '#75715e',
      accentPrimary: '#f92672',
      accentSecondary: '#fd971f',
      accentHover: '#ff669d',
      success: '#a6e22e',
      warning: '#e6db74',
      error: '#f92672',
      info: '#66d9ef',
      bubbleUser: '#3e3d32',
      bubbleAi: '#2d2e27',
      bubbleTool: '#3e3a2e',
      borderSubtle: '#3e3d32',
      borderDefault: '#49483e',
    },
  },

  dracula: {
    name: 'Dracula',
    description: 'Popular dark theme with purple accents',
    colors: {
      bgBase: '#282a36',
      bgElevated: '#2d303e',
      bgElevated2: '#343746',
      bgHover: '#3b3e4f',
      bgActive: '#44475a',
      textPrimary: '#f8f8f2',
      textSecondary: '#c0c0c0',
      textMuted: '#6272a4',
      accentPrimary: '#bd93f9',
      accentSecondary: '#ff79c6',
      accentHover: '#caa9fa',
      success: '#50fa7b',
      warning: '#f1fa8c',
      error: '#ff5555',
      info: '#8be9fd',
      bubbleUser: '#343746',
      bubbleAi: '#2d303e',
      bubbleTool: '#303340',
      borderSubtle: '#3b3e4f',
      borderDefault: '#44475a',
    },
  },

  light: {
    name: 'Light',
    description: 'Clean light theme for daylight use',
    colors: {
      bgBase: '#ffffff',
      bgElevated: '#f9fafb',
      bgElevated2: '#f3f4f6',
      bgHover: '#e5e7eb',
      bgActive: '#d1d5db',
      textPrimary: '#111827',
      textSecondary: '#4b5563',
      textMuted: '#9ca3af',
      accentPrimary: '#3b82f6',
      accentSecondary: '#6366f1',
      accentHover: '#2563eb',
      success: '#10b981',
      warning: '#f59e0b',
      error: '#ef4444',
      info: '#6366f1',
      bubbleUser: '#dbeafe',
      bubbleAi: '#f3f4f6',
      bubbleTool: '#e0e7ff',
      borderSubtle: '#e5e7eb',
      borderDefault: '#d1d5db',
    },
  },

  'high-contrast': {
    name: 'High Contrast',
    description: 'Accessibility-focused theme with maximum contrast',
    colors: {
      bgBase: '#000000',
      bgElevated: '#0a0a0a',
      bgElevated2: '#141414',
      bgHover: '#1f1f1f',
      bgActive: '#2a2a2a',
      textPrimary: '#ffffff',
      textSecondary: '#e0e0e0',
      textMuted: '#a0a0a0',
      accentPrimary: '#fbbf24',
      accentSecondary: '#facc15',
      accentHover: '#fcd34d',
      success: '#22c55e',
      warning: '#fbbf24',
      error: '#ef4444',
      info: '#60a5fa',
      bubbleUser: '#1e293b',
      bubbleAi: '#0a0a0a',
      bubbleTool: '#1a1a2e',
      borderSubtle: '#2a2a2a',
      borderDefault: '#404040',
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
