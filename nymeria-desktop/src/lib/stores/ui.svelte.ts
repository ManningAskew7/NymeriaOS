const STORAGE_KEY = 'nymeria-ui';

interface UIState {
  sidebarCollapsed: boolean;
  rightPanelCollapsed: boolean;
}

function loadUIState(): UIState {
  if (typeof localStorage === 'undefined') {
    return { sidebarCollapsed: false, rightPanelCollapsed: false };
  }

  try {
    const stored = localStorage.getItem(STORAGE_KEY);
    if (stored) {
      const state = JSON.parse(stored);
      return {
        sidebarCollapsed: state.sidebarCollapsed ?? false,
        rightPanelCollapsed: state.rightPanelCollapsed ?? false,
      };
    }
  } catch (e) {
    console.error('Failed to load UI state:', e);
  }

  return { sidebarCollapsed: false, rightPanelCollapsed: false };
}

function saveUIState(state: UIState): void {
  if (typeof localStorage === 'undefined') return;

  try {
    localStorage.setItem(STORAGE_KEY, JSON.stringify(state));
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

  function save() {
    saveUIState({ sidebarCollapsed, rightPanelCollapsed });
  }

  return {
    get sidebarCollapsed() {
      return sidebarCollapsed;
    },
    get rightPanelCollapsed() {
      return rightPanelCollapsed;
    },
    toggleSidebar() {
      sidebarCollapsed = !sidebarCollapsed;
      save();
    },
    toggleRightPanel() {
      rightPanelCollapsed = !rightPanelCollapsed;
      save();
    },
  };
}

export const uiStore = createUIStore();
