'use client';

import { ExternalLink, X } from 'lucide-react';

import { ErrorState } from '@/components/shared/error-state';
import { StatusBadge, stageTone } from '@/components/shared/status-badge';
import { Button } from '@/components/ui/button';
import { formatMetric, formatTimestamp } from '@/lib/format';
import type { ModelInfo } from '@/lib/types';
import type { ModelArtifacts, ModelLineage } from '@/lib/types/auth';

export interface DetailDrawerProps {
  readonly model: ModelInfo;
  readonly lineage: ModelLineage | undefined;
  readonly artifacts: ModelArtifacts | undefined;
  readonly isLoading: boolean;
  readonly error: unknown;
  readonly onClose: () => void;
}

/**
 * Everything known about one version.
 *
 * Artifacts are listed by role and availability, never by storage path: the
 * API loads them itself, and handing a browser a bucket location would both
 * leak infrastructure and invite bypassing the API.
 *
 * "Available" here means the bundle actually loaded, so this answers "can this
 * version serve a forecast" rather than "does a file exist".
 */
export function DetailDrawer({
  model,
  lineage,
  artifacts,
  isLoading,
  error,
  onClose,
}: DetailDrawerProps) {
  return (
    <div
      role="dialog"
      aria-modal="true"
      aria-labelledby="detail-title"
      className="fixed inset-0 z-40 flex justify-end bg-background/70 backdrop-blur-sm"
      onClick={(event) => {
        if (event.target === event.currentTarget) onClose();
      }}
    >
      <aside className="h-full w-full max-w-xl overflow-y-auto border-l bg-card p-6">
        <div className="flex items-start justify-between gap-4">
          <div className="space-y-1">
            <h2 id="detail-title" className="text-base font-semibold">
              {model.label} version {model.version}
            </h2>
            <div className="flex items-center gap-2">
              <StatusBadge tone={stageTone(model.stage)}>{model.stage}</StatusBadge>
              {model.is_default ? (
                <StatusBadge tone="neutral">default model</StatusBadge>
              ) : null}
            </div>
          </div>
          <Button variant="ghost" size="icon" onClick={onClose} aria-label="Close">
            <X aria-hidden="true" className="h-4 w-4" />
          </Button>
        </div>

        {error ? (
          <div className="mt-6">
            <ErrorState error={error} />
          </div>
        ) : null}

        {isLoading ? (
          <p className="mt-6 text-sm text-muted-foreground">Loading detail…</p>
        ) : null}

        <section className="mt-6 space-y-3">
          <h3 className="text-sm font-semibold">Training metrics</h3>
          <dl className="grid gap-x-6 gap-y-2 text-xs sm:grid-cols-2">
            {Object.entries(lineage?.metrics ?? {}).length === 0 ? (
              <p className="text-muted-foreground">
                No metrics are recorded for this version.
              </p>
            ) : (
              Object.entries(lineage?.metrics ?? {}).map(([name, value]) => (
                <Field key={name} label={name.toUpperCase()} value={formatMetric(value, 4)} />
              ))
            )}
          </dl>
        </section>

        <section className="mt-6 space-y-3">
          <h3 className="text-sm font-semibold">Lineage</h3>
          <dl className="grid gap-x-6 gap-y-2 text-xs">
            <Field label="Run ID" value={lineage?.run_id ?? '—'} mono />
            <Field
              label="Trained"
              value={
                lineage?.training_date
                  ? formatTimestamp(lineage.training_date)
                  : '—'
              }
            />
            {Object.entries(lineage?.dataset ?? {}).map(([name, value]) => (
              <Field
                key={name}
                label={name.replace(/_/g, ' ')}
                value={String(value)}
                mono={name.includes('hash')}
              />
            ))}
          </dl>
          {lineage?.mlflow_run_url ? (
            <a
              href={lineage.mlflow_run_url}
              target="_blank"
              rel="noreferrer noopener"
              className="inline-flex items-center gap-1 text-xs font-medium underline underline-offset-4"
            >
              Open the run in MLflow
              <ExternalLink aria-hidden="true" className="h-3 w-3" />
            </a>
          ) : null}
        </section>

        <section className="mt-6 space-y-3">
          <h3 className="text-sm font-semibold">Artifacts</h3>
          {artifacts?.warnings.map((warning) => (
            <p key={warning} className="text-xs text-amber-500">
              {warning}
            </p>
          ))}
          <ul className="space-y-2">
            {(artifacts?.artifacts ?? []).map((artifact) => (
              <li
                key={artifact.name}
                className="flex items-start justify-between gap-3 rounded-md border p-3"
              >
                <div className="min-w-0 space-y-0.5">
                  <p className="font-mono text-xs">{artifact.name}</p>
                  <p className="text-xs text-muted-foreground">
                    {artifact.description}
                  </p>
                </div>
                <StatusBadge tone={artifact.available ? 'success' : 'danger'}>
                  {artifact.available ? 'loaded' : 'unavailable'}
                </StatusBadge>
              </li>
            ))}
          </ul>
          {artifacts?.feature_count ? (
            <p className="text-xs text-muted-foreground">
              {artifacts.feature_count} model inputs.
            </p>
          ) : null}
        </section>

        {model.supported_stores.length > 0 ? (
          <section className="mt-6 space-y-2">
            <h3 className="text-sm font-semibold">Stores</h3>
            <p className="text-xs text-muted-foreground">
              Identifiers this version's encoder recognises. A store outside
              this list is refused rather than mapped to another.
            </p>
            <div className="flex flex-wrap gap-1.5">
              {model.supported_stores.map((store) => (
                <StatusBadge key={store} tone="neutral">
                  {store}
                </StatusBadge>
              ))}
            </div>
          </section>
        ) : null}
      </aside>
    </div>
  );
}

function Field({
  label,
  value,
  mono = false,
}: {
  readonly label: string;
  readonly value: string;
  readonly mono?: boolean;
}) {
  return (
    <div className="flex justify-between gap-4">
      <dt className="shrink-0 capitalize text-muted-foreground">{label}</dt>
      <dd
        className={
          mono
            ? 'break-all text-right font-mono text-[11px]'
            : 'text-right font-medium'
        }
      >
        {value}
      </dd>
    </div>
  );
}
