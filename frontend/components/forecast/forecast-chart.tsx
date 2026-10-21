'use client';

import { useMemo, useState } from 'react';
import {
  Area,
  CartesianGrid,
  ComposedChart,
  Legend,
  Line,
  ResponsiveContainer,
  Tooltip,
  XAxis,
  YAxis,
} from 'recharts';

import { Button } from '@/components/ui/button';
import { Card, CardContent, CardDescription, CardHeader, CardTitle } from '@/components/ui/card';
import { formatCompact, formatLongDate, formatSales, formatShortDate } from '@/lib/format';
import type { ForecastResponse, HistoryPoint } from '@/lib/types';

interface ChartRow {
  date: string;
  actual: number | null;
  forecast: number | null;
  /** Recharts stacks these two to draw the band: base, then its height. */
  bandBase: number | null;
  bandHeight: number | null;
  lower: number | null;
  upper: number | null;
}

type ViewMode = 'daily' | 'cumulative';

/** How much history to draw beside the forecast. */
const HISTORY_CONTEXT_DAYS = 45;

export interface ForecastChartProps {
  readonly forecast: ForecastResponse;
  readonly history: readonly HistoryPoint[];
}

/**
 * Actuals, forecast, and the confidence band.
 *
 * The band is a stacked pair of areas -- a transparent base at the lower bound
 * plus a visible height to the upper bound -- which is how Recharts draws a
 * ribbon between two series without a second axis.
 *
 * Only the recent history is drawn. Showing all of it would compress the
 * forecast into the right margin, which is the part the user came for.
 */
