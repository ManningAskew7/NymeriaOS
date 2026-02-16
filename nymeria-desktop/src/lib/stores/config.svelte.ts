import type { AppConfig, ThemeName } from '$lib/types';
import { applyTheme } from '$lib/themes';

const STORAGE_KEY = 'nymeria-config';

function loadConfig(): AppConfig {
  if (typeof localStorage === 'undefined') {
    return {
      apiUrl: 'http://localhost:8000',
      apiKey: '',
      theme: 'midnight',
      suppressAttachmentWarnings: false,
    };
  }

  try {
    const stored = localStorage.getItem(STORAGE_KEY);
    if (stored) {
      const config = JSON.parse(stored);
      // Ensure theme has a default
      if (!config.theme) {
        config.theme = 'midnight';
      }
      if (config.suppressAttachmentWarnings === undefined) {
        config.suppressAttachmentWarnings = false;
      }
      return config;
    }
  } catch (e) {
    console.error('Failed to load config:', e);
  }

  return {
    apiUrl: 'http://localhost:8000',
    apiKey: '',
    theme: 'midnight',
    suppressAttachmentWarnings: false,
  };
}

function saveConfig(config: AppConfig): void {
  if (typeof localStorage === 'undefined') return;

  try {
    localStorage.setItem(STORAGE_KEY, JSON.stringify(config));
  } catch (e) {
    console.error('Failed to save config:', e);
  }
}

function createConfigStore() {
  const initial = loadConfig();
  let apiUrl = $state(initial.apiUrl);
  let apiKey = $state(initial.apiKey);
  let setupCompleted = $state(initial.setupCompleted ?? false);
  let theme = $state<ThemeName>(initial.theme ?? 'midnight');
  let suppressAttachmentWarnings = $state(initial.suppressAttachmentWarnings ?? false);

  // Apply theme on initial load (client-side only)
  if (typeof document !== 'undefined') {
    applyTheme(theme);
  }

  function saveCurrentConfig() {
    saveConfig({ apiUrl, apiKey, setupCompleted, theme, suppressAttachmentWarnings });
  }

  return {
    get apiUrl() {
      return apiUrl;
    },
    set apiUrl(value: string) {
      apiUrl = value;
      saveCurrentConfig();
    },
    get apiKey() {
      return apiKey;
    },
    set apiKey(value: string) {
      apiKey = value;
      saveCurrentConfig();
    },
    get isConfigured() {
      return apiUrl.length > 0 && apiKey.length > 0;
    },
    get isFirstRun() {
      return !setupCompleted && !apiKey;
    },
    get setupCompleted() {
      return setupCompleted;
    },
    set setupCompleted(value: boolean) {
      setupCompleted = value;
      saveCurrentConfig();
    },
    get theme() {
      return theme;
    },
    set theme(value: ThemeName) {
      theme = value;
      applyTheme(value);
      saveCurrentConfig();
    },
    setTheme(value: ThemeName) {
      theme = value;
      applyTheme(value);
      saveCurrentConfig();
    },
    completeSetup() {
      setupCompleted = true;
      saveCurrentConfig();
    },
    get suppressAttachmentWarnings() {
      return suppressAttachmentWarnings;
    },
    set suppressAttachmentWarnings(value: boolean) {
      suppressAttachmentWarnings = value;
      saveCurrentConfig();
    },
    reset() {
      apiUrl = 'http://localhost:8000';
      apiKey = '';
      setupCompleted = false;
      theme = 'midnight';
      suppressAttachmentWarnings = false;
      applyTheme('midnight');
      saveCurrentConfig();
    }
  };
}

export const configStore = createConfigStore();
