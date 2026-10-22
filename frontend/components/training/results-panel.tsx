'use client';

import { CheckCircle2, ExternalLink, Rocket, XCircle } from 'lucide-react';
import Link from 'next/link';
import { useState } from 'react';

import { Badge } from '@/components/ui/badge';
import { Button } from '@/components/ui/button';
import { Card, CardContent, CardDescription, CardHeader, CardTitle } from '@/components/ui/card';
import { Dialog } from '@/components/ui/dialog';
import { Input } from '@/components/ui/input';
import { Label } from '@/components/ui/label';
import { Textarea } from '@/components/ui/textarea';
import { useRegistryMutation } from '@/hooks/use-registry';
import { useRejectCandidate } from '@/hooks/use-training';
import { ApiError, friendlyMessage } from '@/lib/api';
import type { Candidate, TrainingRunDetail } from '@/lib/types/training';

type Metrics = Record<string, number | undefined>;
type Gate = { passed: boolean; checks: { check: string; passed: boolean; detail: string }[] };
type Comparison = {
  comparable: boolean;
  reason: string;
  champion_version?: string;
  candidate_wape?: number | null;
  champion_wape?: number | null;
  wape_change_points?: number | null;
  rows?: number;
};

const LABEL: Record<string, string> = { ensemble: 'Ensemble', xgboost: 'XGBoost', lightgbm: 'LightGBM' };

function fmt(value: number | null | undefined, digits = 2, suffix = ''): string {
  return typeof value === 'number' && Number.isFinite(value) ? `${value.toFixed(digits)}${suffix}` : '—';
}

function checksOf(details: unknown): Gate['checks'] {
  const checks = (details as { checks?: unknown } | undefined)?.checks;
  return Array.isArray(checks) ? (checks as Gate['checks']) : [];
}

