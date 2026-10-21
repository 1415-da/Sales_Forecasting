'use client';

import { EmptyState } from '@/components/shared/empty-state';
import { StatusBadge, type StatusTone } from '@/components/shared/status-badge';
import { Card, CardContent, CardDescription, CardHeader, CardTitle } from '@/components/ui/card';
import type { DriftReport } from '@/lib/types/auth';

/** Tone for a drift verdict. */
function driftTone(status: string): StatusTone {
  switch (status) {
    case 'drift_detected':
      return 'danger';
    case 'warning':
      return 'warning';
    case 'stable':
      return 'success';
    default:
      return 'neutral';
  }
}

/** Readable label for a drift verdict. */
function driftLabel(status: string): string {
  switch (status) {
    case 'drift_detected':
      return 'Drift detected';
    case 'warning':
      return 'Warning';
    case 'stable':
      return 'Stable';
    default:
      return status;
  }
}

/**
 * Per-feature distribution shift.
 *
 * The sample sizes are shown alongside each score, because a PSI computed from
 * a couple of dozen observations is noise and should not be acted on as though
 * it were a signal.
 */
export function DriftTable({ report }: { readonly report: DriftReport }) {
  return (
    <Card>
      <CardHeader className="flex flex-row flex-wrap items-start justify-between gap-3">
        <div className="space-y-1">
          <CardTitle>Drift</CardTitle>
          <CardDescription>
            Current distribution against the preceding window.
          </CardDescription>
        </div>
        {report.has_data ? (
          <div className="flex gap-2">
            {report.drifting_features > 0 ? (
              <StatusBadge tone="danger">
                {report.drifting_features} drifting
              </StatusBadge>
            ) : null}
            {report.warning_features > 0 ? (
              <StatusBadge tone="warning">
                {report.warning_features} warning
              </StatusBadge>
            ) : null}
            {report.drifting_features === 0 && report.warning_features === 0 ? (
              <StatusBadge tone="success">All stable</StatusBadge>
            ) : null}
          </div>
        ) : null}
      </CardHeader>
      <CardContent className={report.has_data ? 'p-0' : undefined}>
        {!report.has_data ? (
          <EmptyState
            title="No drift evaluation yet"
            description={
              report.no_data_message ??
              'Drift is measured by the monitoring DAG once enough scored days exist in two consecutive windows.'
            }
          />
        ) : (
          <div className="overflow-x-auto">
            <table className="w-full text-sm">
              <caption className="sr-only">Drift score per feature</caption>
              <thead className="bg-muted/60">
                <tr>
                  <th scope="col" className="px-4 py-2 text-left font-medium">
                    Feature
                  </th>
                  <th scope="col" className="px-4 py-2 text-left font-medium">
                    Method
                  </th>
                  <th scope="col" className="px-4 py-2 text-right font-medium">
                    Score
                  </th>
                  <th scope="col" className="px-4 py-2 text-right font-medium">
                    Threshold
                  </th>
                  <th scope="col" className="px-4 py-2 text-right font-medium">
                    Sample
                  </th>
                  <th scope="col" className="px-4 py-2 text-left font-medium">
                    Status
                  </th>
                  <th scope="col" className="px-4 py-2 text-left font-medium">
                    Evaluated
                  </th>
                </tr>
              </thead>
              <tbody>
                {report.features.map((row) => (
                  <tr key={`${row.feature_name}-${row.drift_method}`} className="border-t">
                    <td className="px-4 py-2 font-mono text-xs">{row.feature_name}</td>
                    <td className="px-4 py-2 text-xs uppercase text-muted-foreground">
                      {row.drift_method === 'kolmogorov_smirnov' ? 'KS' : row.drift_method}
                    </td>
                    <td className="px-4 py-2 text-right font-medium tabular-nums">
                      {row.drift_score.toFixed(4)}
                    </td>
                    <td className="px-4 py-2 text-right tabular-nums text-muted-foreground">
                      {row.threshold.toFixed(2)}
                    </td>
                    <td className="px-4 py-2 text-right tabular-nums text-muted-foreground">
                      {row.reference_size ?? '—'} / {row.current_size ?? '—'}
                    </td>
                    <td className="px-4 py-2">
                      <StatusBadge tone={driftTone(row.status)}>
                        {driftLabel(row.status)}
                      </StatusBadge>
                    </td>
                    <td className="px-4 py-2 text-xs text-muted-foreground">
                      {row.evaluation_date ?? '—'}
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        )}
      </CardContent>
    </Card>
  );
}
