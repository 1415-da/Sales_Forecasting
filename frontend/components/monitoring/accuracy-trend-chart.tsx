'use client';

import { useMemo } from 'react';
import {
  CartesianGrid,
  Legend,
  Line,
  LineChart,
  ResponsiveContainer,
  Tooltip,
  XAxis,
  YAxis,
} from 'recharts';

import { EmptyState } from '@/components/shared/empty-state';
import { Card, CardContent, CardDescription, CardHeader, CardTitle } from '@/components/ui/card';
import { formatLongDate, formatPercent, formatShortDate } from '@/lib/format';
import type { AccuracyReport } from '@/lib/types/auth';

interface Point {
  date: string;
  wape: number | null;
  bias: number | null;
  sampleSize: number | null;
}

/**
 * Error over time, from the stored metric history.
 *
 * Read from `monitoring_metrics` rather than recomputed per render: the DAG's
 * value for a date records what was true then, and recomputing it today would
 * silently rewrite history as late actuals arrive.
 *
 * WAPE is plotted as a percentage on the left axis and bias in sales units on
 * the right, because they are not on the same scale and sharing one axis would
 * flatten whichever is smaller.
 */
export function AccuracyTrendChart({ report }: { readonly report: AccuracyReport }) {
  const points = useMemo<Point[]>(
    () =>
      report.series
        .map((row) => {
          const record = row as Record<string, unknown>;
          const date = record.evaluation_date;
          if (typeof date !== 'string') return null;
          return {
            date,
            wape: typeof record.wape === 'number' ? record.wape : null,
            bias: typeof record.bias === 'number' ? record.bias : null,
            sampleSize:
              typeof record.sample_size === 'number' ? record.sample_size : null,
          };
        })
        .filter((point): point is Point => point !== null),
    [report.series],
  );

  return (
    <Card>
      <CardHeader className="space-y-1">
        <CardTitle>Accuracy over time</CardTitle>
        <CardDescription>
          Recorded by the monitoring DAG, one point per evaluation.
        </CardDescription>
      </CardHeader>
      <CardContent>
        {points.length < 2 ? (
          <EmptyState
            title={
              points.length === 0
                ? 'No accuracy history yet'
                : 'Only one evaluation so far'
            }
            description={
              points.length === 0
                ? report.no_data_message ??
                  'The trend appears once the monitoring DAG has scored forecasts on at least two days.'
                : 'A trend needs at least two evaluations. Check back after the next monitoring run.'
            }
          />
        ) : (
          <div className="h-[300px] w-full">
            <ResponsiveContainer width="100%" height="100%">
              <LineChart data={points} margin={{ top: 8, right: 8, bottom: 0, left: 0 }}>
                <CartesianGrid
                  stroke="hsl(var(--chart-grid))"
                  strokeDasharray="3 3"
                  vertical={false}
                />
                <XAxis
                  dataKey="date"
                  tickFormatter={formatShortDate}
                  stroke="hsl(var(--muted-foreground))"
                  tick={{ fontSize: 11 }}
                  minTickGap={24}
                />
                <YAxis
                  yAxisId="wape"
                  tickFormatter={(value: number) => formatPercent(value, 0)}
                  stroke="hsl(var(--muted-foreground))"
                  tick={{ fontSize: 11 }}
                  width={52}
                />
                <YAxis
                  yAxisId="bias"
                  orientation="right"
                  stroke="hsl(var(--muted-foreground))"
                  tick={{ fontSize: 11 }}
                  width={52}
                />
                <Tooltip content={<TrendTooltip />} />
                <Legend verticalAlign="bottom" height={28} wrapperStyle={{ fontSize: 12 }} />
                <Line
                  yAxisId="wape"
                  dataKey="wape"
                  name="WAPE"
                  stroke="hsl(var(--chart-scenario))"
                  strokeWidth={2}
                  dot={{ r: 2 }}
                  connectNulls
                  isAnimationActive={false}
                />
                <Line
                  yAxisId="bias"
                  dataKey="bias"
                  name="Bias"
                  stroke="hsl(var(--chart-neutral))"
                  strokeWidth={2}
                  strokeDasharray="4 3"
                  dot={{ r: 2 }}
                  connectNulls
                  isAnimationActive={false}
                />
              </LineChart>
            </ResponsiveContainer>
          </div>
        )}
      </CardContent>
    </Card>
  );
}

function TrendTooltip({
  active,
  payload,
}: {
  readonly active?: boolean;
  readonly payload?: readonly { payload: Point }[];
}) {
  if (!active || !payload?.length) return null;
  const point = payload[0]!.payload;
  return (
    <div className="rounded-md border bg-popover p-3 text-xs shadow-md">
      <p className="mb-1 font-medium">{formatLongDate(point.date)}</p>
      <dl className="space-y-0.5">
        <div className="flex justify-between gap-6">
          <dt className="text-muted-foreground">WAPE</dt>
          <dd className="font-medium tabular-nums">
            {point.wape === null ? '—' : formatPercent(point.wape, 2)}
          </dd>
        </div>
        <div className="flex justify-between gap-6">
          <dt className="text-muted-foreground">Bias</dt>
          <dd className="tabular-nums">
            {point.bias === null ? '—' : point.bias.toFixed(1)}
          </dd>
        </div>
        {/* The sample size travels with the value: a WAPE from 3 store-days
            should not be read like one from 300. */}
        <div className="flex justify-between gap-6">
          <dt className="text-muted-foreground">Scored days</dt>
          <dd className="tabular-nums">{point.sampleSize ?? '—'}</dd>
        </div>
      </dl>
    </div>
  );
}
