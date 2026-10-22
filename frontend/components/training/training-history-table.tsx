'use client';

import { Badge } from '@/components/ui/badge';
import { Card, CardContent, CardDescription, CardHeader, CardTitle } from '@/components/ui/card';
import type { TrainingRun } from '@/lib/types/training';
import { cn } from '@/lib/utils';

const DEPLOYMENT: Record<string, string> = {
  not_ready: 'Not ready',
  awaiting_review: 'Awaiting review',
  deployed: 'Deployed',
  rejected: 'Rejected',
  blocked: 'Blocked by quality gate',
};

/** Step 5: every request, newest first. */
export function TrainingHistoryTable({
  runs,
  selectedId,
  onSelect,
}: {
  runs: TrainingRun[];
  selectedId: string | null;
  onSelect: (id: string) => void;
}) {
  return (
    <Card>
      <CardHeader>
        <CardTitle className="text-base">5. Training history</CardTitle>
        <CardDescription>Select a request to see its progress and results.</CardDescription>
      </CardHeader>
      <CardContent>
        {runs.length === 0 ? (
          <p className="text-sm text-muted-foreground">No training requests yet.</p>
        ) : (
          <div className="overflow-x-auto">
            <table className="w-full text-sm">
              <thead className="text-left text-xs text-muted-foreground">
                <tr>
                  <th className="py-1 pr-3">Dataset</th>
                  <th className="py-1 pr-3">Requested by</th>
                  <th className="py-1 pr-3">Status</th>
                  <th className="py-1 pr-3">Candidates</th>
                  <th className="py-1 pr-3">Ensemble WAPE</th>
                  <th className="py-1 pr-3">Deployment</th>
                  <th className="py-1">Created</th>
                </tr>
              </thead>
              <tbody>
                {runs.map((run) => (
                  <tr
                    key={run.id}
                    className={cn('cursor-pointer border-t hover:bg-muted/50', run.id === selectedId && 'bg-muted')}
                    onClick={() => onSelect(run.id)}
                  >
                    <td className="py-1.5 pr-3">
                      <button type="button" className="text-left font-medium underline-offset-4 hover:underline">
                        {run.dataset_name} v{run.dataset_version}
                      </button>
                    </td>
                    <td className="py-1.5 pr-3">
                      {run.created_by ?? '—'}
                      {run.created_by_role ? <span className="text-xs text-muted-foreground"> ({run.created_by_role})</span> : null}
                    </td>
                    <td className="py-1.5 pr-3">
                      <Badge variant={run.status === 'succeeded' ? 'success' : run.status === 'failed' ? 'destructive' : 'warning'}>
                        {run.status}
                      </Badge>
                    </td>
                    <td className="py-1.5 pr-3 text-xs">
                      {run.candidates.length > 0
                        ? run.candidates.map((item) => `${item.model} v${item.version}`).join(', ')
                        : '—'}
                    </td>
                    <td className="py-1.5 pr-3">
                      {run.headline_wape != null ? `${run.headline_wape.toFixed(2)}%` : '—'}
                    </td>
                    <td className="py-1.5 pr-3">
                      {DEPLOYMENT[run.deployment_state] ?? run.deployment_state}
                      {run.deployed_model ? ` (${run.deployed_model} v${run.deployed_version})` : ''}
                    </td>
                    <td className="py-1.5 text-xs">{new Date(run.created_at).toLocaleString()}</td>
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
