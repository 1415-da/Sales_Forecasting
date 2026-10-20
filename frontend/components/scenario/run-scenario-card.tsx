'use client';

import { AlertCircle, Layers, Loader2, Play, RefreshCw } from 'lucide-react';

import { Alert, AlertDescription } from '@/components/ui/alert';
import { Button } from '@/components/ui/button';
import { Card, CardContent } from '@/components/ui/card';
import type { FormErrors, ResultStatus } from '@/hooks/use-scenario-form';
import type { ErrorPresentation } from '@/lib/utils/errors';

export interface RunScenarioCardProps {
  onRun: () => void;
  isRunning: boolean;
  /** 'none' shows "Run scenario"; 'stale' shows "Update scenario". */
  resultStatus: ResultStatus;
  canRun: boolean;
  errors?: FormErrors;
  /** Server-side failure, already mapped to friendly text. */
  failure?: ErrorPresentation | null;
  onRetry?: () => void;
  /** True while an automatic retry is in flight. */
  isRetrying?: boolean;
  /** Set when a baseline forecast is being reused rather than regenerated. */
  isReusingBaseline?: boolean;
  /** Blocks further runs once the scenario has been rolled back. */
  isRolledBack?: boolean;
  className?: string;
}

/** The four button states, derived rather than passed in. */
function buttonLabel(isRunning: boolean, resultStatus: ResultStatus): string {
  if (isRunning) return 'Running…';
  return resultStatus === 'none' ? 'Run scenario' : 'Update scenario';
}

export function RunScenarioCard({
  onRun,
  isRunning,
  resultStatus,
  canRun,
  errors = {},
  failure = null,
  onRetry,
  isRetrying = false,
  isReusingBaseline = false,
  isRolledBack = false,
  className,
}: RunScenarioCardProps) {
  const messages = Object.entries(errors).filter(([, message]) => Boolean(message));
  const disabled = isRunning || !canRun || isRolledBack;
  const label = isRolledBack ? 'Scenario rolled back' : buttonLabel(isRunning, resultStatus);

  return (
    <Card className={className}>
      <CardContent className="space-y-3 p-4">
        {messages.length > 0 ? (
          <Alert variant="warning" role="alert">
            <AlertCircle aria-hidden="true" className="h-4 w-4" />
            <AlertDescription>
              <ul className="space-y-0.5 text-xs">
                {messages.map(([field, message]) => (
                  <li key={field}>{message}</li>
                ))}
              </ul>
            </AlertDescription>
          </Alert>
        ) : null}

        {failure ? (
          <Alert
            variant={failure.severity === 'warning' ? 'warning' : 'destructive'}
            role="alert"
          >
            <AlertCircle aria-hidden="true" className="h-4 w-4" />
            <AlertDescription className="space-y-2 text-xs">
              <p>{failure.message}</p>
              {failure.requestId ? (
                <p className="text-muted-foreground">Reference: {failure.requestId}</p>
              ) : null}
              {failure.retryable && onRetry ? (
                <Button variant="outline" size="sm" onClick={onRetry} disabled={isRunning}>
                  <RefreshCw aria-hidden="true" className="h-3.5 w-3.5" />
                  Retry
                </Button>
              ) : null}
            </AlertDescription>
          </Alert>
        ) : null}

        {isReusingBaseline && !isRunning ? (
          <div className="flex items-center gap-1.5 text-xs text-muted-foreground">
            <Layers aria-hidden="true" className="h-3.5 w-3.5" />
            Reusing baseline forecast
          </div>
        ) : null}

        {isRetrying ? (
          <p role="status" className="text-xs text-muted-foreground">
            Connection problem — retrying automatically…
          </p>
        ) : null}

        {isRolledBack ? (
          <p role="status" className="text-xs text-muted-foreground">
            This scenario was rolled back. Change an assumption to run a new one.
          </p>
        ) : null}

        {resultStatus === 'stale' && !isRunning && !isRolledBack ? (
          <p className="text-xs text-muted-foreground">
            Assumptions changed since the last run. Update to refresh the comparison.
          </p>
        ) : null}

        <Button
          className="w-full"
          size="lg"
          onClick={onRun}
          disabled={disabled}
          /* `aria-disabled` keeps the control focusable so a screen-reader user
             can discover *why* it is unavailable from the error list above. */
          aria-disabled={disabled}
          aria-busy={isRunning}
        >
          {isRunning ? (
            <Loader2 aria-hidden="true" className="h-4 w-4 animate-spin" />
          ) : (
            <Play aria-hidden="true" className="h-4 w-4" />
          )}
          {label}
        </Button>

        <p aria-live="polite" className="sr-only">
          {isRunning ? 'Running scenario' : `Ready: ${label}`}
        </p>
      </CardContent>
    </Card>
  );
}