/** Step 6: what was produced, how it scored, and the decision. */
export function ResultsPanel({ run }: { run: TrainingRunDetail }) {
  const evaluation = run.evaluation as {
    metrics?: Record<string, Metrics>;
    quality_gate?: Record<string, Gate>;
    comparison?: Record<string, Comparison>;
    split?: { test?: string[] };
    test_rows?: number;
    compatibility?: { supported_stores?: string[]; currency?: string; target?: string; frequency?: string; feature_count?: number };
  };
  const promote = useRegistryMutation();
  const reject = useRejectCandidate(run.id);
  const [action, setAction] = useState<{ kind: 'approve' | 'reject'; candidate: Candidate } | null>(null);
  const [reason, setReason] = useState('');
  const [typedIdentity, setTypedIdentity] = useState('');

  if (run.status !== 'succeeded' || run.candidates.length === 0) {
    return (
      <Card>
        <CardHeader>
          <CardTitle className="text-base">7. Results and approval</CardTitle>
        </CardHeader>
        <CardContent>
          <p className="text-sm text-muted-foreground">
            {run.status === 'failed'
              ? 'This run failed, so it produced no deployable candidate. The current Production model is unchanged.'
              : 'Candidates appear here when the run completes.'}
          </p>
        </CardContent>
      </Card>
    );
  }

  const identity = action ? `${action.candidate.model}:${action.candidate.version}` : '';
  const stores = evaluation.compatibility?.supported_stores ?? [];
  const deployedLink =
    run.deployed_model && run.deployed_version
      ? `/forecast?${new URLSearchParams({
          model: run.deployed_model,
          version: run.deployed_version,
          ...(stores[0] ? { store: stores[0] } : {}),
          dataset: run.dataset_id,
        })}`
      : null;

  const close = () => {
    setAction(null);
    setReason('');
    setTypedIdentity('');
    promote.reset();
    reject.reset();
  };

  const submit = () => {
    if (!action) return;
    if (action.kind === 'approve') {
      promote.mutate(
        {
          action: 'promote',
          version: action.candidate.version,
          model: action.candidate.model,
          reason: reason.trim(),
          expectedIdentity: identity,
        },
        { onSuccess: close },
      );
    } else {
      reject.mutate(
        { model: action.candidate.model, version: action.candidate.version, reason: reason.trim() },
        { onSuccess: close },
      );
    }
  };

  const pendingError = action?.kind === 'approve' ? promote.error : reject.error;
  const blockedChecks = pendingError instanceof ApiError ? checksOf(pendingError.details) : [];

  return (
    <Card>
      <CardHeader>
        <CardTitle className="text-base">7. Results and approval</CardTitle>
        <CardDescription>
          Scored once on the untouched test window {evaluation.split?.test?.join(' to ') ?? ''} (
          {evaluation.test_rows?.toLocaleString() ?? '—'} store-days). Target {evaluation.compatibility?.target}, daily,{' '}
          {evaluation.compatibility?.currency}; {stores.length} stores; {evaluation.compatibility?.feature_count} features.
        </CardDescription>
      </CardHeader>
      <CardContent className="space-y-4">
        {run.deployed_model ? (
          <div className="rounded-md border border-emerald-500/30 bg-emerald-500/10 p-3 text-sm text-emerald-300">
            <p className="font-medium">
              Active model: {LABEL[run.deployed_model] ?? run.deployed_model} v{run.deployed_version} (Production)
            </p>
            {deployedLink ? (
              <Link href={deployedLink} className="mt-1 inline-flex items-center gap-1 font-medium underline underline-offset-4">
                <Rocket className="h-4 w-4" aria-hidden /> Generate forecast with this model
              </Link>
            ) : null}
          </div>
        ) : null}

        <div className="overflow-x-auto">
          <table className="w-full text-sm">
            <thead className="text-left text-xs text-muted-foreground">
              <tr>
                <th className="py-1 pr-3">Candidate</th>
                <th className="py-1 pr-3">WAPE</th>
                <th className="py-1 pr-3">MAE</th>
                <th className="py-1 pr-3">RMSE</th>
                <th className="py-1 pr-3">Bias</th>
                <th className="py-1 pr-3">Quality gate</th>
                <th className="py-1 pr-3">Against current model</th>
                <th className="py-1" />
              </tr>
            </thead>
            <tbody>
              {run.candidates.map((candidate) => {
                const metrics = evaluation.metrics?.[candidate.model] ?? {};
                const gate = evaluation.quality_gate?.[candidate.model];
                const comparison = evaluation.comparison?.[candidate.model];
                const decided = run.decisions.find(
                  (item) => item.model === candidate.model && item.version === candidate.version,
                );
                return (
                  <tr key={`${candidate.model}-${candidate.version}`} className="border-t align-top">
                    <td className="py-2 pr-3 font-medium">
                      {LABEL[candidate.model] ?? candidate.model} v{candidate.version}
                      <div className="text-xs font-normal text-muted-foreground">stage None (candidate)</div>
                    </td>
                    <td className="py-2 pr-3">{fmt(metrics.wape, 2, '%')}</td>
                    <td className="py-2 pr-3">{fmt(metrics.mae, 0)}</td>
                    <td className="py-2 pr-3">{fmt(metrics.rmse, 0)}</td>
                    <td className="py-2 pr-3">{fmt(metrics.bias, 0)}</td>
                    <td className="py-2 pr-3">
                      {gate ? (
                        <details>
                          <summary className="cursor-pointer">
                            {gate.passed ? (
                              <span className="inline-flex items-center gap-1 text-emerald-700">
                                <CheckCircle2 className="h-4 w-4" aria-hidden /> Passed
                              </span>
                            ) : (
                              <span className="inline-flex items-center gap-1 text-destructive">
                                <XCircle className="h-4 w-4" aria-hidden /> Failed
                              </span>
                            )}
                          </summary>
                          <ul className="mt-1 space-y-0.5 text-xs">
                            {gate.checks.map((item) => (
                              <li key={item.check}>
                                {item.passed ? '✓' : '✗'} {item.check}: {item.detail}
                              </li>
                            ))}
                          </ul>
                        </details>
                      ) : (
                        '—'
                      )}
                    </td>
                    <td className="max-w-xs py-2 pr-3 text-xs">
                      {comparison ? (
                        comparison.comparable ? (
                          <span>
                            WAPE {fmt(comparison.candidate_wape, 2, '%')} vs {fmt(comparison.champion_wape, 2, '%')} (v
                            {comparison.champion_version}) on the same {comparison.rows} rows
                          </span>
                        ) : (
                          <span className="text-muted-foreground">Not directly comparable (see below)</span>
                        )
                      ) : (
                        '—'
                      )}
                    </td>
                    <td className="py-2 text-right">
                      {decided ? (
                        <Badge variant={decided.decision === 'approved' ? 'success' : 'destructive'}>
                          {decided.decision}
                        </Badge>
                      ) : run.can_deploy ? (
                        <div className="flex flex-col gap-1 whitespace-nowrap">
                          <Button
                            size="sm"
                            disabled={!candidate.gate_passed}
                            onClick={() => setAction({ kind: 'approve', candidate })}
                          >
                            Approve &amp; deploy
                          </Button>
                          <Button size="sm" variant="outline" onClick={() => setAction({ kind: 'reject', candidate })}>
                            Reject
                          </Button>
                        </div>
                      ) : (
                        <span className="text-xs text-muted-foreground">Awaiting ML Engineer review</span>
                      )}
                    </td>
                  </tr>
                );
              })}
            </tbody>
          </table>
        </div>

        {Array.from(
          new Set(
            Object.values(evaluation.comparison ?? {})
              .filter((item) => !item.comparable)
              .map((item) => item.reason),
          ),
        ).map((reason) => (
          <p key={reason} className="rounded-md border border-amber-500/30 bg-amber-500/5 p-3 text-xs text-amber-300">
            {reason} No improvement is claimed.
          </p>
        ))}

        <div className="rounded-md border p-3 text-sm">
          <p className="font-medium">Current Production models</p>
          <ul className="mt-1 text-xs text-muted-foreground">
            {run.production.length === 0 ? <li>None</li> : null}
            {run.production.map((item) => (
              <li key={item.model}>
                {LABEL[item.model] ?? item.model} v{item.version}
                {item.dataset_version ? ` · trained on ${item.dataset_version}` : ''}
                {typeof item.metrics.wape === 'number' ? ` · own test WAPE ${item.metrics.wape.toFixed(2)}%` : ''}
              </li>
            ))}
          </ul>
          <p className="mt-1 text-xs text-muted-foreground">
            A model&apos;s own test score is measured on its own test window; scores from different windows are
            not compared here.
          </p>
        </div>

        {run.decisions.length > 0 ? (
          <div className="text-sm">
            <p className="font-medium">Decisions</p>
            <ul className="mt-1 space-y-1 text-xs">
              {run.decisions.map((item) => (
                <li key={`${item.model}-${item.version}-${item.decided_at}`}>
                  <Badge variant={item.decision === 'approved' ? 'success' : 'destructive'}>{item.decision}</Badge>{' '}
                  {item.model} v{item.version} by {item.decided_by} ({item.decided_by_role}) on{' '}
                  {new Date(item.decided_at).toLocaleString()}: &ldquo;{item.reason}&rdquo;
                  {item.previous_production_version ? ` · replaced v${item.previous_production_version}` : ''}
                </li>
              ))}
            </ul>
          </div>
        ) : null}

        <div className="flex flex-wrap gap-3 text-sm">
          {run.mlflow_url ? (
            <a href={run.mlflow_url} target="_blank" rel="noreferrer" className="inline-flex items-center gap-1 text-primary hover:underline">
              View report and artifacts in MLflow <ExternalLink className="h-3.5 w-3.5" aria-hidden />
            </a>
          ) : null}
          <Link href="/models" className="text-primary hover:underline">
            Open the Model Registry
          </Link>
        </div>

        <Dialog
          open={action !== null}
          onOpenChange={(open) => (open ? null : close())}
          title={action?.kind === 'approve' ? 'Approve and deploy this candidate?' : 'Reject this candidate?'}
          description={
            action?.kind === 'approve'
              ? 'It becomes the Production version every forecast uses. The current version is archived and can be restored with a rollback.'
              : 'The candidate stays registered but is recorded as rejected.'
          }
          footer={
            <>
              <Button variant="outline" onClick={close}>
                Cancel
              </Button>
              <Button
                variant={action?.kind === 'reject' ? 'destructive' : 'default'}
                onClick={submit}
                disabled={
                  reason.trim().length < 5 ||
                  (action?.kind === 'approve' && typedIdentity.trim() !== identity) ||
                  promote.isPending ||
                  reject.isPending
                }
              >
                {action?.kind === 'approve'
                  ? promote.isPending
                    ? 'Checking and deploying…'
                    : 'Approve & deploy'
                  : 'Reject'}
              </Button>
            </>
          }
        >
          <div className="space-y-3 text-sm">
            {action?.kind === 'approve' ? (
              <div className="space-y-1.5">
                <Label htmlFor="confirm-identity">
                  Type <code className="font-semibold">{identity}</code> to confirm the exact version
                </Label>
                <Input
                  id="confirm-identity"
                  value={typedIdentity}
                  onChange={(event) => setTypedIdentity(event.target.value)}
                  autoComplete="off"
                />
              </div>
            ) : null}
            <div className="space-y-1.5">
              <Label htmlFor="decision-reason">Reason (recorded in the audit log)</Label>
              <Textarea
                id="decision-reason"
                value={reason}
                rows={3}
                maxLength={1000}
                onChange={(event) => setReason(event.target.value)}
              />
            </div>
            {pendingError ? (
              <div role="alert" className="space-y-1 text-destructive">
                <p>{pendingError.message || friendlyMessage(pendingError)}</p>
                {blockedChecks.length > 0 ? (
                  <ul className="text-xs">
                    {blockedChecks.map((item) => (
                      <li key={item.check}>
                        {item.passed ? '✓' : '✗'} {item.check}: {item.detail}
                      </li>
                    ))}
                  </ul>
                ) : null}
              </div>
            ) : null}
          </div>
        </Dialog>
      </CardContent>
    </Card>
  );
}
