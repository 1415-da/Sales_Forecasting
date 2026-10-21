'use client';

import { AlertOctagon, AlertTriangle, Check, Info, Loader2 } from 'lucide-react';
import { useState } from 'react';

import { EmptyState } from '@/components/shared/empty-state';
import { ErrorState } from '@/components/shared/error-state';
import { StatusBadge, type StatusTone } from '@/components/shared/status-badge';
import { Button } from '@/components/ui/button';
import { Card, CardContent, CardDescription, CardHeader, CardTitle } from '@/components/ui/card';
import { Input } from '@/components/ui/input';
import { Label } from '@/components/ui/label';
import { formatRelative } from '@/lib/format';
import { NO_PERMISSION_MESSAGE } from '@/lib/rbac';
import type { AlertList, MonitoringAlert } from '@/lib/types/auth';

function severityTone(severity: string): StatusTone {
  switch (severity) {
    case 'critical':
      return 'danger';
    case 'warning':
      return 'warning';
    default:
      return 'info';
  }
}

function SeverityIcon({ severity }: { readonly severity: string }) {
  const Icon =
    severity === 'critical'
      ? AlertOctagon
      : severity === 'warning'
        ? AlertTriangle
        : Info;
  return <Icon aria-hidden="true" className="mt-0.5 h-4 w-4 shrink-0" />;
}

export interface AlertsPanelProps {
  readonly data: AlertList | undefined;
  readonly isLoading: boolean;
  readonly error: unknown;
  readonly canManage: boolean;
  readonly onAcknowledge: (alertId: string) => void;
  readonly onResolve: (alertId: string, note?: string) => void;
  readonly pendingId?: string | null;
  readonly onRetry?: () => void;
}

/**
 * Open alerts, with the action each one calls for.
 *
 * The recommended action is rendered as prominently as the message: an alert
 * that only states a number leaves the reader to work out what to do, which is
 * how alert panels come to be ignored.
 *
 * Acknowledge and Resolve are distinct on purpose. Acknowledging says "seen",
 * resolving says "dealt with"; collapsing them would lose the difference
 * between a known-ongoing problem and a fixed one.
 */
export function AlertsPanel({
  data,
  isLoading,
  error,
  canManage,
  onAcknowledge,
  onResolve,
  pendingId,
  onRetry,
}: AlertsPanelProps) {
  const [resolving, setResolving] = useState<string | null>(null);
  const [note, setNote] = useState('');

  if (error) {
    return (
      <Card>
        <CardContent className="pt-6">
          <ErrorState error={error} {...(onRetry ? { onRetry } : {})} />
        </CardContent>
      </Card>
    );
  }

  return (
    <Card>
      <CardHeader className="flex flex-row flex-wrap items-start justify-between gap-3">
        <div className="space-y-1">
          <CardTitle>Alerts</CardTitle>
          <CardDescription>
            Thresholds crossed by the latest monitoring run.
          </CardDescription>
        </div>
        <div className="flex gap-2">
          {(data?.critical_count ?? 0) > 0 ? (
            <StatusBadge tone="danger">{data?.critical_count} critical</StatusBadge>
          ) : null}
          {(data?.warning_count ?? 0) > 0 ? (
            <StatusBadge tone="warning">{data?.warning_count} warning</StatusBadge>
          ) : null}
        </div>
      </CardHeader>
      <CardContent className="space-y-3">
        {isLoading ? (
          <p className="text-sm text-muted-foreground">Loading alerts…</p>
        ) : (data?.alerts.length ?? 0) === 0 ? (
          <EmptyState
            icon={<Check aria-hidden="true" className="h-8 w-8" />}
            title="No open alerts"
            description="Nothing has crossed a threshold. Alerts appear here when accuracy degrades, data goes stale, or a feature drifts."
          />
        ) : (
          data?.alerts.map((alert) => (
            <article
              key={alert.id}
              className="space-y-3 rounded-lg border p-4"
              aria-labelledby={`alert-${alert.id}-title`}
            >
              <div className="flex items-start justify-between gap-3">
                <div className="flex min-w-0 items-start gap-2">
                  <SeverityIcon severity={alert.severity} />
                  <div className="min-w-0 space-y-1">
                    <h3
                      id={`alert-${alert.id}-title`}
                      className="text-sm font-medium"
                    >
                      {alert.title}
                    </h3>
                    <p className="text-sm text-muted-foreground">{alert.message}</p>
                  </div>
                </div>
                <div className="flex shrink-0 flex-col items-end gap-1">
                  <StatusBadge tone={severityTone(alert.severity)}>
                    {alert.severity}
                  </StatusBadge>
                  {alert.status === 'acknowledged' ? (
                    <StatusBadge tone="neutral">Acknowledged</StatusBadge>
                  ) : null}
                </div>
              </div>

              {alert.recommended_action ? (
                <p className="rounded-md bg-muted/50 p-3 text-xs">
                  <span className="font-medium">What to do: </span>
                  {alert.recommended_action}
                </p>
              ) : null}

              <div className="flex flex-wrap items-center gap-x-4 gap-y-1 text-xs text-muted-foreground">
                <span>First seen {formatRelative(alert.created_at)}</span>
                {alert.occurrence_count > 1 ? (
                  /* An ongoing problem on its tenth day is more urgent than
                     the same problem on its first. */
                  <span>Seen {alert.occurrence_count}×</span>
                ) : null}
                {alert.model_version ? <span>v{alert.model_version}</span> : null}
                {alert.store_id ? <span>{alert.store_id}</span> : null}
              </div>

              {canManage ? (
                resolving === alert.id ? (
                  <div className="space-y-2 border-t pt-3">
                    <Label htmlFor={`note-${alert.id}`} className="text-xs">
                      What was done? (optional)
                    </Label>
                    <Input
                      id={`note-${alert.id}`}
                      value={note}
                      onChange={(event) => setNote(event.target.value)}
                      placeholder="Retrained on recent data"
                      className="h-8"
                    />
                    <div className="flex gap-2">
                      <Button
                        size="sm"
                        disabled={pendingId === alert.id}
                        onClick={() => {
                          onResolve(alert.id, note.trim() || undefined);
                          setResolving(null);
                          setNote('');
                        }}
                      >
                        {pendingId === alert.id ? (
                          <Loader2 aria-hidden="true" className="h-3.5 w-3.5 animate-spin" />
                        ) : null}
                        Confirm resolve
                      </Button>
                      <Button
                        size="sm"
                        variant="ghost"
                        onClick={() => {
                          setResolving(null);
                          setNote('');
                        }}
                      >
                        Cancel
                      </Button>
                    </div>
                  </div>
                ) : (
                  <div className="flex flex-wrap gap-2 border-t pt-3">
                    {alert.status === 'open' ? (
                      <Button
                        size="sm"
                        variant="outline"
                        disabled={pendingId === alert.id}
                        onClick={() => onAcknowledge(alert.id)}
                      >
                        {pendingId === alert.id ? (
                          <Loader2 aria-hidden="true" className="h-3.5 w-3.5 animate-spin" />
                        ) : null}
                        Acknowledge
                      </Button>
                    ) : null}
                    <Button
                      size="sm"
                      variant="ghost"
                      onClick={() => setResolving(alert.id)}
                    >
                      Resolve
                    </Button>
                  </div>
                )
              ) : (
                <p className="border-t pt-3 text-xs text-muted-foreground">
                  {NO_PERMISSION_MESSAGE} Acknowledging and resolving alerts
                  requires an ML Engineer or Administrator role.
                </p>
              )}
            </article>
          ))
        )}
      </CardContent>
    </Card>
  );
}
