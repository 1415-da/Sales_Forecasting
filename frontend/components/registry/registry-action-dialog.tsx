'use client';

import { AlertTriangle, Loader2 } from 'lucide-react';
import { useState } from 'react';

import { ErrorState } from '@/components/shared/error-state';
import { StatusBadge } from '@/components/shared/status-badge';
import { Button } from '@/components/ui/button';
import { Input } from '@/components/ui/input';
import { Label } from '@/components/ui/label';
import { formatMetric } from '@/lib/format';
import type { ModelInfo } from '@/lib/types';

export type RegistryActionKind = 'promote' | 'archive' | 'rollback';

export interface RegistryActionDialogProps {
  readonly action: RegistryActionKind;
  readonly target: ModelInfo;
  readonly currentProduction: ModelInfo | undefined;
  readonly isPending: boolean;
  readonly error: unknown;
  readonly onConfirm: (reason: string) => void;
  readonly onCancel: () => void;
}

const COPY: Record<
  RegistryActionKind,
  { title: string; verb: string; warning: string }
> = {
  promote: {
    title: 'Promote to Production',
    verb: 'Promote',
    warning:
      'Every forecast the platform produces will come from this version from now on.',
  },
  archive: {
    title: 'Archive this version',
    verb: 'Archive',
    warning:
      'An archived version is retained but will no longer be offered for forecasts.',
  },
  rollback: {
    title: 'Roll Production back',
    verb: 'Roll back',
    warning:
      'Production will return to this earlier version, and the current one will be archived.',
  },
};

/**
 * Confirmation for a registry mutation.
 *
 * Shows the before/after and requires a typed confirmation phrase for a
 * promotion or rollback. The friction is deliberate: these change what serves
 * every forecast, and an accidental click should not be able to do that.
 *
 * The reason field is optional but prominent -- it lands in the audit log, and
 * "why was this promoted" is the question asked weeks later.
 */
export function RegistryActionDialog({
  action,
  target,
  currentProduction,
  isPending,
  error,
  onConfirm,
  onCancel,
}: RegistryActionDialogProps) {
  const [reason, setReason] = useState('');
  const [typed, setTyped] = useState('');

  const copy = COPY[action];
  // Archiving is reversible by promoting again; changing what serves is not
  // something to do by accident.
  const needsPhrase = action !== 'archive';
  const phrase = target.version;
  const confirmed = !needsPhrase || typed.trim() === phrase;

  return (
    <div
      role="dialog"
      aria-modal="true"
      aria-labelledby="registry-action-title"
      className="fixed inset-0 z-50 flex items-center justify-center bg-background/80 p-4 backdrop-blur-sm"
    >
      <div className="w-full max-w-lg space-y-4 rounded-lg border bg-card p-6 shadow-xl">
        <div className="space-y-1">
          <h2 id="registry-action-title" className="text-base font-semibold">
            {copy.title}
          </h2>
          <p className="text-sm text-muted-foreground">
            {target.label} version {target.version}
          </p>
        </div>

        <div className="flex items-start gap-3 rounded-md border border-amber-500/40 bg-amber-500/5 p-3">
          <AlertTriangle
            aria-hidden="true"
            className="mt-0.5 h-4 w-4 shrink-0 text-amber-500"
          />
          <p className="text-sm">{copy.warning}</p>
        </div>

        {/* Before and after, so the consequence is visible rather than implied. */}
        <dl className="space-y-2 rounded-md border p-3 text-xs">
          <div className="flex items-center justify-between gap-4">
            <dt className="text-muted-foreground">Currently serving</dt>
            <dd>
              {currentProduction ? (
                <StatusBadge tone="success">
                  v{currentProduction.version} · RMSE{' '}
                  {formatMetric(currentProduction.metrics.rmse)}
                </StatusBadge>
              ) : (
                <StatusBadge tone="neutral">none</StatusBadge>
              )}
            </dd>
          </div>
          <div className="flex items-center justify-between gap-4">
            <dt className="text-muted-foreground">After this change</dt>
            <dd>
              <StatusBadge tone={action === 'archive' ? 'neutral' : 'info'}>
                {action === 'archive'
                  ? `v${target.version} archived`
                  : `v${target.version} · RMSE ${formatMetric(target.metrics.rmse)}`}
              </StatusBadge>
            </dd>
          </div>
        </dl>

        <div className="space-y-2">
          <Label htmlFor="reason">Reason (recorded in the audit log)</Label>
          <Input
            id="reason"
            value={reason}
            onChange={(event) => setReason(event.target.value)}
            placeholder="Lower RMSE on the last four weeks of actuals"
          />
        </div>

        {needsPhrase ? (
          <div className="space-y-2">
            <Label htmlFor="confirm-phrase">
              Type <span className="font-mono">{phrase}</span> to confirm
            </Label>
            <Input
              id="confirm-phrase"
              value={typed}
              onChange={(event) => setTyped(event.target.value)}
              autoComplete="off"
            />
          </div>
        ) : null}

        {error ? <ErrorState error={error} /> : null}

        <div className="flex justify-end gap-2 pt-2">
          <Button variant="ghost" onClick={onCancel} disabled={isPending}>
            Cancel
          </Button>
          <Button
            variant={action === 'archive' ? 'default' : 'destructive'}
            disabled={!confirmed || isPending}
            onClick={() => onConfirm(reason.trim())}
          >
            {isPending ? (
              <>
                <Loader2 aria-hidden="true" className="h-4 w-4 animate-spin" />
                Applying…
              </>
            ) : (
              copy.verb
            )}
          </Button>
        </div>
      </div>
    </div>
  );
}
