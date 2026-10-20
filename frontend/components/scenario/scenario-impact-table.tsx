'use client';

import { useMemo, useState } from 'react';
import { ArrowDown, ArrowUp, ArrowUpDown } from 'lucide-react';

import { Skeleton } from '@/components/ui/skeleton';
import { cn } from '@/lib/utils';
import { formatDate, formatNumber, formatSignedNumber, formatSignedPercent } from '@/lib/utils/format';
import {
  TONE_CLASSES,
  impactTone,
  sortImpactPoints,
  type SortDirection,
  type TableSortKey,
} from '@/lib/utils/scenario-results';
import type { ScenarioForecastPoint } from '@/lib/types/scenario';

export interface ScenarioImpactTableProps {
  points: ScenarioForecastPoint[];
  /** Dates the scenario modified, highlighted in the table. */
  modifiedDates?: string[];
  locale?: string;
}

const SORT_LABELS: Record<SortDirection, string> = {
  asc: 'ascending',
  desc: 'descending',
};

export function ScenarioImpactTable({
  points,
  modifiedDates = [],
  locale,
}: ScenarioImpactTableProps) {
  const [sortKey, setSortKey] = useState<TableSortKey>('date');
  const [direction, setDirection] = useState<SortDirection>('asc');

  const modified = useMemo(() => new Set(modifiedDates), [modifiedDates]);
  const rows = useMemo(
    () => sortImpactPoints(points, sortKey, direction),
    [points, sortKey, direction],
  );

  const toggleSort = (key: TableSortKey) => {
    if (key === sortKey) {
      setDirection((current) => (current === 'asc' ? 'desc' : 'asc'));
    } else {
      setSortKey(key);
      // Dates read naturally oldest-first; impacts are most useful largest-first.
      setDirection(key === 'date' ? 'asc' : 'desc');
    }
  };

  const SortButton = ({ column, label }: { column: TableSortKey; label: string }) => {
    const active = sortKey === column;
    const Icon = !active ? ArrowUpDown : direction === 'asc' ? ArrowUp : ArrowDown;
    return (
      <button
        type="button"
        onClick={() => toggleSort(column)}
        className="inline-flex items-center gap-1 rounded-sm font-medium hover:underline focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring"
        aria-label={`Sort by ${label}, currently ${active ? SORT_LABELS[direction] : 'unsorted'}`}
      >
        {label}
        <Icon aria-hidden="true" className="h-3.5 w-3.5" />
      </button>
    );
  };

  if (points.length === 0) {
    return <p className="p-4 text-sm text-muted-foreground">No forecast days to show.</p>;
  }

  return (
    // Horizontal scroll on narrow screens rather than letting the table squash
    // its numeric columns into unreadable wraps.
    <div className="max-h-[420px] overflow-auto rounded-md border">
      <table className="w-full min-w-[560px] border-collapse text-sm">
        <caption className="sr-only">
          Daily baseline and scenario forecasts with their difference, sortable by
          date or difference.
        </caption>
        <thead className="sticky top-0 z-10 bg-muted/95 backdrop-blur">
          <tr className="text-left">
            <th scope="col" className="px-3 py-2">
              <SortButton column="date" label="Date" />
            </th>
            <th scope="col" className="px-3 py-2 text-right font-medium">
              Baseline
            </th>
            <th scope="col" className="px-3 py-2 text-right font-medium">
              Scenario
            </th>
            <th scope="col" className="px-3 py-2 text-right">
              <SortButton column="difference" label="Difference" />
            </th>
            <th scope="col" className="px-3 py-2 text-right font-medium">
              Change %
            </th>
          </tr>
        </thead>
        <tbody>
          {rows.map((point) => {
            const tone = impactTone(point.daily_difference);
            const inWindow = modified.has(point.date);
            return (
              <tr
                key={point.date}
                className={cn('border-t', inWindow && 'bg-accent/40')}
              >
                <th scope="row" className="whitespace-nowrap px-3 py-2 text-left font-normal">
                  {formatDate(point.date, { locale })}
                  {inWindow ? (
                    <span className="sr-only"> (inside the scenario window)</span>
                  ) : null}
                </th>
                <td className="px-3 py-2 text-right tabular-nums">
                  {formatNumber(point.baseline_forecast, { locale })}
                </td>
                <td className="px-3 py-2 text-right tabular-nums">
                  {formatNumber(point.scenario_forecast, { locale })}
                </td>
                <td
                  className={cn(
                    'px-3 py-2 text-right font-medium tabular-nums',
                    TONE_CLASSES[tone].text,
                  )}
                >
                  {formatSignedNumber(point.daily_difference, { locale })}
                </td>
                <td
                  className={cn(
                    'px-3 py-2 text-right tabular-nums',
                    TONE_CLASSES[tone].text,
                  )}
                >
                  {formatSignedPercent(point.daily_difference_pct, { locale })}
                </td>
              </tr>
            );
          })}
        </tbody>
      </table>
    </div>
  );
}

/** Loading placeholder matching the table shape. */
export function ScenarioImpactTableSkeleton({ rows = 8 }: { rows?: number }) {
  return (
    <div
      className="space-y-2 rounded-md border p-3"
      aria-busy="true"
      aria-label="Loading impact table"
    >
      <Skeleton className="h-8 w-full" />
      {Array.from({ length: rows }, (_, index) => (
        <Skeleton key={index} className="h-6 w-full" />
      ))}
    </div>
  );
}
