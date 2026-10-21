'use client';

import { CheckCircle2, ShieldCheck } from 'lucide-react';

import { StatusBadge } from '@/components/shared/status-badge';
import { Card, CardContent, CardDescription, CardHeader, CardTitle } from '@/components/ui/card';
import { formatMetric, formatRelative } from '@/lib/format';
import type { ModelInfo } from '@/lib/types';

/**
 * The version currently serving.
 *
 * Given its own card because it is the single most consequential fact on the
 * page: every forecast the platform produces comes from this version, and
 * finding it by scanning a stage column is unnecessary friction.
 */
export function ProductionModelCard({
  model,
}: {
  readonly model: ModelInfo | undefined;
}) {
  if (!model) {
    return (
      <Card className="border-dashed">
        <CardHeader className="space-y-1">
          <CardTitle className="flex items-center gap-2">
            <ShieldCheck aria-hidden="true" className="h-4 w-4 text-muted-foreground" />
            No Production model
          </CardTitle>
          <CardDescription>
            The API cannot serve a forecast until a version is promoted.
          </CardDescription>
        </CardHeader>
        <CardContent>
          <p className="text-sm text-muted-foreground">
            Run the <span className="font-mono text-xs">sales_forecast_training</span>{' '}
            DAG in Airflow. It trains, registers, and promotes a model; this page
            picks it up automatically.
          </p>
        </CardContent>
      </Card>
    );
  }

  return (
    <Card className="border-emerald-500/30 bg-emerald-500/5">
      <CardHeader className="flex flex-row flex-wrap items-start justify-between gap-3">
        <div className="space-y-1">
          <CardTitle className="flex items-center gap-2">
            <CheckCircle2 aria-hidden="true" className="h-4 w-4 text-emerald-500" />
            Serving production traffic
          </CardTitle>
          <CardDescription>
            Every forecast the platform produces comes from this version.
          </CardDescription>
        </div>
        <StatusBadge tone="success">
          {model.label} v{model.version}
        </StatusBadge>
      </CardHeader>
      <CardContent>
        <dl className="grid gap-x-6 gap-y-2 text-xs sm:grid-cols-3">
          <Field label="RMSE" value={formatMetric(model.metrics.rmse)} />
          <Field label="MAE" value={formatMetric(model.metrics.mae)} />
          <Field
            label="R²"
            value={formatMetric(model.metrics.r2, 4)}
          />
          <Field
            label="Promoted"
            value={model.training_date ? formatRelative(model.training_date) : '—'}
          />
          <Field label="Features" value={`${model.feature_count ?? '—'}`} />
          <Field
            label="Trained on"
            value={
              model.data_source
                ? `${model.data_source} ${model.dataset_version ?? ''}`.trim()
                : '—'
            }
          />
        </dl>
      </CardContent>
    </Card>
  );
}

function Field({
  label,
  value,
}: {
  readonly label: string;
  readonly value: string;
}) {
  return (
    <div className="flex justify-between gap-3">
      <dt className="text-muted-foreground">{label}</dt>
      <dd className="text-right font-medium tabular-nums">{value}</dd>
    </div>
  );
}
