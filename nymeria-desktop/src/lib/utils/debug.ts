export const debugLoggingEnabled = import.meta.env.DEV;

export function debugLog(...args: unknown[]) {
  if (debugLoggingEnabled) {
    console.log(...args);
  }
}
