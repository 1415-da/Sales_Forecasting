'use client';

import {
  Activity,
  AlertTriangle,
  CalendarClock,
  Database,
  Gauge,
  TrendingDown,
  TrendingUp,
} from 'lucide-react';
import type { ReactNode } from 'react';

import { MetricCard } from '@/components/shared/metric-card';
import { StatusBadge, type StatusTone } from '@/components/shared/status-badge';
import { Card, CardContent, CardDescription, CardHeader, CardTitle } from '@/components/ui/card';
import { formatMetric, formatPercent, formatRelative, formatSales } from '@/lib/format';
import type {
  AccuracyMetrics,
  FreshnessReport,
  MonitoringSummary,
  RetrainingRecommendation,
} from '@/lib/types/auth';
import { cn } from '@/lib/utils';

/** Tone for an overall monitoring verdict. `no_data` is not "healthy". */
export function statusTone(status: string): StatusTone {
  switch (status) {
    case 'healthy':
      return 'success';
    case 'warning':
      return 'warning';
    case 'degraded':
      return 'danger';
    default:
      return 'neutral';
  }
}

/** Human-readable verdict label. */
export function statusLabel(status: string): string {
  switch (status) {
    case 'healthy':
      return 'Healthy';
    case 'warning':
      return 'Warning';
    case 'degraded':
      return 'Degraded';
    case 'no_data':
      return 'No data yet';
    default:
      return status;
  }
}

/**
 * The overall health panel.
 *
 * `no_data` is rendered as its own state, never as healthy: a platform that
 * has measured nothing is not known to be working, and a green badge over an
 * empty table is the one failure mode a monitoring page cannot afford.
 */
export function HealthStatusCard({ summary }: { readonly summary: MonitoringSummary }) {
  const tone = statusTone(summary.status);

  return (
    <Card>
      <CardHeader className="flex flex-row flex-wrap items-start justify-between gap-3">
        <div className="space-y-1">
          <CardTitle className="flex items-center gap-2">
            <Activity aria-hidden="true" className="h-4 w-4 text-muted-foreground" />
            Model health
          </CardTitle>
          <CardDescription>
            {summary.window_start} to {summary.window_end} ·{' '}
            {summary.model_version
              ? `version ${summary.model_version}`
              : 'no Production version resolved'}
          </CardDescription>
        </div>
        <StatusBadge tone={tone}>{statusLabel(summary.status)}</StatusBadge>
      </CardHeader>
      <CardContent className="space-y-3">
        {summary.no_data_message ? (
          <p className="rounded-md border border-dashed p-4 text-sm text-muted-foreground">
            {summary.no_data_message}
          </p>
        ) : null}

        <dl className="grid gap-x-6 gap-y-2 text-xs sm:grid-cols-2">
          <Row
            label="Open alerts"
            value={`${summary.alert_summary.open ?? 0}`}
            tone={
              (summary.alert_summary.critical ?? 0) > 0
                ? 'danger'
                : (summary.alert_summary.warning ?? 0) > 0
                  ? 'warning'
                  : undefined
            }
          />
          <Row
            label="Drifting features"
            value={`${summary.drift_summary.drifting_features ?? 0} of ${
              summary.drift_summary.evaluated_features ?? 0
            }`}
            tone={
              (summary.drift_summary.drifting_features ?? 0) > 0 ? 'danger' : undefined
            }
          />
          <Row
            label="Scored store-days"
            value={`${summary.accuracy.current.sample_size}`}
          />
          <Row
            label="Evaluation window"
            value={`${summary.window_days} days`}
          />
        </dl>
      </CardContent>
    </Card>
  );
}

function Row({
  label,
  value,
  tone,
}: {
  readonly label: string;
  readonly value: ReactNode;
  readonly tone?: 'danger' | 'warning';
}) {
  return (
    <div className="flex justify-between gap-4">
      <dt className="text-muted-foreground">{label}</dt>
      <dd
        className={cn(
          'text-right font-medium tabular-nums',
          tone === 'danger' ? 'text-destructive' : '',
          tone === 'warning' ? 'text-amber-500' : '',
        )}
      >
        {value}
      </dd>
    </div>
  );
}

