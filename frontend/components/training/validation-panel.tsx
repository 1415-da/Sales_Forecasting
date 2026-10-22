'use client';

import { AlertTriangle, CheckCircle2, Download, Loader2, Lock, RotateCcw, ServerCrash, ShieldAlert } from 'lucide-react';
import { useEffect, useState } from 'react';

import { Badge } from '@/components/ui/badge';
import { Button } from '@/components/ui/button';
import { Card, CardContent, CardDescription, CardHeader, CardTitle } from '@/components/ui/card';
import { formatBytes } from '@/components/training/upload-card';
import { useRetryDataset, useSealDataset } from '@/hooks/use-training';
import { downloadValidationReport, friendlyMessage } from '@/lib/api';
import { stageNames, type TrainingDatasetDetail, type ValidationIssue } from '@/lib/types/training';

const STATUS_BADGE: Record<string, { label: string; variant: 'success' | 'warning' | 'destructive' | 'secondary' }> = {
  processing: { label: 'Processing', variant: 'secondary' },
  needs_mapping: { label: 'Needs confirmation', variant: 'warning' },
  invalid: { label: 'Invalid', variant: 'destructive' },
  validated: { label: 'Validated, not sealed', variant: 'warning' },
  ready: { label: 'Sealed (immutable)', variant: 'success' },
  failed: { label: 'Processing failed', variant: 'destructive' },
};

const STAGE_LABEL: Record<string, string> = {
  file: 'Files',
  detection: 'Detection',
  mapping: 'Mapping',
  schema: 'Schema',
  rows: 'Rows',
  readiness: 'Training readiness',
  append: 'Append',
  deployment: 'Deployment compatibility',
};

const STAGE_STYLE: Record<string, string> = {
  passed: 'border-emerald-500/40 text-emerald-400',
  warning: 'border-amber-500/40 text-amber-400',
  failed: 'border-destructive/60 text-destructive',
  not_run: 'border-border text-muted-foreground',
};

function count(value: number | null | undefined): string {
  return value == null ? '—' : value.toLocaleString();
}

