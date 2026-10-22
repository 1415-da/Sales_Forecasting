'use client';

import { CheckCircle2, Download, Lock, ShieldAlert } from 'lucide-react';
import { useState } from 'react';

import { Badge } from '@/components/ui/badge';
import { Button } from '@/components/ui/button';
import { Card, CardContent, CardDescription, CardHeader, CardTitle } from '@/components/ui/card';
import { useSealDataset } from '@/hooks/use-training';
import { downloadValidationReport, friendlyMessage } from '@/lib/api';
import type { TrainingDatasetDetail, ValidationIssue } from '@/lib/types/training';

const STATUS_BADGE: Record<string, { label: string; variant: 'success' | 'warning' | 'destructive' }> = {
  ready: { label: 'Sealed (immutable)', variant: 'success' },
  validated: { label: 'Validated, not sealed', variant: 'warning' },
  invalid: { label: 'Invalid', variant: 'destructive' },
};

function IssueList({ title, issues, tone }: { title: string; issues: ValidationIssue[]; tone: 'error' | 'warning' }) {
  if (issues.length === 0) return null;
  return (
    <div className="space-y-2">
      <h4 className={tone === 'error' ? 'text-sm font-semibold text-destructive' : 'text-sm font-semibold text-amber-400'}>
        {title}
      </h4>
      <ul className="space-y-2">
        {issues.map((issue) => (
          <li key={issue.code} className="rounded-md border p-2 text-sm">
            <p>
              <code className="text-xs">{issue.code}</code>
              {issue.count ? <span className="ml-2 text-muted-foreground">({issue.count.toLocaleString()})</span> : null}
            </p>
            <p className="text-muted-foreground">{issue.message}</p>
            {issue.examples.length > 0 ? (
              <div className="mt-1 overflow-x-auto">
                <table className="text-xs">
                  <tbody>
                    {issue.examples.slice(0, 5).map((example, index) => (
                      <tr key={index}>
                        <td className="pr-2 font-mono text-muted-foreground">
                          {Object.entries(example)
                            .map(([key, value]) => `${key}=${String(value)}`)
                            .join('  ')}
                        </td>
                      </tr>
                    ))}
                  </tbody>
                </table>
              </div>
            ) : null}
          </li>
        ))}
      </ul>
    </div>
  );
}

export interface ValidationPanelProps {
  dataset: TrainingDatasetDetail;
  onSealed: (dataset: TrainingDatasetDetail) => void;
}

