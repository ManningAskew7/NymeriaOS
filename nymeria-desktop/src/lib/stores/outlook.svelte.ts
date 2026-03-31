/**
 * Office.js bridge for Outlook add-in integration.
 * Reads the current email's metadata when running inside Outlook's taskpane.
 * No-ops gracefully when running in a regular browser or Tauri.
 *
 * Office.js is loaded dynamically AFTER SvelteKit boots to avoid
 * interfering with the router (Office.js overwrites history API).
 *
 * Note: EWS item IDs from Office.js do NOT match Graph API message IDs,
 * so we capture email metadata (subject, sender, date) for the agent to
 * search with instead of passing raw IDs.
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
  let currentEmailSender = $state<string | null>(null);
  let currentEmailDate = $state<string | null>(null);
  let initialized = $state(false);

  function readCurrentItem() {
    try {
      const item = Office.context?.mailbox?.item;
      if (!item) {
        currentEmailId = null;
        currentEmailSubject = null;
        currentEmailSender = null;
        currentEmailDate = null;
        return;
      }

      // Capture the EWS ID (may not work with Graph API, but include as fallback)
      const ewsId = item.itemId;
      if (ewsId && Office.context.mailbox.convertToRestId) {
        try {
          currentEmailId = Office.context.mailbox.convertToRestId(
            ewsId,
            Office.MailboxEnums.RestVersion.v2_0
          );
        } catch {
          currentEmailId = ewsId || null;
        }
      } else {
        currentEmailId = ewsId || null;
      }

      currentEmailSubject = item.subject || null;

      // Get sender
      if (item.from) {
        currentEmailSender = item.from.emailAddress || item.from.displayName || null;
      } else {
        currentEmailSender = null;
      }

      // Get date
      if (item.dateTimeCreated) {
        const d = item.dateTimeCreated;
        currentEmailDate = d instanceof Date ? d.toISOString().slice(0, 10) : null;
      } else {
        currentEmailDate = null;
      }
    } catch (e) {
      console.warn('[Outlook] Failed to read current item:', e);
      currentEmailId = null;
      currentEmailSubject = null;
      currentEmailSender = null;
      currentEmailDate = null;
    }
  }

  function loadOfficeJs(): Promise<void> {
    return new Promise((resolve, reject) => {
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

  /**
   * Build a context string the agent can use to find this email.
   * Includes subject, sender, and date — the agent uses outlook_search_emails
   * to locate the exact email rather than relying on potentially mismatched IDs.
   */
  function getEmailContext(): string {
    const parts: string[] = [];
    if (currentEmailSubject) parts.push(`Subject: "${currentEmailSubject}"`);
    if (currentEmailSender) parts.push(`From: ${currentEmailSender}`);
    if (currentEmailDate) parts.push(`Date: ${currentEmailDate}`);
    if (currentEmailId) parts.push(`Email ID (may need search fallback): ${currentEmailId}`);
    return parts.join('\n');
  }

  return {
    get isOutlook() { return isOutlook; },
    get isOutlookMode() { return outlookParam; },
    get currentEmailId() { return currentEmailId; },
    get currentEmailSubject() { return currentEmailSubject; },
    get currentEmailSender() { return currentEmailSender; },
    get currentEmailDate() { return currentEmailDate; },
    get hasEmail() { return !!(currentEmailSubject || currentEmailId); },
    getEmailContext,
    initialize,
  };
}

export const outlookStore = createOutlookStore();
