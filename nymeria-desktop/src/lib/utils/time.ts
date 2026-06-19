// Centralized time formatting for the frontend. One definition per role so the
// format can't drift between components (previously every component re-rolled its
// own `toLocaleTimeString` / "time ago" ladder).

/**
 * Absolute clock time for chat messages: "6:12 PM" (12-hour, no leading zero,
 * uppercase meridiem). Computed from the date's parts rather than
 * `toLocaleTimeString`, so it is identical regardless of the machine's locale
 * (which is what produced the inconsistent "06:12 pm").
 */
export function formatMessageTime(date: Date): string {
  let hours = date.getHours();
  const minutes = date.getMinutes();
  const meridiem = hours < 12 ? 'AM' : 'PM';
  hours = hours % 12 || 12;
  return `${hours}:${String(minutes).padStart(2, '0')} ${meridiem}`;
}

/**
 * Relative "time ago" for the Activity feed: "Just now", "5m ago", "3h ago",
 * "2d ago". Reproduces the feed's existing ladder exactly. `now` is injectable
 * for testing.
 */
export function formatRelativeTime(date: Date, now: Date = new Date()): string {
  const minutes = Math.floor((now.getTime() - date.getTime()) / 60000);
  const hours = Math.floor(minutes / 60);
  const days = Math.floor(hours / 24);
  if (days > 0) return `${days}d ago`;
  if (hours > 0) return `${hours}h ago`;
  if (minutes > 0) return `${minutes}m ago`;
  return 'Just now';
}
