import { CalendarRange, Sigma, TrendingUp, Workflow } from 'lucide-react';

import { MetricCard } from '@/components/shared/metric-card';
import {
  formatCompact,
  formatDateRange,
  formatPercent,
  formatSales,
} from '@/lib/format';
import type { ForecastResponse } from '@/lib/types';

/**
 * The four numbers a planner reads first.
 *
 * The expected range is a card of its own rather than a subtitle on the total:
 * a point total with no interval invites being treated as a commitment, which
 * is exactly what a recursive forecast cannot support.
 */
export function ForecastKpiCards({ forecast }: { readonly forecast: ForecastResponse }) {
  const first = forecast.forecasts[0]!;
  const last = forecast.forecasts[forecast.forecasts.length - 1]!;

  return (
    <div className="grid gap-4 sm:grid-cols-2 xl:grid-cols-4">
      <MetricCard
        label="Total forecast sales"
        value={formatCompact(forecast.summary.total_forecast_sales)}
        hint={`${formatSales(forecast.summary.total_forecast_sales)} over ${forecast.horizon_days} days`}
        icon={<Sigma aria-hidden="true" className="h-4 w-4" />}
      />
      <MetricCard
        label={`Expected range (${formatPercent(forecast.confidence_level)})`}
        value={`${formatCompact(forecast.summary.lower_bound_total)} – ${formatCompact(
          forecast.summary.upper_bound_total,
        )}`}
        hint="Interval widens with horizon"
        icon={<TrendingUp aria-hidden="true" className="h-4 w-4" />}
      />
      <MetricCard
        label="Average daily"
        value={formatSales(forecast.summary.average_daily_sales)}
        hint={formatDateRange(first.date, last.date)}
        icon={<CalendarRange aria-hidden="true" className="h-4 w-4" />}
      />
      <MetricCard
        label="Model"
        value={`${forecast.model} v${forecast.model_version}`}
        hint={`${forecast.model_stage} · history ${forecast.metadata.history_days} days`}
        icon={<Workflow aria-hidden="true" className="h-4 w-4" />}
      />
    </div>
  );
}
