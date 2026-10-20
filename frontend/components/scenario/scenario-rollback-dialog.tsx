'use client';

import { useEffect, useState } from 'react';
import { Loader2, Undo2 } from 'lucide-react';

import { Button } from '@/components/ui/button';
import { Dialog } from '@/components/ui/dialog';
import { Label } from '@/components/ui/label';
import { Textarea } from '@/components/ui/textarea';
import { MIN_ROLLBACK_REASON_LENGTH, isValidRollbackReason } from '@/hooks/use-scenario-rollback';

export interface ScenarioRollbackDialogProps {
  open: boolean;
  onOpenChange: (open: boolean) => void;
  scenarioId: string;
  onConfirm: (reason: string) => void;
  isPending?: boolean;
  errorMessage?: string | null;
}

/**
 * Confirmation for withdrawing a scenario.
 *
 * A reason is required because it is written to the audit trail: a rollback
 * with no stated cause is a gap in the record, not a convenience.
 */
export function ScenarioRollbackDialog({
  open,
  onOpenChange,
  scenarioId,
  onConfirm,
  isPending = false,
  errorMessage = null,
}: ScenarioRollbackDialogProps) {
  const [reason, setReason] = useState('');
  const [touched, setTouched] = useState(false);

  // Clear the field between openings, so a previous attempt's text is never
  // silently resubmitted.
  useEffect(() => {
    if (open) {
      setReason('');
      setTouched(false);
    }
  }, [open]);

  const valid = isValidRollbackReason(reason);
  const showError = touched && !valid;

  return (
    <Dialog
      open={open}
      onOpenChange={onOpenChange}
      title="Roll back this scenario?"
      description="The comparison is marked as withdrawn. Its audit history and the baseline forecast are preserved."
      footer={
        <>
          <Button variant="outline" onClick={() => onOpenChange(false)} disabled={isPending}>
            Cancel
          </Button>
          <Button
            variant="destructive"
            onClick={() => onConfirm(reason.trim())}
            disabled={!valid || isPending}
            aria-disabled={!valid || isPending}
          >
            {isPending ? (
              <Loader2 aria-hidden="true" className="h-4 w-4 animate-spin" />
            ) : (
              <Undo2 aria-hidden="true" className="h-4 w-4" />
            )}
            {isPending ? 'Rolling back…' : 'Roll back'}
          </Button>
        </>
      }
    >
      <div className="space-y-1.5">
        <Label htmlFor="rollback-reason">Reason</Label>
        <Textarea
          id="rollback-reason"
          value={reason}
          onChange={(event) => setReason(event.target.value)}
          onBlur={() => setTouched(true)}
          placeholder="Why is this scenario being withdrawn?"
          aria-invalid={showError}
          aria-describedby={showError ? 'rollback-reason-error' : 'rollback-reason-hint'}
          disabled={isPending}
        />
        <p id="rollback-reason-hint" className="text-xs text-muted-foreground">
          Recorded in the audit trail for scenario{' '}
          <span className="font-mono">{scenarioId}</span>.
        </p>
        {showError ? (
          <p id="rollback-reason-error" role="alert" className="text-xs text-destructive">
            Enter at least {MIN_ROLLBACK_REASON_LENGTH} characters.
          </p>
        ) : null}
        {errorMessage ? (
          <p role="alert" className="text-xs text-destructive">
            {errorMessage}
          </p>
        ) : null}
      </div>
    </Dialog>
  );
}
