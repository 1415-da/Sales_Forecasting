'use client';

import { useState } from 'react';

import { RequireAuth } from '@/components/auth/require-auth';
import {
  AccuracyCards,
  FreshnessCard,
  HealthStatusCard,
  RetrainingCard,
} from '@/components/monitoring/health-cards';
import { AccuracyTrendChart } from '@/components/monitoring/accuracy-trend-chart';
import { AlertsPanel } from '@/components/monitoring/alerts-panel';
import { DriftTable } from '@/components/monitoring/drift-table';
import { ErrorState } from '@/components/shared/error-state';
import { KpiSkeleton } from '@/components/shared/loading-skeleton';
import { Label } from '@/components/ui/label';
import { Select } from '@/components/ui/select';
import { useAuth } from '@/hooks/use-auth';
import {
  useAccuracy,
  useAlertActions,
  useAlerts,
  useDrift,
  useMonitoringSummary,
} from '@/hooks/use-monitoring';
import { PERMISSIONS } from '@/lib/rbac';

const WINDOW_OPTIONS = [7, 14, 28, 60, 90] as const;

/**
 * Monitoring Dashboard.
 *
 * Every panel can say "there is nothing to show yet" without inventing a
 * number. That is the point of the page: a dashboard that fabricates a
 * plausible zero is worse than an empty one, because the zero gets believed
 * and then acted on.
 */
export default function MonitoringPage() {
  return (
    <RequireAuth permission={PERMISSIONS.viewMonitoring}>
      <MonitoringDashboard />
    </RequireAuth>
  );
}

function MonitoringDashboard() {
  const { can } = useAuth();
  const [windowDays, setWindowDays] = useState<number>(28);
  const [alertStatus, setAlertStatus] = useState<string>('');

  const canDetail = can(PERMISSIONS.viewMonitoringDetail);
  const canManageAlerts = can(PERMISSIONS.manageAlerts);

  const summary = useMonitoringSummary({ windowDays });
  const modelVersion = summary.data?.model_version ?? undefined;

  const accuracy = useAccuracy({
    windowDays,
    ...(modelVersion ? { modelVersion } : {}),
  });
  const drift = useDrift(modelVersion, canDetail);
  const alerts = useAlerts(alertStatus ? { status: alertStatus } : {});
  const { acknowledge, resolve } = useAlertActions();

  const pendingAlertId =
    (acknowledge.isPending ? acknowledge.variables : undefined) ??
    (resolve.isPending ? resolve.variables?.alertId : undefined) ??
    null;

  return (
    <div className="space-y-6">
      <div className="flex flex-wrap items-end justify-between gap-4">
        <div className="space-y-1">
          <h1 className="text-xl font-semibold tracking-tight">Monitoring</h1>
          <p className="text-sm text-muted-foreground">
            Forecast accuracy, data freshness, drift, and alerts.
          </p>
        </div>
        <div className="w-40 space-y-1.5">
          <Label htmlFor="window">Evaluation window</Label>
          <Select
            id="window"
            value={String(windowDays)}
            onChange={(event) => setWindowDays(Number(event.target.value))}
          >
            {WINDOW_OPTIONS.map((days) => (
              <option key={days} value={days}>
                {days} days
              </option>
            ))}
          </Select>
        </div>
      </div>

      {summary.isError ? (
        <ErrorState error={summary.error} onRetry={() => void summary.refetch()} />
      ) : null}

      {summary.isLoading ? (
        <KpiSkeleton count={2} />
      ) : summary.data ? (
        <>
          <div className="grid gap-4 lg:grid-cols-2">
            <HealthStatusCard summary={summary.data} />
            <RetrainingCard recommendation={summary.data.retraining} />
          </div>

          <AccuracyCards
            current={summary.data.accuracy.current}
            trend={summary.data.accuracy.trend}
            hasData={summary.data.accuracy.has_data}
            noDataMessage={summary.data.no_data_message ?? null}
          />

          {accuracy.data ? <AccuracyTrendChart report={accuracy.data} /> : null}

          <FreshnessCard freshness={summary.data.freshness} />

          {canDetail ? (
            drift.data ? (
              <DriftTable report={drift.data} />
            ) : drift.isError ? (
              <ErrorState error={drift.error} onRetry={() => void drift.refetch()} />
            ) : null
          ) : (
            <p className="rounded-lg border border-dashed p-4 text-sm text-muted-foreground">
              Per-feature drift detail requires an Analyst, ML Engineer, or
              Administrator role. The health summary above reflects it.
            </p>
          )}

          <div className="flex flex-wrap items-end justify-between gap-4">
            <h2 className="text-base font-semibold">Alerts</h2>
            <div className="w-44 space-y-1.5">
              <Label htmlFor="alert-status">Show</Label>
              <Select
                id="alert-status"
                value={alertStatus}
                onChange={(event) => setAlertStatus(event.target.value)}
              >
                <option value="">Needs attention</option>
                <option value="open">Open only</option>
                <option value="acknowledged">Acknowledged</option>
                <option value="resolved">Resolved</option>
              </Select>
            </div>
          </div>

          <AlertsPanel
            data={alerts.data}
            isLoading={alerts.isLoading}
            error={alerts.error}
            canManage={canManageAlerts}
            pendingId={pendingAlertId}
            onRetry={() => void alerts.refetch()}
            onAcknowledge={(alertId) => acknowledge.mutate(alertId)}
            onResolve={(alertId, note) =>
              resolve.mutate(note ? { alertId, note } : { alertId })
            }
          />
        </>
      ) : null}
    </div>
  );
}
