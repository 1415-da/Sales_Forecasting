'use client';

import { useMemo } from 'react';

import { cn } from '@/lib/utils';
import { formatDate, formatSignedNumber, formatSignedPercent } from '@/lib/utils/format';
import { buildHeatmap, type HeatmapCell } from '@/lib/utils/scenario-results';
import type { ScenarioForecastPoint } from '@/lib/types/scenario';

export interface ScenarioCalendarHeatmapProps {
  points: ScenarioForecastPoint[];
  locale?: string;
}

const WEEKDAY_LABELS = ['Mon', 'Tue', 'Wed', 'Thu', 'Fri', 'Sat', 'Sun'];

/**
 * Pick a tint for a cell.
 *
 * Intensity is bucketed into three steps rather than mapped to a continuous
 * opacity: Tailwind cannot generate arbitrary runtime classes, and discrete
 * steps stay legible against both light and dark backgrounds.
 */
function cellClasses(cell: HeatmapCell | null): string {
  if (!cell) return 'bg-transparent';
  if (cell.tone === 'neutral') return 'bg-muted text-muted-foreground';

  const step = cell.intensity > 0.66 ? 'strong' : cell.intensity > 0.33 ? 'medium' : 'light';
  if (cell.tone === 'positive') {
    return {
      light: 'bg-emerald-100 text-emerald-900 dark:bg-emerald-950 dark:text-emerald-200',
      medium: 'bg-emerald-300 text-emerald-950 dark:bg-emerald-800 dark:text-emerald-50',
      strong: 'bg-emerald-500 text-white dark:bg-emerald-600',
    }[step];
  }
  return {
    light: 'bg-red-100 text-red-900 dark:bg-red-950 dark:text-red-200',
    medium: 'bg-red-300 text-red-950 dark:bg-red-800 dark:text-red-50',
    strong: 'bg-red-500 text-white dark:bg-red-600',
  }[step];
}

export function ScenarioCalendarHeatmap({ points, locale }: ScenarioCalendarHeatmapProps) {
  const weeks = useMemo(() => buildHeatmap(points), [points]);

  if (weeks.length === 0) {
    return <p className="text-sm text-muted-foreground">No forecast days to show.</p>;
  }

  return (
    <div className="space-y-4">
      <div className="overflow-x-auto">
        <table className="border-separate border-spacing-1 text-xs">
          <caption className="sr-only">
            Daily scenario impact by calendar date. Each cell shows the difference
            from the baseline forecast.
          </caption>
          <thead>
            <tr>
              <th scope="col" className="sr-only">
                Week starting
              </th>
              {WEEKDAY_LABELS.map((day) => (
                <th
                  key={day}
                  scope="col"
                  className="w-14 pb-1 text-center font-medium text-muted-foreground"
                >
                  {day}
                </th>
              ))}
            </tr>
          </thead>
          <tbody>
            {weeks.map((week) => (
              <tr key={week.weekStart}>
                <th
                  scope="row"
                  className="whitespace-nowrap pr-2 text-right font-normal text-muted-foreground"
                >
                  {formatDate(week.weekStart, { locale })}
                </th>
                {week.days.map((cell, index) => (
                  <td key={`${week.weekStart}-${index}`} className="p-0">
                    {cell ? (
                      <div
                        className={cn(
                          'flex h-11 w-14 flex-col items-center justify-center rounded-md px-1 tabular-nums',
                          cellClasses(cell),
                        )}
                        /* The visual encoding is colour, so each cell also
                           carries its value as text for assistive tech. */
                        title={`${formatDate(cell.date, { locale })}: ${formatSignedNumber(
                          cell.difference,
                          { locale },
                        )}`}
                      >
                        <span className="sr-only">
                          {formatDate(cell.date, { locale })}:{' '}
                        </span>
                        <span className="text-[11px] font-medium leading-tight">
                          {formatSignedNumber(cell.difference, {
                            locale,
                            maximumFractionDigits: 0,
                          })}
                        </span>
                        <span className="text-[10px] leading-tight opacity-80">
                          {formatSignedPercent(cell.differencePct, {
                            locale,
                            maximumFractionDigits: 0,
                          })}
                        </span>
                      </div>
                    ) : (
                      <div
                        aria-hidden="true"
                        className="h-11 w-14 rounded-md border border-dashed border-border/60"
                      />
                    )}
                  </td>
                ))}
              </tr>
            ))}
          </tbody>
        </table>
      </div>

      <div className="flex flex-wrap items-center gap-4 text-xs text-muted-foreground">
        <span className="font-medium text-foreground">Legend</span>
        <span className="flex items-center gap-1.5">
          <span className="h-3 w-3 rounded bg-emerald-500 dark:bg-emerald-600" />
          Above baseline
        </span>
        <span className="flex items-center gap-1.5">
          <span className="h-3 w-3 rounded bg-red-500 dark:bg-red-600" />
          Below baseline
        </span>
        <span className="flex items-center gap-1.5">
          <span className="h-3 w-3 rounded bg-muted" />
          No change
        </span>
        <span className="flex items-center gap-1.5">
          <span className="h-3 w-3 rounded border border-dashed border-border/60" />
          Outside horizon
        </span>
        <span>Deeper colour means a larger movement.</span>
      </div>
    </div>
  );
}
