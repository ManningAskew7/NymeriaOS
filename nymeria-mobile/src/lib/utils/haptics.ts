/**
 * Haptic feedback utility for Capacitor.
 * Provides light, medium, and heavy impact feedback.
 * Falls back silently in browser mode.
 */

type ImpactStyle = 'light' | 'medium' | 'heavy';

let loadAttempted = false;
// eslint-disable-next-line @typescript-eslint/no-explicit-any
let hapticsModule: any = null;

async function getHaptics() {
  if (loadAttempted) return hapticsModule;
  loadAttempted = true;
  try {
    hapticsModule = await import('@capacitor/haptics');
  } catch {
    hapticsModule = null;
  }
  return hapticsModule;
}

/**
 * Trigger impact haptic feedback.
 * @param style - 'light' (tap), 'medium' (action), 'heavy' (swipe confirm)
 */
export async function hapticImpact(style: ImpactStyle = 'light'): Promise<void> {
  const mod = await getHaptics();
  if (!mod?.Haptics) return;
  try {
    await mod.Haptics.impact({ style: mod.ImpactStyle[style.toUpperCase() as 'LIGHT' | 'MEDIUM' | 'HEAVY'] ?? style });
  } catch {
    // Silently fail
  }
}

/**
 * Trigger notification haptic feedback.
 * @param type - 'success', 'warning', or 'error'
 */
export async function hapticNotification(
  type: 'success' | 'warning' | 'error' = 'success'
): Promise<void> {
  const mod = await getHaptics();
  if (!mod?.Haptics) return;
  try {
    await mod.Haptics.notification({ type: mod.NotificationType[type.toUpperCase() as 'SUCCESS' | 'WARNING' | 'ERROR'] ?? type });
  } catch {
    // Silently fail
  }
}
