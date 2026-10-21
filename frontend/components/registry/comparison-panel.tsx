'use client';

import { ArrowDown, ArrowRight, ArrowUp, Minus } from 'lucide-react';

import { ErrorState } from '@/components/shared/error-state';
import { StatusBadge } from '@/components/shared/status-badge';
import { Card, CardContent, CardDescription, CardHeader, CardTitle } from '@/components/ui/card';
import { Label } from '@/components/ui/label';
import { Select } from '@/components/ui/select';
import { formatMetric, formatPercent } from '@/lib/format';
import type { ModelComparison } from '@/lib/types';
import { cn } from '@/lib/utils';

export interface ComparisonPanelProps {
  readonly versions: readonly string[];
  readonly versionA: string;
  readonly versionB: string;
  readonly onChange: (next: { versionA?: string; versionB?: string }) => void;
  readonly comparison: ModelComparison | undefined;
  readonly isLoading: boolean;
  readonly error: unknown;
}

/**
 * Side-by-side metric comparison.
 *
 * A verdict per metric, not just a delta: "RMSE changed by -4.2" requires the
 * reader to remember whether lower is better for that metric, and the server
 * already knows. `lower_is_better` travels with each row so R² is not reported
 * as a regression when it improves.
 */
export function ComparisonPanel({
  versions,
  versionA,
  versionB,
  onChange,
  comparison,
  isLoading,
  error,
}: ComparisonPanelProps) {
  const sameVersion = versionA !== '' && versionA === versionB;

  return (
    <Card>
      <CardHeader className="space-y-1">
        <CardTitle>Compare versions</CardTitle>
        <CardDescription>
          The candidate is measured against the incumbent, so “improved” always
          means the candidate is better.
        </CardDescription>
      </CardHeader>
      <CardContent className="space-y-4">
        <div className="grid gap-4 sm:grid-cols-[1fr_auto_1fr] sm:items-end">
          <div className="space-y-1.5">
            <Label htmlFor="version-a">Incumbent</Label>
            <Select
              id="version-a"
              value={versionA}
              onChange={(event) => onChange({ versionA: event.target.value })}
            >
              <option value="">Select a version</option>
              {versions.map((version) => (
                <option key={version} value={version}>
                  Version {version}
                </option>
              ))}
            </Select>
          </div>
          <ArrowRight
            aria-hidden="true"
            className="mx-auto hidden h-4 w-4 shrink-0 text-muted-foreground sm:mb-3 sm:block"
          />
          <div className="space-y-1.5">
            <Label htmlFor="version-b">Candidate</Label>
            <Select
              id="version-b"
              value={versionB}
              onChange={(event) => onChange({ versionB: event.target.value })}
            >
              <option value="">Select a version</option>
              {versions.map((version) => (
                <option key={version} value={version}>
                  Version {version}
                </option>
              ))}
            </Select>
          </div>
        </div>

        {sameVersion ? (
          <p className="text-sm text-muted-foreground">
            Choose two different versions to compare.
          </p>
        ) : null}

        {error ? <ErrorState error={error} /> : null}

        {isLoading ? (
          <p className="text-sm text-muted-foreground">Comparing…</p>
        ) : comparison ? (
          <div className="space-y-4">
            <div className="flex flex-wrap items-center gap-2">
              {comparison.improved_count > 0 ? (
                <StatusBadge tone="success">
                  {comparison.improved_count} improved
                </StatusBadge>
              ) : null}
              {comparison.regressed_count > 0 ? (
                <StatusBadge tone="danger">
                  {comparison.regressed_count} regressed
                </StatusBadge>
              ) : null}
            </div>

            {/*
              The summary sentence is the server's, not assembled here: it is
              careful not to overclaim when no metric is recorded for both
              versions, and restating it client-side would risk losing that.
            */}
            <p className="text-sm">{comparison.summary}</p>

            <div className="overflow-x-auto rounded-md border">
              <table className="w-full text-sm">
                <caption className="sr-only">Metric comparison</caption>
                <thead className="bg-muted/60">
                  <tr>
                    <th scope="col" className="px-4 py-2 text-left font-medium">
                      Metric
                    </th>
                    <th scope="col" className="px-4 py-2 text-right font-medium">
                      v{comparison.version_a.version}
                    </th>
                    <th scope="col" className="px-4 py-2 text-right font-medium">
                      v{comparison.version_b.version}
                    </th>
                    <th scope="col" className="px-4 py-2 text-right font-medium">
                      Change
                    </th>
                    <th scope="col" className="px-4 py-2 text-left font-medium">
                      Verdict
                    </th>
                  </tr>
                </thead>
                <tbody>
                  {comparison.metrics.map((row) => (
                    <tr key={row.metric} className="border-t">
                      <td className="px-4 py-2 font-medium uppercase">{row.metric}</td>
                      <td className="px-4 py-2 text-right tabular-nums">
                        {formatMetric(row.version_a, 4)}
                      </td>
                      <td className="px-4 py-2 text-right tabular-nums">
                        {formatMetric(row.version_b, 4)}
                      </td>
                      <td
                        className={cn(
                          'px-4 py-2 text-right tabular-nums',
                          row.verdict === 'improved' ? 'text-emerald-500' : '',
                          row.verdict === 'regressed' ? 'text-destructive' : '',
                        )}
                      >
                        {row.delta_pct === null || row.delta_pct === undefined
                          ? formatMetric(row.delta, 4)
                          : formatPercent(row.delta_pct, 1)}
                      </td>
                      <td className="px-4 py-2">
                        <Verdict verdict={row.verdict} />
                      </td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
          </div>
        ) : null}
      </CardContent>
    </Card>
  );
}

function Verdict({ verdict }: { readonly verdict: string }) {
  if (verdict === 'improved') {
    return (
      <span className="inline-flex items-center gap-1 text-xs text-emerald-500">
        <ArrowDown aria-hidden="true" className="h-3 w-3" />
        improved
      </span>
    );
  }
  if (verdict === 'regressed') {
    return (
      <span className="inline-flex items-center gap-1 text-xs text-destructive">
        <ArrowUp aria-hidden="true" className="h-3 w-3" />
        regressed
      </span>
    );
  }
  if (verdict === 'unchanged') {
    return (
      <span className="inline-flex items-center gap-1 text-xs text-muted-foreground">
        <Minus aria-hidden="true" className="h-3 w-3" />
        unchanged
      </span>
    );
  }
  return (
    <span className="text-xs text-muted-foreground">
      not recorded for both
    </span>
  );
}
