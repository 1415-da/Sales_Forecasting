'use client';

/**
 * Model registry reads and mutations.
 *
 * A mutation invalidates the model list *and* the monitoring tree: promoting a
 * version changes which model serves every forecast, so the monitoring
 * header's "model version" would otherwise keep naming the old one.
 */
import {
  useMutation,
  useQuery,
  useQueryClient,
  type UseQueryResult,
} from '@tanstack/react-query';

import {
  ApiError,
  compareModels,
  getModelArtifacts,
  getModelLineage,
  registryAction,
} from '@/lib/api';
import type {
  ModelArtifacts,
  ModelLineage,
  RegistryActionResult,
} from '@/lib/types/auth';
import type { ModelComparison } from '@/lib/types';

function retry(failureCount: number, error: unknown): boolean {
  if (failureCount >= 2) return false;
  return error instanceof ApiError ? error.isRetryable : false;
}

/** Two versions side by side. Disabled until both are chosen and differ. */
export function useModelComparison(params: {
  versionA?: string;
  versionB?: string;
  model?: string;
}): UseQueryResult<ModelComparison, Error> {
  const ready = Boolean(
    params.versionA && params.versionB && params.versionA !== params.versionB,
  );
  return useQuery<ModelComparison, Error>({
    queryKey: ['registry', 'compare', params],
    queryFn: ({ signal }) =>
      compareModels(
        {
          versionA: params.versionA as string,
          versionB: params.versionB as string,
          ...(params.model ? { model: params.model } : {}),
        },
        signal,
      ),
    enabled: ready,
    staleTime: 5 * 60_000,
    retry,
  });
}

/** Where a version came from. */
export function useModelLineage(
  version: string | undefined,
  model: string | undefined,
  enabled: boolean,
): UseQueryResult<ModelLineage, Error> {
  return useQuery<ModelLineage, Error>({
    queryKey: ['registry', 'lineage', version ?? null, model ?? null],
    queryFn: ({ signal }) => getModelLineage(version as string, model, signal),
    enabled: enabled && Boolean(version),
    staleTime: 5 * 60_000,
    retry,
  });
}

/** Artifact metadata for a version. */
export function useModelArtifacts(
  version: string | undefined,
  model: string | undefined,
  enabled: boolean,
): UseQueryResult<ModelArtifacts, Error> {
  return useQuery<ModelArtifacts, Error>({
    queryKey: ['registry', 'artifacts', version ?? null, model ?? null],
    queryFn: ({ signal }) => getModelArtifacts(version as string, model, signal),
    enabled: enabled && Boolean(version),
    staleTime: 5 * 60_000,
    retry,
  });
}

export interface RegistryMutationInput {
  readonly action: 'promote' | 'archive' | 'rollback';
  readonly version: string;
  readonly model?: string;
  readonly reason?: string;
  readonly archiveExisting?: boolean;
  /** `<model>:<version>`; required by the API for training candidates. */
  readonly expectedIdentity?: string;
}

/**
 * Promote, archive, or roll back.
 *
 * No automatic retry: these change what serves production traffic, and a
 * silent second attempt after an ambiguous failure is the last thing anyone
 * wants.
 */
export function useRegistryMutation() {
  const queryClient = useQueryClient();

  return useMutation<RegistryActionResult, Error, RegistryMutationInput>({
    mutationFn: ({ action, version, ...rest }) =>
      registryAction(action, version, rest),
    retry: false,
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: ['models'] });
      queryClient.invalidateQueries({ queryKey: ['registry'] });
      queryClient.invalidateQueries({ queryKey: ['monitoring'] });
      queryClient.invalidateQueries({ queryKey: ['health'] });
      queryClient.invalidateQueries({ queryKey: ['training'] });
      // A new Production version can change which scenario levers exist.
      queryClient.invalidateQueries({ queryKey: ['scenario'] });
    },
  });
}
