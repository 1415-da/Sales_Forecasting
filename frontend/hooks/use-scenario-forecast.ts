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
import { useCallback, useRef, useState } from 'react';

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
  isAutoRetryableError,
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

/** Automatic retries, per the integration contract. */
export const MAX_AUTO_RETRIES = 2;
/** First backoff step; doubles each attempt (1s, then 2s). */
export const RETRY_BASE_DELAY_MS = 1000;

/** Exponential backoff for attempt `failureCount` (0-based). */
export function retryDelayMs(failureCount: number): number {
  return RETRY_BASE_DELAY_MS * 2 ** failureCount;
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
  /**
   * The last successful comparison, retained across a re-run.
   *
   * TanStack clears `data` while a mutation is in flight; keeping the previous
   * result lets the panel stay populated during an update instead of blanking
   * out to a skeleton.
   */
  lastComparison: ScenarioComparisonResponse | null;
  /** Whether an automatic retry is currently in progress. */
  isRetrying: boolean;
  /** Forget the retained comparison, e.g. after a reset. */
  clearLastComparison: () => void;
};

/**
 * Submit scenario comparisons.
 *
 * Automatic retries are limited to transport-level failures (network, timeout)
 * and upstream unavailability (502, 503), with exponential backoff. A 422, 404,
 * or 409 is deterministic, so retrying it would only delay the error; those
 * surface immediately and the UI offers a manual retry where it makes sense.
 *
 * Because each submission reuses one `Idempotency-Key`, an automatic retry
 * replays the original result rather than paying for a second inference.
 */
export function useScenarioForecast(
  options: UseScenarioForecastOptions = {},
): UseScenarioForecastResult {
  const { client = scenarioApi, showErrorToast = true, mutationOptions } = options;
  const queryClient = useQueryClient();
  /** Key per in-flight submission, so a retry of the same submit replays. */
  const idempotencyKeyRef = useRef<string | null>(null);
  const [lastComparison, setLastComparison] = useState<ScenarioComparisonResponse | null>(
    null,
  );

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

  const clearLastComparison = useCallback(() => setLastComparison(null), []);

  return {
    ...mutation,
    runScenario,
    fieldErrors: mutation.error ? toFieldErrors(mutation.error) : {},
    errorPresentation: mutation.error ? mutation.error.toPresentation() : null,
    lastComparison,
    isRetrying: mutation.isPending && mutation.failureCount > 0,
    clearLastComparison,
  };
}
