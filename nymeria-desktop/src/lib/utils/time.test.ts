import { describe, it, expect } from 'vitest';
import { formatMessageTime, formatRelativeTime } from './time';

// Dates are built with the local-time constructor so they line up with the
// getHours()/getMinutes() the formatter reads — deterministic across timezones.

describe('formatMessageTime', () => {
  it('uses 12-hour clock, no leading zero, uppercase meridiem', () => {
    expect(formatMessageTime(new Date(2026, 0, 1, 18, 12))).toBe('6:12 PM');
  });

  it('zero-pads the minutes', () => {
    expect(formatMessageTime(new Date(2026, 0, 1, 9, 5))).toBe('9:05 AM');
  });

  it('renders midnight as 12 AM and noon as 12 PM', () => {
    expect(formatMessageTime(new Date(2026, 0, 1, 0, 0))).toBe('12:00 AM');
    expect(formatMessageTime(new Date(2026, 0, 1, 12, 0))).toBe('12:00 PM');
  });
});

describe('formatRelativeTime', () => {
  const now = new Date(2026, 0, 10, 12, 0, 0);

  it('returns "Just now" under a minute', () => {
    expect(formatRelativeTime(new Date(2026, 0, 10, 11, 59, 30), now)).toBe('Just now');
  });

  it('reports minutes, then hours, then days at each threshold', () => {
    expect(formatRelativeTime(new Date(2026, 0, 10, 11, 25), now)).toBe('35m ago');
    expect(formatRelativeTime(new Date(2026, 0, 10, 9, 0), now)).toBe('3h ago');
    expect(formatRelativeTime(new Date(2026, 0, 8, 12, 0), now)).toBe('2d ago');
  });
});
