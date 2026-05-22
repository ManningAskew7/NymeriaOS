import type { LLMProviderSpec, ProviderTier } from '$lib/types';

type ProviderOption = {
  value: string;
  label: string;
  tier?: ProviderTier;
  notesForUser?: string;
};

type ProviderGroup = {
  label: string;
  tier: ProviderTier;
  options: ProviderOption[];
};

const TIER_GROUP_LABELS: Record<ProviderTier, string> = {
  native: 'Native reasoning',
  gateway: 'Gateway',
  unverified: 'Unverified',
};

const TIER_ORDER: ProviderTier[] = ['native', 'gateway', 'unverified'];

/**
 * Bucket every catalog spec by its tier and surface notes_for_user as the
 * warning sub-line in the picker. Mobile-flavored: no synthetic display
 * providers (no anthropic_proxy / openai_custom variants like desktop has),
 * so this is the catalog as-is.
 */
export function buildMobileProviderGroups(
  catalog: LLMProviderSpec[]
): ProviderGroup[] {
  const buckets: Record<ProviderTier, ProviderOption[]> = {
    native: [],
    gateway: [],
    unverified: [],
  };
  for (const spec of catalog) {
    const tier = (spec.tier as ProviderTier | undefined) ?? 'unverified';
    buckets[tier].push({
      value: spec.id,
      label: spec.label,
      tier,
      notesForUser: spec.notes_for_user ?? '',
    });
  }
  return TIER_ORDER.map((tier) => ({
    label: TIER_GROUP_LABELS[tier],
    tier,
    options: buckets[tier],
  })).filter((g) => g.options.length > 0);
}
