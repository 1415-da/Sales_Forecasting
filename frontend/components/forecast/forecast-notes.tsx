import { Info } from 'lucide-react';

import { Card, CardContent, CardHeader, CardTitle } from '@/components/ui/card';
import { formatMetric, formatTimestamp } from '@/lib/format';
import type { ForecastResponse } from '@/lib/types';

const UNCERTAINTY_LABELS: Record<string, string> = {
  residual_normal: "Normal interval from the model's held-out error (RMSE)",
  history_volatility: 'Approximated from the volatility of the supplied history',
};

/**
 * How the forecast was produced, and what not to read into it.
 *
 * Prominent rather than tucked into a tooltip. The API returns these caveats on
 * every response precisely because they change how the numbers should be used:
 * the model leans on a same-day column that cannot be known for a future date,
 * so the projection tracks the recent level rather than predicting movement.
 * A console that hid that would be presenting a persistence forecast as a
 * prediction.
 */
export function ForecastNotes({ forecast }: { readonly forecast: ForecastResponse }) {
  const { metadata } = forecast;

  return (
    <Card>
      <CardHeader>
        <CardTitle className="flex items-center gap-2">
          <Info aria-hidden="true" className="h-4 w-4 text-muted-foreground" />
          How to read this forecast
        </CardTitle>
      </CardHeader>
      <CardContent className="space-y-4">
        <ul className="space-y-2">
          {forecast.warnings.map((warning) => (
            <li key={warning} className="text-sm text-muted-foreground">
              {warning}
            </li>
          ))}
        </ul>

        <dl className="grid gap-x-6 gap-y-2 border-t pt-4 text-xs sm:grid-cols-2">
          <div className="flex justify-between gap-4">
            <dt className="text-muted-foreground">Uncertainty method</dt>
            <dd className="text-right font-medium">
              {UNCERTAINTY_LABELS[metadata.uncertainty_method ?? ''] ??
                metadata.uncertainty_method ??
                '—'}
            </dd>
          </div>
          <div className="flex justify-between gap-4">
            <dt className="text-muted-foreground">Residual std</dt>
            <dd className="text-right font-medium tabular-nums">
              {formatMetric(metadata.residual_std)}
            </dd>
          </div>
          <div className="flex justify-between gap-4">
            <dt className="text-muted-foreground">History used</dt>
            <dd className="text-right font-medium">
              {metadata.history_start_date} → {metadata.history_end_date} (
              {metadata.history_days} days)
            </dd>
          </div>
          <div className="flex justify-between gap-4">
            <dt className="text-muted-foreground">Features</dt>
            <dd className="text-right font-medium tabular-nums">
              {metadata.feature_count ?? '—'} ({metadata.feature_schema_version})
            </dd>
          </div>
          {metadata.imputed_features?.length ? (
            <div className="flex justify-between gap-4 sm:col-span-2">
              <dt className="text-muted-foreground">Reconstructed inputs</dt>
              <dd className="text-right font-mono">
                {metadata.imputed_features.join(', ')}
              </dd>
            </div>
          ) : null}
          {metadata.data_source ? (
            <div className="flex justify-between gap-4">
              <dt className="text-muted-foreground">Trained on</dt>
              <dd className="text-right font-medium">
                {metadata.data_source}
                {metadata.dataset_version ? ` ${metadata.dataset_version}` : ''}
              </dd>
            </div>
          ) : null}
          <div className="flex justify-between gap-4">
            <dt className="text-muted-foreground">Generated</dt>
            <dd className="text-right font-medium">
              {formatTimestamp(forecast.generated_at)}
            </dd>
          </div>
          <div className="flex justify-between gap-4 sm:col-span-2">
            <dt className="text-muted-foreground">Forecast ID</dt>
            <dd className="break-all text-right font-mono">{forecast.forecast_id}</dd>
          </div>
        </dl>
      </CardContent>
    </Card>
  );
}
