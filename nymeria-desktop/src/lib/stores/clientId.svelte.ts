/**
 * Unique client identity for cross-client sync deduplication.
 *
 * Each browser tab / Outlook taskpane / desktop window gets its own ID.
 * This is NOT stored in localStorage — each tab must be independently
 * identifiable so the backend can filter out events that originated
 * from the same client.
 */
export const clientId = crypto.randomUUID();
