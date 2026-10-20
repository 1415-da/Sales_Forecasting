'use client';

/**
 * Roll back a scenario.
 *
 * A mutation, and the one place that invalidates scenario queries: a rollback
 * changes a scenario's status and appends an audit event, so any cached detail
 * or audit view is stale the moment it succeeds.
 */

import {
  useMutation,
  useQueryClient,
  type QueryClient,
  type UseMutationOptions,
  type UseMutationResult,
} from '@tanstack/react-query';
import { useCallback } from 'react';

import { scenarioApi, scenarioKeys, type ScenarioApiClient } from '@/lib/api/scenario-api';
import type { ScenarioRollbackResponse } from '@/lib/types/scenario';
import {
  ScenarioApiError,
  reportError,
  type ErrorPresentation,
} from '@/lib/utils/errors';

/** The API requires a reason of at least 3 characters. */
export const MIN_ROLLBACK_REASON_LENGTH = 3;

/**
 * Invalidate the caches a rollback makes stale.
 *
 * Audit history always changes and the detail view's status does too.
 * Capabilities are keyed by model version and unaffected, so they are
 * deliberately left in place -- dropping them would force a needless refetch
 * of data that cannot have changed.
 *
 * Extracted so the invalidation contract is testable without a renderer.
 */
export async function invalidateScenarioCaches(
  queryClient: QueryClient,
  scenarioId: string,
  options: { removeScenarioFromCache?: boolean } = {},
): Promise<void> {
  await queryClient.invalidateQueries({ queryKey: scenarioKeys.audit(scenarioId) });
  if (options.removeScenarioFromCache) {
    queryClient.removeQueries({ queryKey: scenarioKeys.detail(scenarioId) });
  } else {
    await queryClient.invalidateQueries({ queryKey: scenarioKeys.detail(scenarioId) });
  }
}

export interface ScenarioRollbackVariables {
  scenarioId: string;
  reason: string;
  requestId?: string;
}

export interface UseScenarioRollbackOptions {
  client?: ScenarioApiClient;
  showErrorToast?: boolean;
  /** Also drop the cached comparison. Off by default: the row still exists. */
  removeScenarioFromCache?: boolean;
  mutationOptions?: Partial<
    Omit<
      UseMutationOptions<
        ScenarioRollbackResponse,
        ScenarioApiError,
        ScenarioRollbackVariables
      >,
      'mutationFn'
    >
  >;
}

export type UseScenarioRollbackResult = UseMutationResult<
  ScenarioRollbackResponse,
  ScenarioApiError,
  ScenarioRollbackVariables
> & {
  rollback: (scenarioId: string, reason: string) => Promise<ScenarioRollbackResponse>;
  errorPresentation: ErrorPresentation | null;
};

/** Whether a reason satisfies the API's minimum before submitting. */
export function isValidRollbackReason(reason: string): boolean {
  return reason.trim().length >= MIN_ROLLBACK_REASON_LENGTH;
}

export function useScenarioRollback(
  options: UseScenarioRollbackOptions = {},
): UseScenarioRollbackResult {
  const {
    client = scenarioApi,
    showErrorToast = true,
    removeScenarioFromCache = false,
    mutationOptions,
  } = options;
  const queryClient = useQueryClient();

  const mutation = useMutation<
    ScenarioRollbackResponse,
    ScenarioApiError,
    ScenarioRollbackVariables
  >({
    mutationFn: ({ scenarioId, reason, requestId }) =>
      client.rollbackScenario(scenarioId, reason, { requestId }),
    retry: false,
    // Forwarded with `...args` for version-agnostic callback arity.
    onSuccess: async (...args) => {
      const [data, variables] = args;
      const scenarioId = data.scenario_id ?? variables.scenarioId;
      await invalidateScenarioCaches(queryClient, scenarioId, {
        removeScenarioFromCache,
      });
      return mutationOptions?.onSuccess?.(...args);
    },
    onError: (...args) => {
      if (showErrorToast) reportError(args[0]);
      return mutationOptions?.onError?.(...args);
    },
    ...mutationOptions,
  });

  const rollback = useCallback(
    (scenarioId: string, reason: string) =>
      mutation.mutateAsync({ scenarioId, reason }),
    [mutation],
  );

  return {
    ...mutation,
    rollback,
    errorPresentation: mutation.error ? mutation.error.toPresentation() : null,
  };
}
