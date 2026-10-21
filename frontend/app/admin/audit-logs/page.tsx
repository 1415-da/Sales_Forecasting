'use client';

import { useState } from 'react';

import { RequireAuth } from '@/components/auth/require-auth';
import { EmptyState } from '@/components/shared/empty-state';
import { ErrorState } from '@/components/shared/error-state';
import { TableSkeleton } from '@/components/shared/loading-skeleton';
import { StatusBadge, type StatusTone } from '@/components/shared/status-badge';
import { Button } from '@/components/ui/button';
import { Card, CardContent } from '@/components/ui/card';
import { Label } from '@/components/ui/label';
import { Select } from '@/components/ui/select';
import { useAuditLogs } from '@/hooks/use-admin';
import { formatTimestamp } from '@/lib/format';
import { PERMISSIONS } from '@/lib/rbac';

const PAGE_SIZE = 50;

/** Tone for an audit outcome. A denial is visually distinct from a failure. */
function outcomeTone(outcome: string): StatusTone {
  switch (outcome) {
    case 'success':
      return 'success';
    case 'denied':
      return 'warning';
    case 'failure':
      return 'danger';
    default:
      return 'neutral';
  }
}

/** `model.promoted` -> `Model promoted`. */
function actionLabel(action: string): string {
  const [, verb] = action.split('.');
  const text = (verb ?? action).replace(/_/g, ' ');
  return text.charAt(0).toUpperCase() + text.slice(1);
}

/**
 * The audit trail.
 *
 * What an ADMIN sees and what an ML_ENGINEER sees differ, and the page says
 * which it is showing rather than leaving someone to wonder why logins are
 * missing. The scoping is applied server-side in SQL; this banner only
 * reports it.
 */
export default function AuditLogsPage() {
  return (
    <RequireAuth permission={PERMISSIONS.viewAuditLogs}>
      <AuditLog />
    </RequireAuth>
  );
}

