'use client';

import { Download } from 'lucide-react';
import { useState } from 'react';

import { Button } from '@/components/ui/button';
import type { ForecastResponse } from '@/lib/types';

/** Columns written to the CSV, in order. */
const CSV_COLUMNS = [
  'date',
  'predicted_sales',
  'lower_bound',
  'upper_bound',
  'model',
  'model_version',
  'generated_at',
] as const;

/**
 * Escape one CSV field.
 *
 * A value containing a comma, a quote, or a newline has to be quoted, or the
 * file silently gains a column the moment a store name has a comma in it.
 */
function escapeCsv(value: string | number): string {
  const text = String(value);
  return /[",\n\r]/.test(text) ? `"${text.replace(/"/g, '""')}"` : text;
}

/** Render a forecast as CSV text. */
export function buildForecastCsv(forecast: ForecastResponse): string {
  const lines = [CSV_COLUMNS.join(',')];
  for (const row of forecast.forecasts) {
    lines.push(
      [
        row.date,
        row.predicted_sales,
        row.lower_bound,
        row.upper_bound,
        forecast.model,
        forecast.model_version,
        forecast.generated_at,
      ]
        .map(escapeCsv)
        .join(','),
    );
  }
  // A trailing newline: POSIX tools treat a file without one as truncated.
  return `${lines.join('\n')}\n`;
}

/** Filename that identifies the forecast without needing to open it. */
export function forecastFilename(forecast: ForecastResponse): string {
  const day = forecast.generated_at.slice(0, 10);
  return `forecast_${forecast.store_id}_${forecast.model}_v${forecast.model_version}_${forecast.horizon_days}d_${day}.csv`;
}

export function ExportButton({ forecast }: { readonly forecast: ForecastResponse }) {
  const [failed, setFailed] = useState(false);

  const download = () => {
    try {
      const blob = new Blob([buildForecastCsv(forecast)], {
        type: 'text/csv;charset=utf-8',
      });
      const url = URL.createObjectURL(blob);
      const anchor = document.createElement('a');
      anchor.href = url;
      anchor.download = forecastFilename(forecast);
      document.body.appendChild(anchor);
      anchor.click();
      anchor.remove();
      // Revoking immediately can cancel the download in some browsers, so the
      // URL is released on the next tick instead.
      setTimeout(() => URL.revokeObjectURL(url), 0);
      setFailed(false);
    } catch {
      setFailed(true);
    }
  };

  return (
    <div className="flex flex-col items-end gap-1">
      <Button size="sm" variant="outline" onClick={download}>
        <Download aria-hidden="true" className="h-3.5 w-3.5" />
        Download CSV
      </Button>
      {failed ? (
        <p className="text-xs text-destructive">
          The download could not be started in this browser.
        </p>
      ) : null}
    </div>
  );
}
