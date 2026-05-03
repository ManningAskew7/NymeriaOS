/**
 * Unique ID for this client session.
 *
 * Sent with API requests and the autonomous SSE subscription so the backend can
 * filter sync events that originated from this same client.
 */
export const clientId = crypto.randomUUID();
