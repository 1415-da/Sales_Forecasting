'use client';

/**
 * Forecast generation.
 *
 * A mutation, not a query: a forecast is an explicit action with a cost, and it
 * must not fire on mount, on refocus, or on a retry the user did not ask for.
 *
 * The last successful result is held here rather than read from the mutation,
 * so a failed re-run leaves the previous forecast on screen instead of blanking
 * the page. The error is shown alongside it.
 */
import { useMutation } from '@tanstack/react-query';
import { useCallback, useState } from 'react';

import { ApiError, createForecast } from '@/lib/api';
import type { ForecastRequestBody, ForecastResponse } from '@/lib/types';

export interface UseForecastResult {
  /** Run a forecast. Rejects with `ApiError`. */
  readonly generate: (body: ForecastRequestBody) => void;
  /** The most recent successful forecast, kept across a later failure. */
  readonly forecast: ForecastResponse | undefined;
  /** The request that produced `forecast`, for the export filename. */
  readonly submitted: ForecastRequestBody | undefined;
  readonly isPending: boolean;
  readonly error: ApiError | Error | null;
  readonly reset: () => void;
}

export function useForecast(): UseForecastResult {
  const [forecast, setForecast] = useState<ForecastResponse | undefined>();
  const [submitted, setSubmitted] = useState<ForecastRequestBody | undefined>();

  const mutation = useMutation<ForecastResponse, Error, ForecastRequestBody>({
    mutationFn: (body) => createForecast(body),
    // No automatic retry: inference is expensive and a second attempt should be
    // the user's decision, with the error in front of them.
    retry: false,
    onSuccess: (data, variables) => {
      setForecast(data);
      setSubmitted(variables);
    },
  });

  const reset = useCallback(() => {
    setForecast(undefined);
    setSubmitted(undefined);
    mutation.reset();
  }, [mutation]);

  return {
    generate: mutation.mutate,
    forecast,
    submitted,
    isPending: mutation.isPending,
    error: mutation.error,
    reset,
  };
}