/**
 * Data freshness.
 *
 * Shown prominently because every accuracy figure below it is only as current
 * as the actuals: a stale feed makes a good-looking WAPE meaningless.
 */
export function FreshnessCard({ freshness }: { readonly freshness: FreshnessReport }) {
  return (
    <Card>
      <CardHeader className="flex flex-row items-start justify-between gap-3">
        <div className="space-y-1">
          <CardTitle className="flex items-center gap-2">
            <Database aria-hidden="true" className="h-4 w-4 text-muted-foreground" />
            Data freshness
          </CardTitle>
          <CardDescription>
            Accuracy can only be measured where outcomes exist.
          </CardDescription>
        </div>
        <StatusBadge tone={statusTone(freshness.status)}>
          {statusLabel(freshness.status)}
        </StatusBadge>
      </CardHeader>
      <CardContent className="space-y-4">
        <dl className="grid gap-x-6 gap-y-2 text-xs sm:grid-cols-2">
          <Row
            label="Latest actual"
            value={freshness.latest_actual_date ?? 'none recorded'}
          />
          <Row
            label="Days behind"
            value={
              freshness.days_since_actual === null ||
              freshness.days_since_actual === undefined
                ? '—'
                : `${freshness.days_since_actual}`
            }
            tone={
              (freshness.days_since_actual ?? 0) >= 7
                ? 'danger'
                : (freshness.days_since_actual ?? 0) >= 3
                  ? 'warning'
                  : undefined
            }
          />
          <Row
            label="Latest forecast day"
            value={freshness.latest_forecast_date ?? 'none recorded'}
          />
          <Row
            label="Last forecast run"
            value={
              freshness.latest_forecast_created_at
                ? formatRelative(freshness.latest_forecast_created_at)
                : '—'
            }
          />
          <Row label="Actuals on record" value={formatSales(freshness.actual_count)} />
          <Row
            label="Forecast points"
            value={formatSales(freshness.forecast_count)}
          />
        </dl>

        {freshness.coverage.length > 0 ? (
          <div className="overflow-x-auto rounded-md border">
            <table className="w-full text-xs">
              <caption className="sr-only">Actuals coverage by store</caption>
              <thead className="bg-muted/60">
                <tr>
                  <th scope="col" className="px-3 py-2 text-left font-medium">
                    Store
                  </th>
                  <th scope="col" className="px-3 py-2 text-left font-medium">
                    Latest actual
                  </th>
                  <th scope="col" className="px-3 py-2 text-right font-medium">
                    Days
                  </th>
                </tr>
              </thead>
              <tbody>
                {freshness.coverage.map((row) => (
                  <tr key={row.store_id} className="border-t">
                    <td className="px-3 py-1.5 font-medium">{row.store_id}</td>
                    <td className="px-3 py-1.5">{row.latest_date ?? '—'}</td>
                    <td className="px-3 py-1.5 text-right tabular-nums">
                      {row.days}
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        ) : (
          <p className="text-xs text-muted-foreground">
            {freshness.no_data_message ?? 'No per-store coverage yet.'}
          </p>
        )}
      </CardContent>
    </Card>
  );
}

/** Direction indicator for a metric trend. */
function Trend({
  value,
  lowerIsBetter = true,
}: {
  readonly value: number | null | undefined;
  readonly lowerIsBetter?: boolean;
}) {
  if (value === null || value === undefined || !Number.isFinite(value)) {
    return <span className="text-muted-foreground">no prior window</span>;
  }
  const grew = value > 0;
  const better = lowerIsBetter ? !grew : grew;
  const Icon = grew ? TrendingUp : TrendingDown;
  return (
    <span
      className={cn(
        'inline-flex items-center gap-1',
        better ? 'text-emerald-500' : 'text-destructive',
      )}
    >
      <Icon aria-hidden="true" className="h-3 w-3" />
      {formatPercent(Math.abs(value), 1)} {grew ? 'higher' : 'lower'}
    </span>
  );
}

/**
 * The four accuracy KPIs.
 *
 * WAPE leads rather than MAPE: the data contains many near-zero store-days,
 * which send MAPE to thousands of percent and make it undefined on an exact
 * zero. Interval coverage is included because it is the only number that tests
 * the uncertainty band rather than the point forecast.
 */
export function AccuracyCards({
  current,
  trend,
  hasData,
  noDataMessage,
}: {
  readonly current: AccuracyMetrics;
  readonly trend: Record<string, number | null>;
  readonly hasData: boolean;
  readonly noDataMessage?: string | null;
}) {
  if (!hasData) {
    return (
      <Card>
        <CardContent className="p-6">
          <p className="text-sm text-muted-foreground">
            {noDataMessage ??
              'Monitoring will populate after forecasts and actual outcomes are recorded.'}
          </p>
        </CardContent>
      </Card>
    );
  }

  return (
    <div className="grid gap-4 sm:grid-cols-2 xl:grid-cols-4">
      <MetricCard
        label="WAPE"
        value={current.wape === null || current.wape === undefined ? '—' : formatPercent(current.wape, 2)}
        hint={<Trend value={trend.wape ?? null} />}
        icon={<Gauge aria-hidden="true" className="h-4 w-4" />}
      />
      <MetricCard
        label="MAE"
        value={formatMetric(current.mae, 1)}
        hint={<Trend value={trend.mae ?? null} />}
        icon={<Gauge aria-hidden="true" className="h-4 w-4" />}
      />
      <MetricCard
        label="Bias"
        value={formatMetric(current.bias, 1)}
        hint={
          current.bias === null || current.bias === undefined
            ? 'not measured'
            : current.bias > 0
              ? 'forecasts run high'
              : 'forecasts run low'
        }
        icon={<Activity aria-hidden="true" className="h-4 w-4" />}
      />
      <MetricCard
        label="Interval coverage"
        value={
          current.interval_coverage === null ||
          current.interval_coverage === undefined
            ? '—'
            : formatPercent(current.interval_coverage, 0)
        }
        hint={`from ${current.sample_size} scored day(s)`}
        icon={<CalendarClock aria-hidden="true" className="h-4 w-4" />}
      />
    </div>
  );
}

/** Retraining recommendation, with the reason that produced it. */
export function RetrainingCard({
  recommendation,
}: {
  readonly recommendation: RetrainingRecommendation;
}) {
  const tone: StatusTone =
    recommendation.recommendation === 'retrain'
      ? 'danger'
      : recommendation.recommendation === 'investigate'
        ? 'warning'
        : recommendation.recommendation === 'monitor'
          ? 'success'
          : 'neutral';

  return (
    <Card>
      <CardHeader className="flex flex-row flex-wrap items-start justify-between gap-3">
        <div className="space-y-1">
          <CardTitle className="flex items-center gap-2">
            <AlertTriangle aria-hidden="true" className="h-4 w-4 text-muted-foreground" />
            Retraining
          </CardTitle>
          <CardDescription>
            Derived from accuracy and drift against the current Production model.
          </CardDescription>
        </div>
        <StatusBadge tone={tone}>
          {recommendation.recommendation.replace('_', ' ')}
        </StatusBadge>
      </CardHeader>
      <CardContent className="space-y-3">
        {/*
          The reason is as prominent as the verdict: nobody should retrain a
          model because a dashboard said so without knowing why.
        */}
        <p className="text-sm">{recommendation.reason}</p>
        <dl className="grid gap-x-6 gap-y-2 text-xs sm:grid-cols-2">
          <Row label="Model version" value={recommendation.model_version ?? '—'} />
          <Row
            label="Last trained"
            value={
              recommendation.last_training_date
                ? formatRelative(recommendation.last_training_date)
                : '—'
            }
          />
          <Row
            label="WAPE"
            value={
              recommendation.wape === null || recommendation.wape === undefined
                ? '—'
                : formatPercent(recommendation.wape, 2)
            }
          />
          <Row
            label="Drifting features"
            value={`${recommendation.drift_features}`}
            tone={recommendation.drift_features > 0 ? 'danger' : undefined}
          />
        </dl>
      </CardContent>
    </Card>
  );
}
