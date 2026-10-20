'use client';

import { useMemo, useState } from 'react';
import {
  Area,
  CartesianGrid,
  ComposedChart,
  Legend,
  Line,
  ReferenceArea,
  ResponsiveContainer,
  Tooltip,
  XAxis,
  YAxis,
} from 'recharts';

import { Card, CardContent, CardHeader, CardTitle } from '@/components/ui/card';
import { Label } from '@/components/ui/label';
import { Skeleton } from '@/components/ui/skeleton';
import { Switch } from '@/components/ui/switch';
import { Tabs, TabsList, TabsTrigger } from '@/components/ui/tabs';
import {
  formatAxisDate,
  formatCompactNumber,
  formatDate,
  formatNumber,
  formatSignedNumber,
  formatSignedPercent,
} from '@/lib/utils/format';
import {
  applyChartView,
  hasConfidenceBands,
  scenarioWindow,
  toChartPoints,
  type ChartView,
} from '@/lib/utils/scenario-results';
import type { HistoryPoint, ScenarioComparisonResponse } from '@/lib/types/scenario';

export interface ScenarioComparisonChartCardProps {
  comparison: ScenarioComparisonResponse;
  /** Observed history, shown behind the forecast when toggled on. */
  history?: HistoryPoint[];
  locale?: string;
}

const VIEW_LABELS: Record<ChartView, string> = {
  daily: 'Daily',
  weekly: 'Weekly',
  cumulative: 'Cumulative',
};

interface TooltipRow {
  dataKey?: string | number;
  value?: number | string;
}

/** Tooltip that explains the numbers rather than dumping raw series values. */
function ChartTooltip({
  active,
  payload,
  label,
  locale,
}: {
  active?: boolean;
  payload?: TooltipRow[];
  label?: string | number;
  locale?: string;
}) {
  if (!active || !payload?.length) return null;

  const read = (key: string) =>
    payload.find((row) => row.dataKey === key)?.value as number | undefined;

  const baseline = read('baseline');
  const scenario = read('scenario');
  const difference =
    baseline !== undefined && scenario !== undefined ? scenario - baseline : undefined;
  const pct =
    baseline !== undefined && baseline > 0 && difference !== undefined
      ? (difference / baseline) * 100
      : null;

  return (
    <div className="rounded-md border bg-popover p-2.5 text-xs shadow-md">
      <p className="mb-1.5 font-medium">{formatDate(String(label), { locale })}</p>
      <dl className="space-y-1">
        <div className="flex items-center justify-between gap-4">
          <dt className="text-muted-foreground">Baseline</dt>
          <dd className="tabular-nums">{formatNumber(baseline, { locale })}</dd>
        </div>
        <div className="flex items-center justify-between gap-4">
          <dt className="text-muted-foreground">Scenario</dt>
          <dd className="tabular-nums">{formatNumber(scenario, { locale })}</dd>
        </div>
        <div className="flex items-center justify-between gap-4 border-t pt-1">
          <dt className="text-muted-foreground">Difference</dt>
          <dd className="font-medium tabular-nums">
            {formatSignedNumber(difference, { locale })}
            {pct === null ? '' : ` (${formatSignedPercent(pct, { locale })})`}
          </dd>
        </div>
      </dl>
    </div>
  );
}

