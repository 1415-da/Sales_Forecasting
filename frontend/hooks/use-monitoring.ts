'use client';

/**
 * Monitoring reads and alert actions.
 *
 * The summary endpoint is one call on purpose: the health verdict is a fold
 * over freshness, accuracy, drift, and alerts, and assembling it client-side
 * from four independent queries would let the pieces disagree mid-render.
 */
import {
  useMutation,
  useQuery,
  useQueryClient,
  type UseQueryResult,
} from '@tanstack/react-query';

import {
  ApiError,
  acknowledgeAlert,
  getAccuracy,
  getAlerts,
  getDataFreshness,
  getDrift,
  getMonitoringSummary,
  resolveAlert,
} from '@/lib/api';
import type {
  AccuracyReport,
  AlertList,
  DriftReport,
  FreshnessReport,
  MonitoringSummary,
} from '@/lib/types/auth';

/** Only transport failures retry; a 403 will not resolve itself. */
function retry(failureCount: number, error: unknown): boolean {
  if (failureCount >= 2) return false;
  return error instanceof ApiError ? error.isRetryable : false;
}

/** The monitoring dashboard's header. */
export function useMonitoringSummary(params: {
  windowDays?: number;
  modelVersion?: string;
}): UseQueryResult<MonitoringSummary, Error> {
  return useQuery<MonitoringSummary, Error>({
    queryKey: ['monitoring', 'summary', params],
    queryFn: ({ signal }) => getMonitoringSummary(params, signal),
    // Monitoring is written by a daily DAG, so a short stale time would only
    // produce requests that return the same answer.
    staleTime: 60_000,
    retry,
  });
}

/** Accuracy for the current window, plus the stored trend series. */
export function useAccuracy(params: {
  windowDays?: number;
  modelVersion?: string;
  storeId?: string;
  start?: string;
  end?: string;
}): UseQueryResult<AccuracyReport, Error> {
  return useQuery<AccuracyReport, Error>({
    queryKey: ['monitoring', 'accuracy', params],
    queryFn: ({ signal }) => getAccuracy(params, signal),
    staleTime: 60_000,
    retry,
  });
}

/** How current the monitoring inputs are. */
export function useDataFreshness(): UseQueryResult<FreshnessReport, Error> {
  return useQuery<FreshnessReport, Error>({
    queryKey: ['monitoring', 'freshness'],
    queryFn: ({ signal }) => getDataFreshness(signal),
    staleTime: 60_000,
    retry,
  });
}

/**
 * Per-feature drift.
 *
 * `enabled` is a parameter rather than being inferred, because the caller
 * knows whether the user holds `view_monitoring_detail`; firing the request
 * regardless would produce a guaranteed 403 on every dashboard load for a
 * BUSINESS_USER.
 */
export function useDrift(
  modelVersion: string | undefined,
  enabled: boolean,
): UseQueryResult<DriftReport, Error> {
  return useQuery<DriftReport, Error>({
    queryKey: ['monitoring', 'drift', modelVersion ?? null],
    queryFn: ({ signal }) => getDrift(modelVersion, signal),
    enabled,
    staleTime: 60_000,
    retry,
  });
}

/** Alerts, newest first. */
export function useAlerts(params: {
  status?: string;
  severity?: string;
}): UseQueryResult<AlertList, Error> {
  return useQuery<AlertList, Error>({
    queryKey: ['monitoring', 'alerts', params],
    queryFn: ({ signal }) => getAlerts(params, signal),
    staleTime: 30_000,
    retry,
  });
}

/**
 * Acknowledge or resolve an alert.
 *
 * Invalidates the whole monitoring tree on success: an alert's state feeds the
 * health verdict and the counts in the summary, so refreshing only the alert
 * list would leave the header contradicting the panel below it.
 */
export function useAlertActions() {
  const queryClient = useQueryClient();

  const invalidate = () =>
    queryClient.invalidateQueries({ queryKey: ['monitoring'] });

  const acknowledge = useMutation({
    mutationFn: (alertId: string) => acknowledgeAlert(alertId),
    onSuccess: invalidate,
    retry: false,
  });

  const resolve = useMutation({
    mutationFn: ({ alertId, note }: { alertId: string; note?: string }) =>
      resolveAlert(alertId, note),
    onSuccess: invalidate,
    retry: false,
  });

  return { acknowledge, resolve };
}
