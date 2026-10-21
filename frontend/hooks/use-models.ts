'use client';

/**
 * Model discovery.
 *
 * Model metadata changes only when the training DAG promotes a version, so it
 * is cached for minutes rather than refetched per interaction. The health query
 * is separate and deliberately shorter-lived: it is what tells the console the
 * API is reachable at all.
 */
import { useQuery, type UseQueryResult } from '@tanstack/react-query';

import { ApiError, getHealth, getModels } from '@/lib/api';
import type { HealthReport, ModelInfo, ModelList } from '@/lib/types';

const RETRYABLE_ATTEMPTS = 2;

/** Only transport failures retry; a 404 will not resolve itself. */
function retry(failureCount: number, error: unknown): boolean {
  if (failureCount >= RETRYABLE_ATTEMPTS) return false;
  return error instanceof ApiError ? error.isRetryable : false;
}

/**
 * The query result plus the derived values every caller would otherwise
 * recompute.
 *
 * An intersection rather than `interface extends`: `UseQueryResult` is a
 * discriminated union, and an interface cannot extend one.
 */
export type UseModelsResult = UseQueryResult<ModelList, Error> & {
  /** Models that can serve a forecast right now. */
  readonly productionModels: ModelInfo[];
  /** The model the selector should start on. */
  readonly defaultModel: ModelInfo | undefined;
  /** Stores any listed model was trained on. */
  readonly stores: string[];
  /** True once the registry is known to hold nothing. */
  readonly isEmpty: boolean;
};

/** The model version serving each configured model. */
export function useModels(): UseModelsResult {
  const query = useQuery<ModelList, Error>({
    queryKey: ['models'],
    queryFn: ({ signal }) => getModels(signal),
    staleTime: 2 * 60_000,
    retry,
  });

  const models = query.data?.models ?? [];
  const productionModels = models.filter((model) => model.is_production);
  const defaultModel =
    models.find((model) => model.is_default) ?? productionModels[0] ?? models[0];

  return {
    ...query,
    productionModels,
    defaultModel,
    stores: query.data?.supported_stores ?? [],
    isEmpty: query.isSuccess && models.length === 0,
  };
}

/** API health, dependency state, and the current Production model. */
export function useHealth(): UseQueryResult<HealthReport, Error> {
  return useQuery<HealthReport, Error>({
    queryKey: ['health'],
    queryFn: ({ signal }) => getHealth(signal),
    // Short, because this is the console's "is the backend up" signal.
    staleTime: 30_000,
    refetchInterval: 60_000,
    retry,
  });
}
