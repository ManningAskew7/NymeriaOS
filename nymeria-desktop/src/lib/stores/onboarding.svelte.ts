/**
 * Onboarding session flag.
 *
 * The setup surface must stay mounted while the user works through it, even
 * though adopting a verified connection flips `configStore.needsSetup` to
 * false mid-session (the old wizard avoided this by not persisting anything
 * until its final step, which meant the LLM/settings sections could never use
 * the real API client). The root route renders the surface while EITHER
 * `needsSetup` OR this flag is true; the surface arms the flag when it mounts
 * and clears it on Finish / Skip.
 */
function createOnboardingStore() {
  let active = $state(false);

  return {
    get active() {
      return active;
    },
    /** Called by the setup surface as it mounts (needsSetup was true). */
    begin() {
      active = true;
    },
    /** Finish or skip: hand the session over to the app shell. */
    finish() {
      active = false;
    },
  };
}

export const onboardingStore = createOnboardingStore();
