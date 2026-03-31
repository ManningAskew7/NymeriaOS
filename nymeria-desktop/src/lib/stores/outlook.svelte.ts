/**
 * Office.js bridge for Outlook add-in integration.
 * Reads the current email's ID when running inside Outlook's taskpane.
 * No-ops gracefully when running in a regular browser or Tauri.
 *
 * Office.js is loaded dynamically AFTER SvelteKit boots to avoid
 * interfering with the router (Office.js overwrites history API).
 */

declare const Office: any;

function createOutlookStore() {
  // Detect Outlook mode from URL param
  const urlParams = typeof window !== 'undefined'
    ? new URLSearchParams(window.location.search)
    : null;
  const outlookParam = urlParams?.get('outlook') === '1';

  let isOutlook = $state(false);
  let currentEmailId = $state<string | null>(null);
  let currentEmailSubject = $state<string | null>(null);
  let initialized = $state(false);

  function readCurrentItem() {
    try {
      const item = Office.context?.mailbox?.item;
      if (!item) {
        currentEmailId = null;
        currentEmailSubject = null;
        return;
      }

      // Convert EWS ID to REST/Graph format
      const ewsId = item.itemId;
      if (ewsId && Office.context.mailbox.convertToRestId) {
        currentEmailId = Office.context.mailbox.convertToRestId(
          ewsId,
          Office.MailboxEnums.RestVersion.v2_0
        );
      } else {
        currentEmailId = ewsId || null;
      }

      currentEmailSubject = item.subject || null;
    } catch (e) {
      console.warn('[Outlook] Failed to read current item:', e);
      currentEmailId = null;
      currentEmailSubject = null;
    }
  }

  function loadOfficeJs(): Promise<void> {
    return new Promise((resolve, reject) => {
      // Already loaded?
      if (typeof Office !== 'undefined' && Office.onReady) {
        resolve();
        return;
      }
      const script = document.createElement('script');
      script.src = 'https://appsforoffice.microsoft.com/lib/1.1/hosted/office.js';
      script.onload = () => resolve();
      script.onerror = () => reject(new Error('Failed to load Office.js'));
      document.head.appendChild(script);
    });
  }

  async function initialize() {
    if (initialized || !outlookParam) return;
    initialized = true;

    try {
      await loadOfficeJs();
    } catch (e) {
      console.warn('[Outlook] Could not load Office.js:', e);
      return;
    }

    if (typeof Office === 'undefined' || !Office.onReady) {
      console.warn('[Outlook] Office.js loaded but Office.onReady not available');
      return;
    }

    Office.onReady((info: any) => {
      if (info.host === Office.HostType?.Outlook || info.host === 'Outlook') {
        console.log('[Outlook] Running inside Outlook, reading email context');
        isOutlook = true;
        readCurrentItem();

        // Listen for email selection changes
        try {
          Office.context.mailbox.addHandlerAsync(
            Office.EventType.ItemChanged,
            () => readCurrentItem()
          );
        } catch (e) {
          console.warn('[Outlook] Could not register ItemChanged handler:', e);
        }
      } else {
        console.log('[Outlook] Office.js loaded but not in Outlook host:', info.host);
      }
    });
  }

  return {
    get isOutlook() { return isOutlook; },
    get isOutlookMode() { return outlookParam; },
    get currentEmailId() { return currentEmailId; },
    get currentEmailSubject() { return currentEmailSubject; },
    initialize,
  };
}

export const outlookStore = createOutlookStore();
