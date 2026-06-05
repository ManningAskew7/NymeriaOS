import type { LLMProviderSpec, ProviderRoute } from '$lib/types';

const FALLBACK_DUAL_ROUTE_PROVIDERS: Record<string, { routes: ProviderRoute[]; defaultRoute: ProviderRoute }> = {
  google: { routes: ['native', 'openai_compat'], defaultRoute: 'native' },
  ollama: { routes: ['native', 'openai_compat'], defaultRoute: 'native' },
};

export function providerSpecFor(
  catalog: LLMProviderSpec[],
  provider: string
): LLMProviderSpec | undefined {
  const target = provider.trim().toLowerCase();
  return catalog.find((spec) => spec.id === target || spec.aliases?.includes(target));
}

export function supportedRoutesForProvider(
  provider: string,
  catalog: LLMProviderSpec[]
): ProviderRoute[] {
  const spec = providerSpecFor(catalog, provider);
  if (spec?.supported_routes?.length) return spec.supported_routes;
  return FALLBACK_DUAL_ROUTE_PROVIDERS[provider]?.routes ?? [];
}

export function defaultRouteForProvider(
  provider: string,
  catalog: LLMProviderSpec[]
): ProviderRoute {
  const spec = providerSpecFor(catalog, provider);
  if (spec?.default_route) return spec.default_route;
  return FALLBACK_DUAL_ROUTE_PROVIDERS[provider]?.defaultRoute ?? 'native';
}

export function hasRouteChoice(provider: string, catalog: LLMProviderSpec[]): boolean {
  return supportedRoutesForProvider(provider, catalog).length > 1;
}

export function coerceProviderRoute(
  provider: string,
  catalog: LLMProviderSpec[],
  route: ProviderRoute | 'default' | '' | null | undefined
): ProviderRoute {
  const routes = supportedRoutesForProvider(provider, catalog);
  const fallback = defaultRouteForProvider(provider, catalog);
  if (!routes.length) return fallback;
  return route && route !== 'default' && routes.includes(route) ? route : fallback;
}

export function providerRouteLabel(route: ProviderRoute): string {
  if (route === 'openai_compat') return 'OpenAI-compatible API';
  if (route === 'anthropic_messages') return 'Anthropic Messages (native thinking)';
  return 'Native package';
}