export function ScenarioComparisonChartCard({
  comparison,
  history = [],
  locale,
}: ScenarioComparisonChartCardProps) {
  const [view, setView] = useState<ChartView>('daily');
  const [showBands, setShowBands] = useState(true);
  const [showHistory, setShowHistory] = useState(false);

  const bandsAvailable = useMemo(
    () => hasConfidenceBands(comparison.daily_forecasts),
    [comparison.daily_forecasts],
  );

  const forecastPoints = useMemo(
    () => toChartPoints(comparison.daily_forecasts, comparison.modified_dates),
    [comparison.daily_forecasts, comparison.modified_dates],
  );

  const data = useMemo(() => {
    const forecast = applyChartView(forecastPoints, view);
    if (!showHistory || history.length === 0 || view === 'cumulative') return forecast;

    // History is actuals, so both series carry the same observed value: the
    // scenario cannot differ from the baseline before the forecast begins.
    const historical = history.map((point) => ({
      date: point.date,
      baseline: point.sales,
      scenario: point.sales,
      difference: 0,
      differencePct: null,
      bandFloor: null,
      bandHeight: null,
      scenarioBandFloor: null,
      scenarioBandHeight: null,
      inScenarioWindow: false,
    }));
    return [...historical, ...forecast];
  }, [forecastPoints, view, showHistory, history]);

  const window = useMemo(
    () => scenarioWindow(comparison.modified_dates),
    [comparison.modified_dates],
  );
  const bandsVisible = showBands && bandsAvailable && view !== 'cumulative';

  return (
    <Card>
      <CardHeader className="gap-3">
        <div className="flex flex-wrap items-center justify-between gap-3">
          <CardTitle>Baseline vs scenario</CardTitle>
          <Tabs defaultValue="daily" value={view} onValueChange={(next) => setView(next as ChartView)}>
            <TabsList aria-label="Chart view">
              {(Object.keys(VIEW_LABELS) as ChartView[]).map((key) => (
                <TabsTrigger key={key} value={key}>
                  {VIEW_LABELS[key]}
                </TabsTrigger>
              ))}
            </TabsList>
          </Tabs>
        </div>

        <div className="flex flex-wrap items-center gap-x-5 gap-y-2">
          <div className="flex items-center gap-2">
            <Switch
              id="chart-bands"
              checked={bandsVisible}
              disabled={!bandsAvailable || view === 'cumulative'}
              onCheckedChange={setShowBands}
              aria-label="Show confidence bands"
            />
            <Label htmlFor="chart-bands" className="text-xs font-normal">
              Confidence bands
              {!bandsAvailable ? (
                <span className="text-muted-foreground"> (not available)</span>
              ) : null}
            </Label>
          </div>
          <div className="flex items-center gap-2">
            <Switch
              id="chart-history"
              checked={showHistory}
              disabled={history.length === 0 || view === 'cumulative'}
              onCheckedChange={setShowHistory}
              aria-label="Show historical actuals"
            />
            <Label htmlFor="chart-history" className="text-xs font-normal">
              Historical actuals
              {history.length === 0 ? (
                <span className="text-muted-foreground"> (none loaded)</span>
              ) : null}
            </Label>
          </div>
        </div>
      </CardHeader>

      <CardContent>
        <div className="h-[320px] w-full">
          <ResponsiveContainer width="100%" height="100%">
            <ComposedChart data={data} margin={{ top: 8, right: 8, left: 0, bottom: 0 }}>
              <CartesianGrid stroke="hsl(var(--chart-grid))" strokeDasharray="3 3" vertical={false} />
              <XAxis
                dataKey="date"
                tickFormatter={(value: string) => formatAxisDate(value, { locale })}
                tick={{ fontSize: 11 }}
                stroke="hsl(var(--muted-foreground))"
                minTickGap={24}
              />
              <YAxis
                tickFormatter={(value: number) => formatCompactNumber(value, { locale })}
                tick={{ fontSize: 11 }}
                stroke="hsl(var(--muted-foreground))"
                width={56}
              />
              <Tooltip content={<ChartTooltip locale={locale} />} />
              <Legend wrapperStyle={{ fontSize: 12 }} />

              {/* The scenario's active dates, so a reader can see which part of
                  the horizon the assumptions actually touch. */}
              {window ? (
                <ReferenceArea
                  x1={window.start}
                  x2={window.end}
                  fill="hsl(var(--chart-scenario))"
                  fillOpacity={0.08}
                  ifOverflow="extendDomain"
                />
              ) : null}

              {/* A p10-p90 ribbon, drawn as an invisible floor plus a visible
                  height: Recharts has no native range area. */}
              {bandsVisible ? (
                <>
                  <Area
                    type="monotone"
                    dataKey="bandFloor"
                    stackId="baselineBand"
                    stroke="none"
                    fill="none"
                    legendType="none"
                    isAnimationActive={false}
                    name="Baseline P10"
                  />
                  <Area
                    type="monotone"
                    dataKey="bandHeight"
                    stackId="baselineBand"
                    stroke="none"
                    fill="hsl(var(--chart-baseline))"
                    fillOpacity={0.15}
                    isAnimationActive={false}
                    name="Baseline P10-P90"
                  />
                  <Area
                    type="monotone"
                    dataKey="scenarioBandFloor"
                    stackId="scenarioBand"
                    stroke="none"
                    fill="none"
                    legendType="none"
                    isAnimationActive={false}
                    name="Scenario P10"
                  />
                  <Area
                    type="monotone"
                    dataKey="scenarioBandHeight"
                    stackId="scenarioBand"
                    stroke="none"
                    fill="hsl(var(--chart-scenario))"
                    fillOpacity={0.15}
                    isAnimationActive={false}
                    name="Scenario P10-P90"
                  />
                </>
              ) : null}

              <Line
                type="monotone"
                dataKey="baseline"
                name="Baseline"
                stroke="hsl(var(--chart-baseline))"
                strokeWidth={2}
                strokeDasharray="4 3"
                dot={false}
                isAnimationActive={false}
              />
              <Line
                type="monotone"
                dataKey="scenario"
                name="Scenario"
                stroke="hsl(var(--chart-scenario))"
                strokeWidth={2.5}
                dot={false}
                isAnimationActive={false}
              />
            </ComposedChart>
          </ResponsiveContainer>
        </div>

        <p className="mt-2 text-xs text-muted-foreground">
          {view === 'cumulative'
            ? 'Running totals across the horizon. Confidence bands are omitted here, since summing intervals would overstate certainty.'
            : 'The shaded span marks the dates the scenario modifies.'}
        </p>
      </CardContent>
    </Card>
  );
}

/** Loading placeholder matching the chart card. */
export function ScenarioComparisonChartCardSkeleton() {
  return (
    <Card aria-busy="true" aria-label="Loading comparison chart">
      <CardHeader className="gap-3">
        <div className="flex items-center justify-between gap-3">
          <Skeleton className="h-5 w-40" />
          <Skeleton className="h-9 w-56" />
        </div>
        <Skeleton className="h-4 w-64" />
      </CardHeader>
      <CardContent>
        <Skeleton className="h-[320px] w-full" />
      </CardContent>
    </Card>
  );
}
