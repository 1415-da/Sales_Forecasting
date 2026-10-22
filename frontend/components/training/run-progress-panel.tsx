'use client';

import { CheckCircle2, Circle, ExternalLink, Loader2, XCircle } from 'lucide-react';

import { Badge } from '@/components/ui/badge';
import { Card, CardContent, CardDescription, CardHeader, CardTitle } from '@/components/ui/card';
import type { TaskState, TrainingRunDetail } from '@/lib/types/training';

const STATUS_LABEL: Record<string, { label: string; variant: 'success' | 'warning' | 'destructive' | 'secondary' }> = {
  pending: { label: 'Pending submission', variant: 'secondary' },
  submission_uncertain: { label: 'Confirming submission', variant: 'warning' },
  queued: { label: 'Queued', variant: 'secondary' },
  running: { label: 'Running', variant: 'warning' },
  succeeded: { label: 'Completed', variant: 'success' },
  failed: { label: 'Failed', variant: 'destructive' },
};

const TASK_LABEL: Record<string, string> = {
  extract_data_task: 'Load the exact dataset version',
  validate_data_task: 'Re-validate the full dataset',
  train_models_task: 'Train XGBoost, LightGBM and ensemble',
  evaluate_models_task: 'Rank by test-window error',
  register_best_model_task: 'Register candidates (no promotion)',
  transition_to_production_task: 'Production promotion (skipped: manual review)',
  quality_gate_task: 'Quality gate on reloaded artifacts',
  compare_with_production_task: 'Compare with the current model',
  summarize_training_request_task: 'Record bundle contract and report',
  generate_performance_report_task: 'Performance report',
  cleanup: 'Clean up temporary files',
};

function TaskIcon({ state }: { state: string | null }) {
  if (state === 'success') return <CheckCircle2 className="h-4 w-4 text-emerald-600" aria-hidden />;
  if (state === 'failed' || state === 'upstream_failed')
    return <XCircle className="h-4 w-4 text-destructive" aria-hidden />;
  if (state === 'running' || state === 'queued' || state === 'up_for_retry')
    return <Loader2 className="h-4 w-4 animate-spin text-amber-600" aria-hidden />;
  return <Circle className="h-4 w-4 text-muted-foreground" aria-hidden />;
}

function time(value: string | null | undefined): string {
  return value ? new Date(value).toLocaleString() : '—';
}

function TaskRow({ task }: { task: TaskState }) {
  return (
    <li className="flex items-center justify-between gap-3 border-t py-1.5 text-sm first:border-t-0">
      <span className="flex items-center gap-2">
        <TaskIcon state={task.state} />
        {TASK_LABEL[task.task_id] ?? task.task_id}
      </span>
      <span className="text-xs text-muted-foreground">
        {task.state ?? 'scheduled'}
        {task.duration ? ` · ${task.duration.toFixed(0)}s` : ''}
        {task.try_number && task.try_number > 1 ? ` · attempt ${task.try_number}` : ''}
      </span>
    </li>
  );
}

/** Step 4: the Airflow run, as Airflow reports it. */
export function RunProgressPanel({ run }: { run: TrainingRunDetail }) {
  const status = STATUS_LABEL[run.status] ?? { label: run.status, variant: 'secondary' as const };
  const done = run.task_states.filter((task) => task.state === 'success').length;
  return (
    <Card>
      <CardHeader>
        <div className="flex flex-wrap items-start justify-between gap-2">
          <div>
            <CardTitle className="text-base">6. Training progress</CardTitle>
            <CardDescription>
              {run.dataset_name} v{run.dataset_version} · {String(run.training_config.label ?? run.training_config_id)}
            </CardDescription>
          </div>
          <Badge variant={status.variant}>{status.label}</Badge>
        </div>
      </CardHeader>
      <CardContent className="space-y-3">
        <dl className="grid gap-x-4 gap-y-1 text-xs sm:grid-cols-2">
          <div className="break-all">
            <dt className="inline text-muted-foreground">Request ID: </dt>
            <dd className="inline font-mono">{run.id}</dd>
          </div>
          <div className="break-all">
            <dt className="inline text-muted-foreground">DAG run: </dt>
            <dd className="inline font-mono">
              {run.dag_id} / {run.dag_run_id}
            </dd>
          </div>
          <div>
            <dt className="inline text-muted-foreground">Requested: </dt>
            <dd className="inline">
              {time(run.created_at)} by {run.created_by ?? 'unknown'}
            </dd>
          </div>
          <div>
            <dt className="inline text-muted-foreground">Submitted: </dt>
            <dd className="inline">{time(run.submitted_at)}</dd>
          </div>
          <div>
            <dt className="inline text-muted-foreground">Started: </dt>
            <dd className="inline">{time(run.started_at)}</dd>
          </div>
          <div>
            <dt className="inline text-muted-foreground">Finished: </dt>
            <dd className="inline">{time(run.finished_at)}</dd>
          </div>
          <div>
            <dt className="inline text-muted-foreground">Airflow state: </dt>
            <dd className="inline">{run.airflow_state ?? 'not yet reported'}</dd>
          </div>
          <div>
            <dt className="inline text-muted-foreground">Last checked: </dt>
            <dd className="inline">{time(run.last_synced_at)}</dd>
          </div>
        </dl>

        {run.failure_reason ? (
          <p role="alert" className="rounded-md border border-destructive/40 p-2 text-sm text-destructive">
            {run.failure_reason}
          </p>
        ) : null}
        {run.sync_warning ? <p className="text-sm text-amber-400">{run.sync_warning}</p> : null}

        {run.task_states.length > 0 ? (
          <div>
            <p className="mb-1 text-xs text-muted-foreground">
              {done} of {run.task_states.length} tasks complete (as reported by Airflow)
            </p>
            <ol>
              {run.task_states.map((task) => (
                <TaskRow key={task.task_id} task={task} />
              ))}
            </ol>
          </div>
        ) : (
          <p className="text-sm text-muted-foreground">Waiting for Airflow to schedule the run…</p>
        )}

        {run.airflow_url ? (
          <a
            href={run.airflow_url}
            target="_blank"
            rel="noreferrer"
            className="inline-flex items-center gap-1 text-sm text-primary underline-offset-4 hover:underline"
          >
            Open the run in Airflow <ExternalLink className="h-3.5 w-3.5" aria-hidden />
          </a>
        ) : null}
      </CardContent>
    </Card>
  );
}