/** Step 2: what the validator found, and sealing the version. */
export function ValidationPanel({ dataset, onSealed }: ValidationPanelProps) {
  const seal = useSealDataset();
  const [downloadError, setDownloadError] = useState<string | null>(null);
  const report = dataset.validation_report;
  const status = STATUS_BADGE[dataset.status] ?? { label: dataset.status, variant: 'destructive' as const };
  const combination = report.combination as Record<string, unknown> | null | undefined;
  const split = report.split as Record<string, unknown> | null | undefined;
  const preview = report.preview ?? [];
  const previewColumns = preview.length > 0 ? Object.keys(preview[0] as object) : [];

  return (
    <Card>
      <CardHeader>
        <div className="flex flex-wrap items-start justify-between gap-2">
          <div>
            <CardTitle className="text-base">2. Validation: {dataset.name} v{dataset.version}</CardTitle>
            <CardDescription>
              {dataset.original_filename ?? 'upload'} · {(dataset.raw_size_bytes / 1024).toFixed(0)} KB ·{' '}
              {dataset.mode === 'append' ? 'appended to an approved version' : 'independent'} ·{' '}
              {dataset.source_types.join(', ')} · {dataset.currency}
            </CardDescription>
          </div>
          <Badge variant={status.variant}>{status.label}</Badge>
        </div>
      </CardHeader>
      <CardContent className="space-y-4">
        <dl className="grid grid-cols-2 gap-3 text-sm sm:grid-cols-4">
          <div>
            <dt className="text-muted-foreground">Rows received</dt>
            <dd className="font-medium">{report.rows_received?.toLocaleString() ?? '—'}</dd>
          </div>
          <div>
            <dt className="text-muted-foreground">
              {combination && !combination.errors ? 'Valid rows after combining' : 'Valid / invalid'}
            </dt>
            <dd className="font-medium">
              {report.rows_valid?.toLocaleString() ?? '—'} / {report.rows_invalid?.toLocaleString() ?? 0}
            </dd>
          </div>
          <div>
            <dt className="text-muted-foreground">Date range</dt>
            <dd className="font-medium">
              {report.date_min ?? '—'} to {report.date_max ?? '—'}
            </dd>
          </div>
          <div>
            <dt className="text-muted-foreground">Stores / dates</dt>
            <dd className="font-medium">
              {report.store_count ?? '—'} / {report.total_dates?.toLocaleString() ?? '—'}
            </dd>
          </div>
          <div>
            <dt className="text-muted-foreground">Exact duplicates removed</dt>
            <dd className="font-medium">{report.exact_duplicates_removed ?? 0}</dd>
          </div>
          <div>
            <dt className="text-muted-foreground">Conflicting duplicates</dt>
            <dd className="font-medium">{report.conflicting_duplicates ?? 0}</dd>
          </div>
          {split && split.valid ? (
            <div className="col-span-2">
              <dt className="text-muted-foreground">Chronological split (train · validation · test)</dt>
              <dd className="font-medium">
                {String(split.train_days)} · {String(split.validation_days)} · {String(split.test_days)} days; test{' '}
                {(split.test as string[]).join(' to ')}
              </dd>
            </div>
          ) : null}
        </dl>

        {combination && !combination.errors ? (
          <div className="rounded-md border bg-muted/40 p-3 text-sm">
            <p className="font-medium">Combined with the base version</p>
            <p className="text-muted-foreground">
              {String(combination.base_rows)} base rows + {String(combination.upload_rows)} uploaded rows;{' '}
              {String(combination.overlapping_store_days ?? 0)} overlapping store-days,{' '}
              {String(combination.exact_duplicates_removed ?? 0)} identical (de-duplicated),{' '}
              {String(combination.corrections_applied ?? 0)} corrected ({String(combination.conflict_policy)}).
              Result: {String(combination.rows)} rows.
            </p>
          </div>
        ) : null}

        {Object.keys(report.context_features ?? {}).length > 0 ? (
          <p className="text-sm text-muted-foreground">
            Context features:{' '}
            {Object.entries(report.context_features)
              .map(([feature, how]) => `${feature} (${how})`)
              .join('; ')}
          </p>
        ) : null}

        <IssueList title="Errors (must be fixed)" issues={report.errors} tone="error" />
        <IssueList title="Warnings" issues={report.warnings} tone="warning" />

        {report.stores.length > 0 ? (
          <div className="overflow-x-auto">
            <h4 className="mb-1 text-sm font-semibold">History per store</h4>
            <table className="w-full text-sm">
              <thead className="text-left text-xs text-muted-foreground">
                <tr>
                  <th className="py-1 pr-3">Store</th>
                  <th className="py-1 pr-3">Days</th>
                  <th className="py-1 pr-3">First</th>
                  <th className="py-1 pr-3">Last</th>
                  <th className="py-1 pr-3">Missing</th>
                  <th className="py-1 pr-3">Zero-sales days</th>
                  <th className="py-1">Sufficient</th>
                </tr>
              </thead>
              <tbody>
                {report.stores.map((store) => (
                  <tr key={store.store_id} className="border-t">
                    <td className="py-1 pr-3 font-medium">{store.store_id}</td>
                    <td className="py-1 pr-3">{store.days}</td>
                    <td className="py-1 pr-3">{store.first_date}</td>
                    <td className="py-1 pr-3">{store.last_date}</td>
                    <td className="py-1 pr-3">{store.missing_dates}</td>
                    <td className="py-1 pr-3">{store.zero_sales_days}</td>
                    <td className="py-1">
                      {store.sufficient ? (
                        <CheckCircle2 className="h-4 w-4 text-emerald-600" aria-label="Sufficient" />
                      ) : (
                        <span className="text-destructive">{store.reason}</span>
                      )}
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        ) : null}

        {preview.length > 0 ? (
          <div className="overflow-x-auto">
            <h4 className="mb-1 text-sm font-semibold">Preview (first {preview.length} validated rows)</h4>
            <table className="text-xs">
              <thead className="text-left text-muted-foreground">
                <tr>
                  {previewColumns.map((column) => (
                    <th key={column} className="pr-4">
                      {column}
                    </th>
                  ))}
                </tr>
              </thead>
              <tbody>
                {preview.map((row, index) => (
                  <tr key={index}>
                    {previewColumns.map((column) => (
                      <td key={column} className="pr-4 font-mono">
                        {String((row as Record<string, unknown>)[column])}
                      </td>
                    ))}
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        ) : null}

        {dataset.status === 'ready' ? (
          <div className="rounded-md border border-emerald-500/30 bg-emerald-500/10 p-3 text-xs text-emerald-300">
            <p className="flex items-center gap-1 font-medium">
              <Lock className="h-3.5 w-3.5" aria-hidden /> Immutable version, sealed{' '}
              {dataset.sealed_at ? new Date(dataset.sealed_at).toLocaleString() : ''}
            </p>
            <p className="mt-1 break-all font-mono">content sha256 {dataset.content_sha256}</p>
            <p className="break-all font-mono">manifest sha256 {dataset.manifest_sha256}</p>
          </div>
        ) : null}

        {seal.isError ? (
          <p role="alert" className="text-sm text-destructive">
            {seal.error.message || friendlyMessage(seal.error)}
          </p>
        ) : null}
        {downloadError ? (
          <p role="alert" className="text-sm text-destructive">
            {downloadError}
          </p>
        ) : null}

        <div className="flex flex-wrap gap-2">
          {dataset.status === 'validated' ? (
            <Button onClick={() => seal.mutate(dataset.id, { onSuccess: onSealed })} disabled={seal.isPending}>
              <Lock className="mr-1 h-4 w-4" aria-hidden />
              {seal.isPending ? 'Re-validating from storage…' : 'Create immutable version'}
            </Button>
          ) : null}
          {dataset.status === 'invalid' ? (
            <p className="flex items-center gap-1 text-sm text-destructive">
              <ShieldAlert className="h-4 w-4" aria-hidden /> Fix the errors and upload again; invalid
              uploads are never trained on.
            </p>
          ) : null}
          <Button
            variant="outline"
            onClick={() =>
              downloadValidationReport(dataset.id).catch((error: Error) => setDownloadError(error.message))
            }
          >
            <Download className="mr-1 h-4 w-4" aria-hidden /> Download validation report
          </Button>
        </div>
      </CardContent>
    </Card>
  );
}
