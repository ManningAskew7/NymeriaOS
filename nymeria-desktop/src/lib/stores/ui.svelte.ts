import { scopedKey, registerIdentityReloadHook } from './config.svelte';

const STORAGE_KEY_BASE = 'nymeria-ui';
const STORAGE_KEY = () => scopedKey(STORAGE_KEY_BASE);

interface UIState {
  sidebarCollapsed: boolean;
  rightPanelCollapsed: boolean;
  sidebarWidth: number;
  rightPanelWidth: number;
}

export const SIDEBAR_WIDTH_DEFAULT = 310;
export const RIGHT_PANEL_WIDTH_DEFAULT = 320;
export const SIDEBAR_WIDTH_MIN = SIDEBAR_WIDTH_DEFAULT;
export const RIGHT_PANEL_WIDTH_MIN = RIGHT_PANEL_WIDTH_DEFAULT;
export const PANEL_WIDTH_MAX = 640;

function clampSidebarWidth(w: number): number {
  return Math.max(SIDEBAR_WIDTH_MIN, Math.min(PANEL_WIDTH_MAX, w));
}

function clampRightPanelWidth(w: number): number {
  return Math.max(RIGHT_PANEL_WIDTH_MIN, Math.min(PANEL_WIDTH_MAX, w));
}

function loadUIState(): UIState {
  if (typeof localStorage === 'undefined') {
    return {
      sidebarCollapsed: false,
      rightPanelCollapsed: false,
      sidebarWidth: SIDEBAR_WIDTH_DEFAULT,
      rightPanelWidth: RIGHT_PANEL_WIDTH_DEFAULT,
    };
  }

  try {
    const stored = localStorage.getItem(STORAGE_KEY());
    if (stored) {
      const state = JSON.parse(stored);
      return {
        sidebarCollapsed: state.sidebarCollapsed ?? false,
        rightPanelCollapsed: state.rightPanelCollapsed ?? false,
        sidebarWidth: clampSidebarWidth(state.sidebarWidth ?? SIDEBAR_WIDTH_DEFAULT),
        rightPanelWidth: clampRightPanelWidth(state.rightPanelWidth ?? RIGHT_PANEL_WIDTH_DEFAULT),
      };
    }
  } catch (e) {
    console.error('Failed to load UI state:', e);
  }

  return {
    sidebarCollapsed: false,
    rightPanelCollapsed: false,
    sidebarWidth: SIDEBAR_WIDTH_DEFAULT,
    rightPanelWidth: RIGHT_PANEL_WIDTH_DEFAULT,
  };
}

function saveUIState(state: UIState): void {
  if (typeof localStorage === 'undefined') return;

  try {
    localStorage.setItem(STORAGE_KEY(), JSON.stringify(state));
  } catch (e) {
    console.error('Failed to save UI state:', e);
  }
}

function createUIStore() {
  const initial = loadUIState();

  // In Outlook mode, auto-collapse both panels for maximum chat space
  const isOutlookMode = typeof window !== 'undefined'
    && new URLSearchParams(window.location.search).get('outlook') === '1';

  let sidebarCollapsed = $state(isOutlookMode ? true : initial.sidebarCollapsed);
  let rightPanelCollapsed = $state(isOutlookMode ? true : initial.rightPanelCollapsed);
  let sidebarWidth = $state(initial.sidebarWidth);
  let rightPanelWidth = $state(initial.rightPanelWidth);

  // Reload UI prefs when the connected user changes — different users likely
  // have different sidebar/panel preferences.
  registerIdentityReloadHook(() => {
    const next = loadUIState();
    sidebarCollapsed = isOutlookMode ? true : next.sidebarCollapsed;
    rightPanelCollapsed = isOutlookMode ? true : next.rightPanelCollapsed;
    sidebarWidth = next.sidebarWidth;
    rightPanelWidth = next.rightPanelWidth;
  });

  function save() {
    saveUIState({ sidebarCollapsed, rightPanelCollapsed, sidebarWidth, rightPanelWidth });
  }

  return {
    get sidebarCollapsed() {
      return sidebarCollapsed;
    },
    get rightPanelCollapsed() {
      return rightPanelCollapsed;
    },
    get sidebarWidth() {
      return sidebarWidth;
    },
    get rightPanelWidth() {
      return rightPanelWidth;
    },
    toggleSidebar() {
      sidebarCollapsed = !sidebarCollapsed;
      save();
    },
    toggleRightPanel() {
      rightPanelCollapsed = !rightPanelCollapsed;
      save();
    },
    setSidebarWidth(w: number) {
      sidebarWidth = clampSidebarWidth(w);
    },
    setRightPanelWidth(w: number) {
      rightPanelWidth = clampRightPanelWidth(w);
    },
    persistWidths() {
      save();
    },
  };
}

export const uiStore = createUIStore();
