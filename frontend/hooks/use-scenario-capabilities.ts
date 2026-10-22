'use client';

/**
 * Scenario capabilities for a model version.
 *
 * Capabilities are immutable for a trained model, so they are cached
 * aggressively and keyed by model version: switching the version selector hits
 * a different cache entry rather than refetching the same answer.
 */

import { useQuery, type UseQueryOptions, type UseQueryResult } from '@tanstack/react-query';
import { useMemo } from 'react';

import {
  scenarioApi,
  scenarioKeys,
  type ScenarioApiClient,
} from '@/lib/api/scenario-api';
import {
  SCENARIO_LIMITS,
  type ScenarioCapabilitiesResponse,
  type ScenarioCapabilityStatus,
  type ScenarioType,
} from '@/lib/types/scenario';
import { ScenarioApiError, isRetryableError } from '@/lib/utils/errors';

/** A trained model's capabilities do not change; an hour of staleness is safe. */
export const CAPABILITIES_STALE_TIME_MS = 60 * 60 * 1000;
export const CAPABILITIES_GC_TIME_MS = 2 * 60 * 60 * 1000;
const MAX_RETRIES = 2;

export interface UseScenarioCapabilitiesOptions {
  modelVersion?: string;
  /** Model family; omitted means the deployment default. */
  model?: string;
  enabled?: boolean;
  client?: ScenarioApiClient;
  queryOptions?: Partial<
    Omit<
      UseQueryOptions<ScenarioCapabilitiesResponse, ScenarioApiError>,
      'queryKey' | 'queryFn'
    >
  >;
}

export type UseScenarioCapabilitiesResult = UseQueryResult<
  ScenarioCapabilitiesResponse,
  ScenarioApiError
> & {
  /** Scenario types this model version can serve. */
  supportedScenarioTypes: ScenarioType[];
  /** Capability status by scenario type, for quick lookup in a form. */
  capabilityByType: Partial<Record<ScenarioType, ScenarioCapabilityStatus>>;
  /** Whether one scenario type is usable right now. */
  isScenarioSupported: (scenarioType: ScenarioType) => boolean;
  /** Why a scenario type is unavailable, or null when it is available. */
  unsupportedReason: (scenarioType: ScenarioType) => string | null;
};

/**
 * Fetch and cache scenario capabilities.
 *
 * Only transient failures are retried: a 404 for an unknown model version or a
 * 409 for a model without a feature schema will not resolve by trying again.
 */
export function useScenarioCapabilities(
  options: UseScenarioCapabilitiesOptions = {},
): UseScenarioCapabilitiesResult {
  const {
    modelVersion = SCENARIO_LIMITS.defaultModelVersion,
    model,
    enabled = true,
    client = scenarioApi,
    queryOptions,
  } = options;

  const query = useQuery<ScenarioCapabilitiesResponse, ScenarioApiError>({
    queryKey: scenarioKeys.capabilities(modelVersion, model),
    queryFn: ({ signal }) => client.getScenarioCapabilities(modelVersion, { signal }, model),
    enabled: enabled && Boolean(modelVersion),
    staleTime: CAPABILITIES_STALE_TIME_MS,
    gcTime: CAPABILITIES_GC_TIME_MS,
    retry: (failureCount, error) =>
      failureCount < MAX_RETRIES && isRetryableError(error),
    ...queryOptions,
  });

  const capabilityByType = useMemo(() => {
    const map: Partial<Record<ScenarioType, ScenarioCapabilityStatus>> = {};
    for (const capability of query.data?.capabilities ?? []) {
      map[capability.scenario_type] = capability;
    }
    return map;
  }, [query.data]);

  const supportedScenarioTypes = useMemo(
    () =>
      (query.data?.capabilities ?? [])
        .filter((capability) => capability.supported)
        .map((capability) => capability.scenario_type),
    [query.data],
  );

  return {
    ...query,
    supportedScenarioTypes,
    capabilityByType,
    isScenarioSupported: (scenarioType) =>
      Boolean(capabilityByType[scenarioType]?.supported),
    unsupportedReason: (scenarioType) => {
      const capability = capabilityByType[scenarioType];
      if (!capability || capability.supported) return null;
      return capability.reason ?? 'This scenario is not supported by the selected model.';
    },
  };
}
