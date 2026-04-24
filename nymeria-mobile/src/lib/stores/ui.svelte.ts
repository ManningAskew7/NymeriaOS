/**
 * Mobile UI Store — manages the three-panel swipe layout state.
 *
 * Panels: 'left' (threads/settings), 'chat' (default), 'right' (dashboard).
 * The scroll-snap container drives the visual position; this store tracks
 * which panel is logically active for header/input bar behavior.
 */

import { scopedKey, registerIdentityReloadHook } from './config.svelte';

export type ActivePanel = 'left' | 'chat' | 'right';

const STORAGE_KEY_BASE = 'nymeria-ui-mobile';
const STORAGE_KEY = () => scopedKey(STORAGE_KEY_BASE);

interface MobileUIState {
  activePanel: ActivePanel;
  keyboardVisible: boolean;
  keyboardHeight: number;
}

function loadUIState(): MobileUIState {
  if (typeof localStorage === 'undefined') {
    return { activePanel: 'chat', keyboardVisible: false, keyboardHeight: 0 };
  }

  try {
    const stored = localStorage.getItem(STORAGE_KEY());
    if (stored) {
      const state = JSON.parse(stored);
      return {
        activePanel: state.activePanel ?? 'chat',
        keyboardVisible: false,
        keyboardHeight: 0,
      };
    }
  } catch (e) {
    console.error('Failed to load mobile UI state:', e);
  }

  return { activePanel: 'chat', keyboardVisible: false, keyboardHeight: 0 };
}

function saveUIState(state: Pick<MobileUIState, 'activePanel'>): void {
  if (typeof localStorage === 'undefined') return;

  try {
    localStorage.setItem(STORAGE_KEY(), JSON.stringify({ activePanel: state.activePanel }));
  } catch (e) {
    console.error('Failed to save mobile UI state:', e);
  }
}

function createUIStore() {
  const initial = loadUIState();
  let activePanel = $state<ActivePanel>(initial.activePanel);
  let keyboardVisible = $state(false);
  let keyboardHeight = $state(0);
  let scrollContainer = $state<HTMLElement | null>(null);

  // Reload UI prefs when the connected user changes.
  registerIdentityReloadHook(() => {
    const next = loadUIState();
    activePanel = next.activePanel;
  });

  function save() {
    saveUIState({ activePanel });
  }

  return {
    get activePanel() {
      return activePanel;
    },

    get keyboardVisible() {
      return keyboardVisible;
    },

    get keyboardHeight() {
      return keyboardHeight;
    },

    get scrollContainer() {
      return scrollContainer;
    },

    /** Register the scroll-snap container element for programmatic scrolling */
    setScrollContainer(el: HTMLElement | null) {
      scrollContainer = el;
    },

    /** Update panel from scroll position (called by scroll event handler) */
    syncFromScroll(scrollLeft: number, containerWidth: number) {
      const panelIndex = Math.round(scrollLeft / containerWidth);
      const panels: ActivePanel[] = ['left', 'chat', 'right'];
      const newPanel = panels[panelIndex] ?? 'chat';
      if (newPanel !== activePanel) {
        activePanel = newPanel;
        save();
      }
    },

    /** Programmatically navigate to a panel */
    goToPanel(panel: ActivePanel) {
      activePanel = panel;
      save();
      if (scrollContainer) {
        const index = panel === 'left' ? 0 : panel === 'chat' ? 1 : 2;
        scrollContainer.scrollTo({
          left: index * scrollContainer.clientWidth,
          behavior: 'smooth',
        });
      }
    },

    /** Navigate to chat panel (convenience) */
    goToChat() {
      this.goToPanel('chat');
    },

    /** Update keyboard state (called from Capacitor keyboard plugin) */
    setKeyboard(visible: boolean, height: number = 0) {
      keyboardVisible = visible;
      keyboardHeight = height;
      if (typeof document !== 'undefined') {
        document.documentElement.style.setProperty('--keyboard-height', `${height}px`);
      }
    },
  };
}

export const uiStore = createUIStore();
