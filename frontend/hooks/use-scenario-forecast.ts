'use client';

/**
 * Run a what-if comparison.
 *
 * A mutation rather than a query: it is a non-idempotent POST that runs model
 * inference. Each submission carries a fresh `Idempotency-Key` so an automatic
 * retry or a double-click replays the original result instead of paying for a
 * second inference.
 */

import {
  useMutation,
  useQueryClient,
  type QueryClient,
  type UseMutationOptions,
  type UseMutationResult,
} from '@tanstack/react-query';
import { useCallback, useRef } from 'react';

import {
  generateIdempotencyKey,
  scenarioApi,
  scenarioKeys,
  type ScenarioApiClient,
} from '@/lib/api/scenario-api';
import type {
  ScenarioComparisonResponse,
  ScenarioRequestInput,
} from '@/lib/types/scenario';
import {
  ScenarioApiError,
  reportError,
  toFieldErrors,
  type ErrorPresentation,
} from '@/lib/utils/errors';

/**
 * Seed the detail cache with a fresh comparison.
 *
 * Extracted so the cache behaviour is testable without rendering a component,
 * and so a server component can prime the cache the same way.
 */
export function seedScenarioDetail(
  queryClient: QueryClient,
  comparison: ScenarioComparisonResponse,
): void {
  queryClient.setQueryData(scenarioKeys.detail(comparison.scenario_id), comparison);
}

export interface ScenarioForecastVariables {
  request: ScenarioRequestInput;
  /**
   * Reuse a key to make a retry idempotent. Omit it and the hook mints one per
   * submission, which is what makes TanStack Query's own retries safe.
   */
  idempotencyKey?: string;
  requestId?: string;
}

export interface UseScenarioForecastOptions {
  client?: ScenarioApiClient;
  /** Emit failures to the app's toast sink. Defaults to true. */
  showErrorToast?: boolean;
  mutationOptions?: Partial<
    Omit<
      UseMutationOptions<
        ScenarioComparisonResponse,
        ScenarioApiError,
        ScenarioForecastVariables
      >,
      'mutationFn'
    >
  >;
}

export type UseScenarioForecastResult = UseMutationResult<
  ScenarioComparisonResponse,
  ScenarioApiError,
  ScenarioForecastVariables
> & {
  /** Submit a comparison with a generated idempotency key. */
  runScenario: (request: ScenarioRequestInput) => Promise<ScenarioComparisonResponse>;
  /** Field-keyed messages for inline form errors. */
  fieldErrors: Record<string, string>;
  /** Toast-ready description of the current failure, if any. */
  errorPresentation: ErrorPresentation | null;
};

/**
 * Submit scenario comparisons.
 *
 * Retries are deliberately disabled: inference is expensive, and the stage that
 * failed decides whether a retry is even meaningful. The returned error carries
 * `retryable`, so the UI can offer a retry the user chooses explicitly.
 */
export function useScenarioForecast(
  options: UseScenarioForecastOptions = {},
): UseScenarioForecastResult {
  const { client = scenarioApi, showErrorToast = true, mutationOptions } = options;
  const queryClient = useQueryClient();
  /** Key per in-flight submission, so a retry of the same submit replays. */
  const idempotencyKeyRef = useRef<string | null>(null);

  const mutation = useMutation<
    ScenarioComparisonResponse,
    ScenarioApiError,
    ScenarioForecastVariables
  >({
    mutationFn: ({ request, idempotencyKey, requestId }) =>
      client.createScenarioComparison(request, {
        idempotencyKey: idempotencyKey ?? idempotencyKeyRef.current ?? undefined,
        requestId,
      }),
    retry: false,
    // Callbacks are forwarded with `...args` so this stays correct across
    // TanStack Query versions, which have changed their arity.
    onSuccess: (...args) => {
      const [data] = args;
      // Seed the detail cache so navigating to the scenario is instant.
      seedScenarioDetail(queryClient, data);
      return mutationOptions?.onSuccess?.(...args);
    },
    onError: (...args) => {
      if (showErrorToast) reportError(args[0]);
      return mutationOptions?.onError?.(...args);
    },
    onSettled: (...args) => {
      idempotencyKeyRef.current = null;
      return mutationOptions?.onSettled?.(...args);
    },
    ...mutationOptions,
  });

  const runScenario = useCallback(
    (request: ScenarioRequestInput) => {
      const idempotencyKey = generateIdempotencyKey();
      idempotencyKeyRef.current = idempotencyKey;
      return mutation.mutateAsync({ request, idempotencyKey });
    },
    [mutation],
  );

  return {
    ...mutation,
    runScenario,
    fieldErrors: mutation.error ? toFieldErrors(mutation.error) : {},
    errorPresentation: mutation.error ? mutation.error.toPresentation() : null,
  };
}