function AuditLog() {
  const [action, setAction] = useState('');
  const [outcome, setOutcome] = useState('');
  const [page, setPage] = useState(0);

  const logs = useAuditLogs({
    ...(action ? { actions: [action] } : {}),
    ...(outcome ? { outcome } : {}),
    limit: PAGE_SIZE,
    offset: page * PAGE_SIZE,
  });

  const totalPages = Math.max(1, Math.ceil((logs.data?.total ?? 0) / PAGE_SIZE));
  const actionOptions = Object.entries(logs.data?.available_actions ?? {}).sort(
    ([left], [right]) => left.localeCompare(right),
  );

  return (
    <div className="space-y-6">
      <div className="space-y-1">
        <h1 className="text-xl font-semibold tracking-tight">Audit log</h1>
        <p className="text-sm text-muted-foreground">
          Who did what, and when. Append-only.
        </p>
      </div>

      {logs.data && !logs.data.unrestricted ? (
        <p className="rounded-lg border border-dashed p-3 text-sm text-muted-foreground">
          You are seeing model, scenario, forecast, and alert activity. Account
          activity such as sign-ins and role changes is visible to
          administrators only.
        </p>
      ) : null}

      <Card>
        <CardContent className="grid gap-4 p-4 sm:grid-cols-2">
          <div className="space-y-1.5">
            <Label htmlFor="filter-action">Action</Label>
            <Select
              id="filter-action"
              value={action}
              onChange={(event) => {
                setAction(event.target.value);
                setPage(0);
              }}
            >
              <option value="">All actions</option>
              {/* Only actions that actually have records, with their counts:
                  offering every action name would mostly produce empty pages. */}
              {actionOptions.map(([name, count]) => (
                <option key={name} value={name}>
                  {actionLabel(name)} ({count})
                </option>
              ))}
            </Select>
          </div>
          <div className="space-y-1.5">
            <Label htmlFor="filter-outcome">Outcome</Label>
            <Select
              id="filter-outcome"
              value={outcome}
              onChange={(event) => {
                setOutcome(event.target.value);
                setPage(0);
              }}
            >
              <option value="">All outcomes</option>
              <option value="success">Success</option>
              <option value="denied">Denied</option>
              <option value="failure">Failure</option>
            </Select>
          </div>
        </CardContent>
      </Card>

      {logs.isError ? (
        <ErrorState error={logs.error} onRetry={() => void logs.refetch()} />
      ) : logs.isLoading ? (
        <TableSkeleton rows={8} />
      ) : (logs.data?.entries.length ?? 0) === 0 ? (
        <EmptyState
          title="No audit records"
          description="Records appear as people sign in, generate forecasts, run scenarios, and change models."
        />
      ) : (
        <Card>
          <CardContent className="p-0">
            <div className="overflow-x-auto">
              <table className="w-full text-sm">
                <caption className="sr-only">Audit records</caption>
                <thead className="sticky top-0 bg-muted/90 backdrop-blur">
                  <tr>
                    <th scope="col" className="px-4 py-2 text-left font-medium">
                      When
                    </th>
                    <th scope="col" className="px-4 py-2 text-left font-medium">
                      Action
                    </th>
                    <th scope="col" className="px-4 py-2 text-left font-medium">
                      Who
                    </th>
                    <th scope="col" className="px-4 py-2 text-left font-medium">
                      Resource
                    </th>
                    <th scope="col" className="px-4 py-2 text-left font-medium">
                      Outcome
                    </th>
                    <th scope="col" className="px-4 py-2 text-left font-medium">
                      Detail
                    </th>
                  </tr>
                </thead>
                <tbody>
                  {logs.data?.entries.map((entry) => (
                    <tr key={entry.id} className="border-t align-top hover:bg-muted/40">
                      <td className="whitespace-nowrap px-4 py-2 text-xs text-muted-foreground">
                        {formatTimestamp(entry.created_at)}
                      </td>
                      <td className="px-4 py-2">
                        <span className="font-medium">{actionLabel(entry.action)}</span>
                        <span className="block font-mono text-[11px] text-muted-foreground">
                          {entry.action}
                        </span>
                      </td>
                      <td className="px-4 py-2 text-xs">
                        {entry.user_email ?? (
                          <span className="text-muted-foreground">unauthenticated</span>
                        )}
                        {entry.user_role ? (
                          <span className="block text-muted-foreground">
                            {entry.user_role}
                          </span>
                        ) : null}
                      </td>
                      <td className="px-4 py-2 text-xs">
                        <span className="text-muted-foreground">
                          {entry.resource_type}
                        </span>
                        {entry.resource_id ? (
                          <span className="block break-all font-mono text-[11px]">
                            {entry.resource_id}
                          </span>
                        ) : null}
                      </td>
                      <td className="px-4 py-2">
                        <StatusBadge tone={outcomeTone(entry.outcome)}>
                          {entry.outcome}
                        </StatusBadge>
                      </td>
                      <td className="max-w-xs px-4 py-2">
                        {Object.keys(entry.metadata).length === 0 ? (
                          <span className="text-xs text-muted-foreground">—</span>
                        ) : (
                          <dl className="space-y-0.5 text-[11px]">
                            {Object.entries(entry.metadata)
                              .slice(0, 6)
                              .map(([key, value]) => (
                                <div key={key} className="flex gap-2">
                                  <dt className="shrink-0 text-muted-foreground">
                                    {key}
                                  </dt>
                                  <dd className="truncate font-mono">
                                    {typeof value === 'object'
                                      ? JSON.stringify(value)
                                      : String(value)}
                                  </dd>
                                </div>
                              ))}
                          </dl>
                        )}
                        {entry.request_id ? (
                          <p className="pt-1 font-mono text-[10px] text-muted-foreground">
                            {entry.request_id}
                          </p>
                        ) : null}
                      </td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
          </CardContent>
        </Card>
      )}

      {totalPages > 1 ? (
        <div className="flex items-center justify-between">
          <p className="text-xs text-muted-foreground">
            Showing {page * PAGE_SIZE + 1}–
            {Math.min((page + 1) * PAGE_SIZE, logs.data?.total ?? 0)} of{' '}
            {logs.data?.total ?? 0}
          </p>
          <div className="flex gap-2">
            <Button
              size="sm"
              variant="outline"
              disabled={page === 0}
              onClick={() => setPage((current) => Math.max(0, current - 1))}
            >
              Previous
            </Button>
            <Button
              size="sm"
              variant="outline"
              disabled={page + 1 >= totalPages}
              onClick={() => setPage((current) => current + 1)}
            >
              Next
            </Button>
          </div>
        </div>
      ) : null}
    </div>
  );
}