export function ForecastChart({ forecast, history }: ForecastChartProps) {
  const [view, setView] = useState<ViewMode>('daily');

  const rows = useMemo<ChartRow[]>(() => {
    const recent = [...history]
      .sort((left, right) => left.date.localeCompare(right.date))
      .slice(-HISTORY_CONTEXT_DAYS);

    if (view === 'daily') {
      const historyRows: ChartRow[] = recent.map((point) => ({
        date: point.date,
        actual: point.sales,
        forecast: null,
        bandBase: null,
        bandHeight: null,
        lower: null,
        upper: null,
      }));

      const forecastRows: ChartRow[] = forecast.forecasts.map((point) => ({
        date: point.date,
        actual: null,
        forecast: point.predicted_sales,
        bandBase: point.lower_bound,
        bandHeight: point.upper_bound - point.lower_bound,
        lower: point.lower_bound,
        upper: point.upper_bound,
      }));

      // Join the two series at the last observed day so the forecast line does
      // not start detached from the actuals.
      const seam = historyRows[historyRows.length - 1];
      if (seam) {
        seam.forecast = seam.actual;
        seam.bandBase = seam.actual;
        seam.bandHeight = 0;
      }
      return [...historyRows, ...forecastRows];
    }

    let runningForecast = 0;
    let runningLower = 0;
    let runningUpper = 0;
    return forecast.forecasts.map((point) => {
      runningForecast += point.predicted_sales;
      runningLower += point.lower_bound;
      runningUpper += point.upper_bound;
      return {
        date: point.date,
        actual: null,
        forecast: runningForecast,
        bandBase: runningLower,
        bandHeight: runningUpper - runningLower,
        lower: runningLower,
        upper: runningUpper,
      };
    });
  }, [forecast, history, view]);

  return (
    <Card>
      <CardHeader className="flex flex-row flex-wrap items-start justify-between gap-3">
        <div className="space-y-1">
          <CardTitle>Forecast</CardTitle>
          <CardDescription>
            {view === 'daily'
              ? 'Recent actuals, forecast, and the confidence band.'
              : 'Cumulative forecast across the horizon.'}
          </CardDescription>
        </div>
        <div role="tablist" aria-label="Chart view" className="flex gap-1">
          {(['daily', 'cumulative'] as const).map((mode) => (
            <Button
              key={mode}
              role="tab"
              aria-selected={view === mode}
              size="sm"
              variant={view === mode ? 'secondary' : 'ghost'}
              onClick={() => setView(mode)}
            >
              {mode === 'daily' ? 'Daily' : 'Cumulative'}
            </Button>
          ))}
        </div>
      </CardHeader>
      <CardContent>
        <div className="h-[340px] w-full">
          <ResponsiveContainer width="100%" height="100%">
            <ComposedChart data={rows} margin={{ top: 8, right: 8, bottom: 0, left: 0 }}>
              <CartesianGrid stroke="hsl(var(--chart-grid))" strokeDasharray="3 3" vertical={false} />
              <XAxis
                dataKey="date"
                tickFormatter={formatShortDate}
                stroke="hsl(var(--muted-foreground))"
                tick={{ fontSize: 11 }}
                minTickGap={24}
              />
              <YAxis
                tickFormatter={formatCompact}
                stroke="hsl(var(--muted-foreground))"
                tick={{ fontSize: 11 }}
                width={56}
              />
              <Tooltip content={<ForecastTooltip />} />
              <Legend verticalAlign="bottom" height={32} wrapperStyle={{ fontSize: 12 }} />
              {/*
                The band is a stacked pair: an invisible base at the lower bound
                and a visible height up to the upper bound. The base carries
                `legendType="none"` so only the ribbon itself is labelled.
              */}
              <Area
                dataKey="bandBase"
                stackId="band"
                stroke="none"
                fill="none"
                isAnimationActive={false}
                legendType="none"
                activeDot={false}
              />
              <Area
                dataKey="bandHeight"
                name={`${Math.round(forecast.confidence_level * 100)}% interval`}
                stackId="band"
                stroke="none"
                fill="hsl(var(--chart-scenario))"
                fillOpacity={0.16}
                isAnimationActive={false}
                legendType="rect"
                activeDot={false}
              />
              {view === 'daily' ? (
                <Line
                  dataKey="actual"
                  name="Actual"
                  stroke="hsl(var(--chart-baseline))"
                  strokeWidth={2}
                  dot={false}
                  connectNulls={false}
                  isAnimationActive={false}
                />
              ) : null}
              <Line
                dataKey="forecast"
                name="Forecast"
                stroke="hsl(var(--chart-scenario))"
                strokeWidth={2}
                strokeDasharray="5 3"
                dot={false}
                connectNulls={false}
                isAnimationActive={false}
              />
            </ComposedChart>
          </ResponsiveContainer>
        </div>
      </CardContent>
    </Card>
  );
}

interface TooltipProps {
  readonly active?: boolean;
  readonly payload?: readonly { payload: ChartRow }[];
}

/** Date, forecast, and both bounds -- the values the chart encodes visually. */
function ForecastTooltip({ active, payload }: TooltipProps) {
  if (!active || !payload?.length) return null;
  const row = payload[0]!.payload;

  return (
    <div className="rounded-md border bg-popover p-3 text-xs shadow-md">
      <p className="mb-1 font-medium">{formatLongDate(row.date)}</p>
      <dl className="space-y-0.5">
        {row.actual != null ? (
          <div className="flex justify-between gap-6">
            <dt className="text-muted-foreground">Actual</dt>
            <dd className="font-medium tabular-nums">{formatSales(row.actual)}</dd>
          </div>
        ) : null}
        {row.forecast != null ? (
          <div className="flex justify-between gap-6">
            <dt className="text-muted-foreground">Forecast</dt>
            <dd className="font-medium tabular-nums">{formatSales(row.forecast)}</dd>
          </div>
        ) : null}
        {row.lower != null && row.upper != null ? (
          <>
            <div className="flex justify-between gap-6">
              <dt className="text-muted-foreground">Lower</dt>
              <dd className="tabular-nums">{formatSales(row.lower)}</dd>
            </div>
            <div className="flex justify-between gap-6">
              <dt className="text-muted-foreground">Upper</dt>
              <dd className="tabular-nums">{formatSales(row.upper)}</dd>
            </div>
          </>
        ) : null}
      </dl>
    </div>
  );
}
