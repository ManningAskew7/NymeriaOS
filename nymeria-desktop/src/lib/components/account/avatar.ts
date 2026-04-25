// Helpers for rendering account avatars and identity strings.
// Initials come from display_name (first + last initial) when present, falling
// back to the first two characters of the email local-part. Background colour
// is a deterministic HSL hue derived from the user id so the same account
// always renders the same swatch.

import type { AccountIdentity } from '$lib/types';

function hashString(input: string): number {
  let h = 0;
  for (let i = 0; i < input.length; i++) {
    h = (h * 31 + input.charCodeAt(i)) | 0;
  }
  return Math.abs(h);
}

export function avatarInitials(identity: Pick<AccountIdentity, 'display_name' | 'email'> | null | undefined): string {
  if (!identity) return '?';
  const name = (identity.display_name || '').trim();
  if (name) {
    const parts = name.split(/\s+/).filter(Boolean);
    if (parts.length >= 2) {
      return (parts[0][0] + parts[parts.length - 1][0]).toUpperCase();
    }
    return parts[0].slice(0, 2).toUpperCase();
  }
  const local = (identity.email || '').split('@')[0] || '';
  return (local.slice(0, 2) || '?').toUpperCase();
}

export function avatarBackground(id: string | undefined | null): string {
  // 8 hand-picked hues that read well over Nymeria's dark themes. Hash the id
  // to a stable index so the same account is always the same colour.
  const hues = [205, 165, 280, 25, 340, 195, 95, 260];
  const idx = hashString(id || 'unknown') % hues.length;
  return `hsl(${hues[idx]} 55% 42%)`;
}

export function avatarTextColour(): string {
  return '#ffffff';
}

export function identityDisplayName(identity: AccountIdentity | null | undefined): string {
  if (!identity) return 'Not signed in';
  return identity.display_name || identity.email || 'Unknown';
}

export function identitySecondary(identity: AccountIdentity | null | undefined): string {
  if (!identity) return '';
  // Show email as the secondary line, unless the display name *is* the email.
  if (!identity.display_name || identity.display_name === identity.email) return '';
  return identity.email;
}