function IssueList({ issues, tone }: { issues: ValidationIssue[]; tone: 'error' | 'warning' }) {
  return (
    <ul className="space-y-2">
      {issues.map((issue, index) => (
        <li key={`${issue.code}-${index}`} className="rounded-md border border-current/20 p-2 text-sm">
          <p className="flex flex-wrap items-center gap-2">
            <code className="text-xs font-semibold">{issue.code}</code>
            {issue.stage ? <span className="text-xs opacity-80">stage: {STAGE_LABEL[issue.stage] ?? issue.stage}</span> : null}
            {issue.file ? <span className="text-xs opacity-80">file: {issue.file}</span> : null}
            {issue.column ? <span className="text-xs opacity-80">column: {issue.column}</span> : null}
            {issue.count ? <span className="text-xs opacity-80">affected: {issue.count.toLocaleString()}</span> : null}
          </p>
          <p className={tone === 'error' ? 'text-destructive' : 'text-amber-300'}>{issue.message}</p>
          {issue.action ? <p className="text-xs">What to do: {issue.action}</p> : null}
          {issue.examples.length > 0 ? (
            <div className="mt-1 overflow-x-auto">
              <table className="text-xs">
                <tbody>
                  {issue.examples.slice(0, 5).map((example, row) => (
                    <tr key={row}>
                      <td className="pr-2 font-mono opacity-80">
                        {Object.entries(example)
                          .map(([key, value]) => `${key}=${value === null ? 'blank' : String(value)}`)
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
  );
}

export interface ValidationPanelProps {
  dataset: TrainingDatasetDetail;
  onSealed: (dataset: TrainingDatasetDetail) => void;
}

/** Steps 3 and 4: what each stage found; confirming creates the immutable version. */
export function ValidationPanel({ dataset, onSealed }: ValidationPanelProps) {
  const seal = useSealDataset();
  const retry = useRetryDataset();
  const [downloadError, setDownloadError] = useState<string | null>(null);
  const [acknowledged, setAcknowledged] = useState<string[]>([]);
  const report = dataset.validation_report;
  const status = STATUS_BADGE[dataset.status] ?? { label: dataset.status, variant: 'secondary' as const };
  const errors = report.errors ?? [];
  const warnings = report.warnings ?? [];
  const mustAcknowledge = Array.from(new Set(warnings.filter((item) => item.requires_ack).map((item) => item.code)));
  const rowsChecked = report.row_validation_performed === true;
  const combination = report.combination as Record<string, unknown> | null | undefined;
  const split = report.split as Record<string, unknown> | null | undefined;
  const preview = report.preview ?? [];
  const previewColumns = preview.length > 0 ? Object.keys(preview[0] as object) : [];
  const stores = report.stores ?? [];
  const shownStores = [...stores.filter((store) => !store.sufficient), ...stores.filter((store) => store.sufficient)].slice(0, 25);
  const compatibility = report.deployment_compatibility;

  // A different mapping version is a different validation: acknowledgments do not carry over.
  useEffect(() => setAcknowledged([]), [dataset.id, dataset.mapping_version]);

  const canSeal =
    dataset.status === 'validated' && mustAcknowledge.every((code) => acknowledged.includes(code)) && !seal.isPending;

  return (
    <Card>
      <CardHeader>
        <div className="flex flex-wrap items-start justify-between gap-2">
          <div>
            <CardTitle className="text-base">
              3. Validate and preview: {dataset.name} v{dataset.version}
            </CardTitle>
            <CardDescription>
              {dataset.source_files.map((file) => `${String(file.name)} (${formatBytes(file.size as number)})`).join(', ')} ·{' '}
              {dataset.mode === 'append' ? 'appended to an approved version' : 'independent'} · {dataset.source_types.join(', ')}
              {' · currency '}
              {dataset.currency ?? 'not confirmed'}
              {dataset.adapter_id ? ` · adapter ${dataset.adapter_id} v${dataset.adapter_version}` : ''}
              {dataset.mapping_version ? ` · mapping v${dataset.mapping_version}` : ''}
            </CardDescription>
          </div>
          <Badge variant={status.variant}>{status.label}</Badge>
        </div>
      </CardHeader>
      <CardContent className="space-y-4">
        {dataset.status === 'processing' ? (
          <div role="status" className="flex items-center gap-2 rounded-md border p-3 text-sm">
            <Loader2 className="h-4 w-4 animate-spin" aria-hidden />
            Processing on the server: inspecting files, detecting the source, validating every row. You can leave this
            page; the result is saved and shown when you come back.
          </div>
        ) : null}

        {dataset.status === 'failed' ? (
          <div role="alert" className="space-y-2 rounded-md border border-destructive/60 p-3 text-sm">
            <p className="flex items-center gap-2 font-medium text-destructive">
              <ServerCrash className="h-4 w-4" aria-hidden /> Processing did not finish. This is a service problem,
              not a verdict on the data.
            </p>
            <p className="text-muted-foreground">{dataset.processing_error}</p>
            {retry.isError ? <p className="text-destructive">{retry.error.message || friendlyMessage(retry.error)}</p> : null}
            <Button size="sm" variant="outline" onClick={() => retry.mutate(dataset.id)} disabled={retry.isPending}>
              <RotateCcw className="mr-1 h-4 w-4" aria-hidden /> {retry.isPending ? 'Retrying…' : 'Retry processing'}
            </Button>
          </div>
        ) : null}

        {Object.keys(report.stages ?? {}).length > 0 ? (
          <ol className="flex flex-wrap gap-2" aria-label="Validation stages">
            {stageNames.map((stage) => {
              const result = report.stages[stage]?.status ?? 'not_run';
              return (
                <li key={stage} className={`rounded-full border px-2 py-0.5 text-xs ${STAGE_STYLE[result] ?? ''}`}>
                  {STAGE_LABEL[stage]}: {result === 'not_run' ? 'not run' : result}
                </li>
              );
            })}
          </ol>
        ) : null}

        {report.adapter_message ? (
          <p className="rounded-md border border-sky-500/30 bg-sky-500/5 p-2 text-sm text-sky-300">{report.adapter_message}</p>
        ) : null}

        <dl className="grid grid-cols-2 gap-3 text-sm sm:grid-cols-4">
          <div>
            <dt className="text-muted-foreground">Rows received</dt>
            <dd className="font-medium">{count(report.rows_received)}</dd>
          </div>
          <div>
            <dt className="text-muted-foreground">Valid / invalid rows</dt>
            <dd className="font-medium">
              {rowsChecked ? `${count(report.rows_valid)} / ${count(report.rows_invalid)}` : 'Row validation not performed'}
            </dd>
          </div>
          <div>
            <dt className="text-muted-foreground">Rows accepted for training</dt>
            <dd className="font-medium">{count(report.rows_accepted)}</dd>
          </div>
          <div>
            <dt className="text-muted-foreground">Date range</dt>
            <dd className="font-medium">{report.date_min ? `${report.date_min} to ${report.date_max}` : '—'}</dd>
          </div>
          <div>
            <dt className="text-muted-foreground">Stores / distinct dates</dt>
            <dd className="font-medium">
              {count(report.store_count)} / {count(report.total_dates)}
            </dd>
          </div>
          <div>
            <dt className="text-muted-foreground">Duplicates removed / conflicts</dt>
            <dd className="font-medium">
              {count(report.exact_duplicates_removed)} / {count(report.conflicting_duplicates)}
            </dd>
          </div>
          {report.stores_excluded.length > 0 ? (
            <div>
              <dt className="text-muted-foreground">Stores excluded by policy</dt>
              <dd className="font-medium">{report.stores_excluded.length.toLocaleString()}</dd>
            </div>
          ) : null}
          {split && split.valid ? (
            <div>
              <dt className="text-muted-foreground">Train · validation · test days</dt>
              <dd className="font-medium">
                {String(split.train_days)} · {String(split.validation_days)} · {String(split.test_days)}
              </dd>
            </div>
          ) : null}
        </dl>

        {errors.length > 0 ? (
          <section role="alert" className="space-y-2 rounded-md border border-destructive/60 p-3">
            <h4 className="flex items-center gap-2 text-sm font-semibold text-destructive">
              <ShieldAlert className="h-4 w-4" aria-hidden /> {errors.length} blocking issue{errors.length === 1 ? '' : 's'}: this
              upload cannot become a training dataset
            </h4>
            <IssueList issues={errors} tone="error" />
            <p className="text-xs text-muted-foreground">
              Edit the mapping or policies above and apply again, or upload corrected files as a new upload.
            </p>
          </section>
        ) : null}

        {warnings.length > 0 ? (
          <section className="space-y-2 rounded-md border border-amber-500/40 p-3">
            <h4 className="flex items-center gap-2 text-sm font-semibold text-amber-300">
              <AlertTriangle className="h-4 w-4" aria-hidden /> {warnings.length} warning{warnings.length === 1 ? '' : 's'}
            </h4>
            <IssueList issues={warnings} tone="warning" />
            {dataset.status === 'validated' && mustAcknowledge.length > 0 ? (
              <div className="space-y-1">
                {mustAcknowledge.map((code) => (
                  <label key={code} className="flex items-center gap-2 text-sm">
                    <input
                      type="checkbox"
                      checked={acknowledged.includes(code)}
                      onChange={(event) =>
                        setAcknowledged((current) =>
                          event.target.checked ? [...current, code] : current.filter((item) => item !== code),
                        )
                      }
                    />
                    I acknowledge {code}
                  </label>
                ))}
              </div>
            ) : null}
          </section>
        ) : null}

        {compatibility ? (
          <div
            className={`rounded-md border p-3 text-sm ${compatibility.compatible ? 'border-emerald-500/30' : 'border-amber-500/40'}`}
          >
            <p className="font-medium">
              Training readiness: {dataset.status === 'validated' || dataset.status === 'ready' ? 'ready' : 'not ready'} ·
              Deployment compatibility with {compatibility.production_model ?? 'the serving model'}:{' '}
              {compatibility.compatible ? 'compatible' : 'not compatible'}
            </p>
            {compatibility.reasons.map((reason) => (
              <p key={reason} className="text-xs text-muted-foreground">
                {reason}
              </p>
            ))}
            {compatibility.note ? <p className="text-xs text-muted-foreground">{compatibility.note}</p> : null}
          </div>
        ) : null}

        {(report.transformations ?? []).length > 0 ? (
          <div className="overflow-x-auto">
            <h4 className="mb-1 text-sm font-semibold">Source to store_day_v1</h4>
            <table className="w-full text-sm">
              <thead className="text-left text-xs text-muted-foreground">
                <tr>
                  <th className="py-1 pr-3">Source</th>
                  <th className="py-1 pr-3">Canonical</th>
                  <th className="py-1">Rule</th>
                </tr>
              </thead>
              <tbody>
                {(report.transformations ?? []).map((item, index) => (
                  <tr key={index} className="border-t">
                    <td className="py-1 pr-3 font-mono text-xs">{item.source}</td>
                    <td className="py-1 pr-3 font-mono text-xs">{item.target}</td>
                    <td className="py-1 text-xs">{item.rule}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        ) : null}

        {(report.retained_columns ?? []).length > 0 ? (
          <div>
            <h4 className="mb-1 text-sm font-semibold">Retained as context, not used by this training configuration</h4>
            <ul className="space-y-0.5 text-xs">
              {(report.retained_columns ?? []).map((item) => (
                <li key={`${item.file}-${item.column}`}>
                  <code>{item.column}</code> ({item.file}): {item.reason}
                </li>
              ))}
            </ul>
          </div>
        ) : null}

        {report.store_metadata ? (
          <p className="rounded-md border p-2 text-xs">
            Store metadata {String(report.store_metadata.file)}:{' '}
            {report.store_metadata.joined
              ? `${String(report.store_metadata.relationship)}; ${Number(report.store_metadata.rows_before).toLocaleString()} rows before and ${Number(report.store_metadata.rows_after).toLocaleString()} after the join (no multiplication); ${String(report.store_metadata.stores_in_metadata)} stores described.`
              : 'not joined (see errors).'}
          </p>
        ) : null}

        {(report.files?.excluded ?? []).length > 0 ? (
          <p className="text-xs text-muted-foreground">
            Excluded files:{' '}
            {(report.files?.excluded ?? []).map((item) => `${String(item.name)} (${String(item.reason)})`).join('; ')}
          </p>
        ) : null}

        {combination && !combination.errors ? (
          <p className="rounded-md border bg-muted/40 p-2 text-sm text-muted-foreground">
            Combined with the base version: {String(combination.base_rows)} base rows + {String(combination.upload_rows)}{' '}
            uploaded; {String(combination.overlapping_store_days ?? 0)} overlapping store-days,{' '}
            {String(combination.exact_duplicates_removed ?? 0)} identical, {String(combination.corrections_applied ?? 0)}{' '}
            corrected. Result: {String(combination.rows)} rows.
          </p>
        ) : null}

        {shownStores.length > 0 ? (
          <div className="overflow-x-auto">
            <h4 className="mb-1 text-sm font-semibold">
              History per store{' '}
              {stores.length > shownStores.length ? `(${shownStores.length} of ${stores.length}; insufficient first)` : ''}
            </h4>
            <table className="w-full text-sm">
              <thead className="text-left text-xs text-muted-foreground">
                <tr>
                  <th className="py-1 pr-3">Store</th>
                  <th className="py-1 pr-3">Days</th>
                  <th className="py-1 pr-3">First</th>
                  <th className="py-1 pr-3">Last</th>
                  <th className="py-1 pr-3">Zero-sales days</th>
                  <th className="py-1">Sufficient</th>
                </tr>
              </thead>
              <tbody>
                {shownStores.map((store) => (
                  <tr key={store.store_id} className="border-t">
                    <td className="py-1 pr-3 font-medium">{store.store_id}</td>
                    <td className="py-1 pr-3">{store.days}</td>
                    <td className="py-1 pr-3">{store.first_date}</td>
                    <td className="py-1 pr-3">{store.last_date}</td>
                    <td className="py-1 pr-3">{store.zero_sales_days}</td>
                    <td className="py-1">
                      {store.sufficient ? (
                        <CheckCircle2 className="h-4 w-4 text-emerald-500" aria-label="Sufficient" />
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
            <h4 className="mb-1 text-sm font-semibold">Canonical preview (first {preview.length} rows, computed on the server)</h4>
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
            {dataset.acknowledged_warnings.length ? <p>Acknowledged: {dataset.acknowledged_warnings.join(', ')}</p> : null}
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
            <Button
              onClick={() =>
                seal.mutate(
                  { datasetId: dataset.id, expectedMappingVersion: dataset.mapping_version, acknowledgedWarnings: acknowledged },
                  { onSuccess: onSealed },
                )
              }
              disabled={!canSeal}
            >
              <Lock className="mr-1 h-4 w-4" aria-hidden />
              {seal.isPending ? 'Creating version…' : '4. Confirm: create immutable version'}
            </Button>
          ) : null}
          {dataset.status !== 'processing' ? (
            <Button
              variant="outline"
              onClick={() => downloadValidationReport(dataset.id).catch((error: Error) => setDownloadError(error.message))}
            >
              <Download className="mr-1 h-4 w-4" aria-hidden /> Download validation report
            </Button>
          ) : null}
        </div>
      </CardContent>
    </Card>
  );
}
