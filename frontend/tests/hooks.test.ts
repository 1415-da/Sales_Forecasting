/**
 * Cache-behaviour tests for the scenario hooks.
 *
 * These drive a real `QueryClient` rather than rendering components: the parts
 * worth pinning down are the caching and invalidation contracts, which live in
 * exported helpers and query keys, not in React plumbing.
 */

import { QueryClient } from '@tanstack/react-query';
import { describe, expect, it, vi } from 'vitest';

import { createScenarioApiClient, scenarioKeys } from '@/lib/api/scenario-api';
import { seedScenarioDetail } from '@/hooks/use-scenario-forecast';
import {
  MIN_ROLLBACK_REASON_LENGTH,
  invalidateScenarioCaches,
  isValidRollbackReason,
} from '@/hooks/use-scenario-rollback';
import {
  CAPABILITIES_STALE_TIME_MS,
} from '@/hooks/use-scenario-capabilities';
import type { ScenarioComparisonResponse } from '@/lib/types/scenario';

import { AUDIT_PAYLOAD, CAPABILITIES_PAYLOAD, COMPARISON_PAYLOAD, stubFetch } from './fixtures';

function queryClient(): QueryClient {
  return new QueryClient({
    defaultOptions: { queries: { retry: false }, mutations: { retry: false } },
  });
}

describe('capabilities caching', () => {
  it('caches by model version and does not refetch within the stale window', async () => {
    const { impl, calls } = stubFetch([{ body: CAPABILITIES_PAYLOAD }]);
    const api = createScenarioApiClient({ baseUrl: 'http://api.test', fetchImpl: impl });
    const client = queryClient();

    const fetchFor = (version: string) =>
      client.fetchQuery({
        queryKey: scenarioKeys.capabilities(version),
        queryFn: () => api.getScenarioCapabilities(version),
        staleTime: CAPABILITIES_STALE_TIME_MS,
      });

    await fetchFor('14');
    await fetchFor('14');

    expect(calls).toHaveLength(1);
    expect(client.getQueryData(scenarioKeys.capabilities('14'))).toBeDefined();
  });

  it('keeps a separate cache entry per model version', async () => {
    const { impl, calls } = stubFetch([{ body: CAPABILITIES_PAYLOAD }]);
    const api = createScenarioApiClient({ baseUrl: 'http://api.test', fetchImpl: impl });
    const client = queryClient();

    for (const version of ['14', '21']) {
      await client.fetchQuery({
        queryKey: scenarioKeys.capabilities(version),
        queryFn: () => api.getScenarioCapabilities(version),
        staleTime: CAPABILITIES_STALE_TIME_MS,
      });
    }

    expect(calls).toHaveLength(2);
    expect(client.getQueryData(scenarioKeys.capabilities('14'))).toBeDefined();
    expect(client.getQueryData(scenarioKeys.capabilities('21'))).toBeDefined();
  });
});

describe('forecast cache seeding', () => {
  it('primes the detail cache so navigating to a result is instant', () => {
    const client = queryClient();
    const comparison = COMPARISON_PAYLOAD as unknown as ScenarioComparisonResponse;

    seedScenarioDetail(client, comparison);

    expect(client.getQueryData(scenarioKeys.detail('scn_abc123'))).toBe(comparison);
  });
});

describe('rollback invalidation', () => {
  it('invalidates the detail and audit queries for the scenario', async () => {
    const client = queryClient();
    client.setQueryData(scenarioKeys.detail('scn_abc123'), COMPARISON_PAYLOAD);
    client.setQueryData(scenarioKeys.audit('scn_abc123'), AUDIT_PAYLOAD);
    const invalidate = vi.spyOn(client, 'invalidateQueries');

    await invalidateScenarioCaches(client, 'scn_abc123');

    expect(invalidate).toHaveBeenCalledWith({
      queryKey: scenarioKeys.audit('scn_abc123'),
    });
    expect(invalidate).toHaveBeenCalledWith({
      queryKey: scenarioKeys.detail('scn_abc123'),
    });
    // Invalidated, not dropped: the data is still there, just stale.
    expect(client.getQueryData(scenarioKeys.detail('scn_abc123'))).toBeDefined();
    expect(
      client.getQueryState(scenarioKeys.detail('scn_abc123'))?.isInvalidated,
    ).toBe(true);
  });

  it('can remove the cached comparison when asked', async () => {
    const client = queryClient();
    client.setQueryData(scenarioKeys.detail('scn_abc123'), COMPARISON_PAYLOAD);

    await invalidateScenarioCaches(client, 'scn_abc123', {
      removeScenarioFromCache: true,
    });

    expect(client.getQueryData(scenarioKeys.detail('scn_abc123'))).toBeUndefined();
  });

  it('leaves capabilities untouched, since a rollback cannot change them', async () => {
    const client = queryClient();
    client.setQueryData(scenarioKeys.capabilities('14'), CAPABILITIES_PAYLOAD);

    await invalidateScenarioCaches(client, 'scn_abc123');

    expect(
      client.getQueryState(scenarioKeys.capabilities('14'))?.isInvalidated,
    ).toBe(false);
  });

  it('does not touch another scenario', async () => {
    const client = queryClient();
    client.setQueryData(scenarioKeys.detail('scn_other'), COMPARISON_PAYLOAD);

    await invalidateScenarioCaches(client, 'scn_abc123');

    expect(client.getQueryState(scenarioKeys.detail('scn_other'))?.isInvalidated).toBe(
      false,
    );
  });
});

describe('rollback reason validation', () => {
  it('mirrors the API minimum length', () => {
    expect(MIN_ROLLBACK_REASON_LENGTH).toBe(3);
    expect(isValidRollbackReason('ok')).toBe(false);
    expect(isValidRollbackReason('   ')).toBe(false);
    expect(isValidRollbackReason('assumptions were wrong')).toBe(true);
  });
});
