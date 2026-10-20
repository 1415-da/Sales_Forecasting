'use client';

import { AlertTriangle, RefreshCw } from 'lucide-react';

import { Button } from '@/components/ui/button';
import { Card, CardContent } from '@/components/ui/card';
import type { ErrorPresentation } from '@/lib/utils/errors';

export interface ScenarioErrorStateProps {
  /**
   * A presentation descriptor, not a raw error.
   *
   * `toErrorPresentation` has already mapped the API's error code to approved
   * copy, so nothing server-authored, and no stack trace, can reach the screen
   * from here.
   */
  presentation: ErrorPresentation;
  onRetry?: () => void;
  isRetrying?: boolean;
}

export function ScenarioErrorState({
  presentation,
  onRetry,
  isRetrying = false,
}: ScenarioErrorStateProps) {
  const showRetry = presentation.retryable && Boolean(onRetry);

  return (
    <Card className="border-destructive/40">
      <CardContent className="flex min-h-[320px] flex-col items-center justify-center gap-4 p-8 text-center">
        <div className="rounded-full bg-destructive/10 p-3">
          <AlertTriangle aria-hidden="true" className="h-6 w-6 text-destructive" />
        </div>

        <div className="space-y-1.5" role="alert">
          <h3 className="text-base font-semibold">{presentation.title}</h3>
          <p className="mx-auto max-w-sm text-sm text-muted-foreground">
            {presentation.message}
          </p>
        </div>

        <dl className="space-y-0.5 text-xs text-muted-foreground">
          <div className="flex items-center justify-center gap-1.5">
            <dt>Error code</dt>
            <dd className="font-mono font-medium">{presentation.code}</dd>
          </div>
          {presentation.requestId ? (
            <div className="flex items-center justify-center gap-1.5">
              <dt>Request ID</dt>
              {/* Shown so a user can quote it to support, and so it can be
                  correlated with the server log line for the same request. */}
              <dd className="font-mono font-medium">{presentation.requestId}</dd>
            </div>
          ) : null}
        </dl>

        {showRetry ? (
          <Button variant="outline" onClick={onRetry} disabled={isRetrying}>
            <RefreshCw
              aria-hidden="true"
              className={`h-4 w-4 ${isRetrying ? 'animate-spin' : ''}`}
            />
            {isRetrying ? 'Retrying…' : 'Retry'}
          </Button>
        ) : (
          <p className="text-xs text-muted-foreground">
            Adjust your inputs and run the scenario again.
          </p>
        )}
      </CardContent>
    </Card>
  );
}
