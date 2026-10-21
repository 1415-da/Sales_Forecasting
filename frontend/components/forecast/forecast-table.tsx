'use client';

import { ArrowDown, ArrowUp } from 'lucide-react';
import { useMemo, useState } from 'react';

import { Card, CardContent, CardDescription, CardHeader, CardTitle } from '@/components/ui/card';
import { formatLongDate, formatSales } from '@/lib/format';
import type { DailyForecast, ForecastResponse } from '@/lib/types';
import { cn } from '@/lib/utils';

type SortKey = keyof Pick<
  DailyForecast,
  'date' | 'predicted_sales' | 'lower_bound' | 'upper_bound'
>;

const COLUMNS: readonly { key: SortKey; label: string; numeric: boolean }[] = [
  { key: 'date', label: 'Date', numeric: false },
  { key: 'predicted_sales', label: 'Predicted sales', numeric: true },
  { key: 'lower_bound', label: 'Lower bound', numeric: true },
  { key: 'upper_bound', label: 'Upper bound', numeric: true },
];

/**
 * The forecast, day by day.
 *
 * Sortable because a planner's question is often "which days are highest", and
 * scanning 90 chronological rows for that is a worse experience than one click.
 * The header is sticky so the columns stay identifiable while scrolling.
 */
export function ForecastTable({ forecast }: { readonly forecast: ForecastResponse }) {
  const [sortKey, setSortKey] = useState<SortKey>('date');
  const [direction, setDirection] = useState<'asc' | 'desc'>('asc');

  const rows = useMemo(() => {
    const sorted = [...forecast.forecasts];
    sorted.sort((left, right) => {
      const a = left[sortKey];
      const b = right[sortKey];
      const comparison =
        typeof a === 'string' && typeof b === 'string'
          ? a.localeCompare(b)
          : Number(a) - Number(b);
      return direction === 'asc' ? comparison : -comparison;
    });
    return sorted;
  }, [forecast.forecasts, sortKey, direction]);

  const toggleSort = (key: SortKey) => {
    if (key === sortKey) {
      setDirection((current) => (current === 'asc' ? 'desc' : 'asc'));
    } else {
      setSortKey(key);
      setDirection(key === 'date' ? 'asc' : 'desc');
    }
  };

  return (
    <Card>
      <CardHeader>
        <CardTitle>Daily forecast</CardTitle>
        <CardDescription>
          {forecast.horizon_days} days, with the{' '}
          {Math.round(forecast.confidence_level * 100)}% interval.
        </CardDescription>
      </CardHeader>
      <CardContent className="p-0">
        <div className="max-h-[480px] overflow-auto">
          <table className="w-full border-collapse text-sm">
            <caption className="sr-only">
              Forecast sales by date with lower and upper bounds
            </caption>
            <thead className="sticky top-0 z-10 bg-muted/90 backdrop-blur">
              <tr>
                {COLUMNS.map((column) => {
                  const isSorted = sortKey === column.key;
                  return (
                    <th
                      key={column.key}
                      scope="col"
                      aria-sort={
                        isSorted
                          ? direction === 'asc'
                            ? 'ascending'
                            : 'descending'
                          : 'none'
                      }
                      className={cn(
                        'px-4 py-2 font-medium',
                        column.numeric ? 'text-right' : 'text-left',
                      )}
                    >
                      <button
                        type="button"
                        onClick={() => toggleSort(column.key)}
                        className={cn(
                          'inline-flex items-center gap-1 rounded hover:text-foreground',
                          isSorted ? 'text-foreground' : 'text-muted-foreground',
                        )}
                      >
                        {column.label}
                        {isSorted ? (
                          direction === 'asc' ? (
                            <ArrowUp aria-hidden="true" className="h-3 w-3" />
                          ) : (
                            <ArrowDown aria-hidden="true" className="h-3 w-3" />
                          )
                        ) : null}
                      </button>
                    </th>
                  );
                })}
              </tr>
            </thead>
            <tbody>
              {rows.map((row) => (
                <tr key={row.date} className="border-t hover:bg-muted/40">
                  <td className="whitespace-nowrap px-4 py-2">{formatLongDate(row.date)}</td>
                  <td className="px-4 py-2 text-right font-medium tabular-nums">
                    {formatSales(row.predicted_sales)}
                  </td>
                  <td className="px-4 py-2 text-right tabular-nums text-muted-foreground">
                    {formatSales(row.lower_bound)}
                  </td>
                  <td className="px-4 py-2 text-right tabular-nums text-muted-foreground">
                    {formatSales(row.upper_bound)}
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      </CardContent>
    </Card>
  );
}
