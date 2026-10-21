'use client';

import { AlertTriangle, Copy, RefreshCw } from 'lucide-react';
import { useState } from 'react';

import { Button } from '@/components/ui/button';
import { ApiError, friendlyMessage } from '@/lib/api';
import { cn } from '@/lib/utils';

export interface ErrorStateProps {
  readonly error: unknown;
  readonly onRetry?: () => void;
  readonly className?: string;
}

/**
 * A failure, explained.
 *
 * Three things, in order of what the user needs: a sentence they can act on,
 * the field at fault when there is one, and the request id. The id is
 * copyable because quoting it is the fastest route to a server-side
 * explanation, and expecting someone to transcribe a 32-character hex string
 * by hand is how that fails in practice.
 */
export function ErrorState({ error, onRetry, className }: ErrorStateProps) {
  const [copied, setCopied] = useState(false);
  const apiError = error instanceof ApiError ? error : undefined;
  const requestId = apiError?.requestId;

  const copyRequestId = async () => {
    if (!requestId) return;
    try {
      await navigator.clipboard.writeText(requestId);
      setCopied(true);
      setTimeout(() => setCopied(false), 2000);
    } catch {
      // Clipboard access can be denied; the id is on screen either way.
      setCopied(false);
    }
  };

  const showRetry = onRetry && (apiError?.isRetryable ?? true);

  return (
    <div
      role="alert"
      className={cn(
        'space-y-3 rounded-lg border border-destructive/40 bg-destructive/5 p-5',
        className,
      )}
    >
      <div className="flex items-start gap-3">
        <AlertTriangle aria-hidden="true" className="mt-0.5 h-5 w-5 shrink-0 text-destructive" />
        <div className="min-w-0 space-y-1">
          <p className="text-sm font-medium">{friendlyMessage(error)}</p>
          {apiError?.field ? (
            <p className="text-xs text-muted-foreground">
              Field: <span className="font-mono">{apiError.field}</span>
            </p>
          ) : null}
          {apiError?.code ? (
            <p className="text-xs text-muted-foreground">
              Code: <span className="font-mono">{apiError.code}</span>
            </p>
          ) : null}
        </div>
      </div>

      <div className="flex flex-wrap items-center gap-2">
        {showRetry ? (
          <Button size="sm" variant="outline" onClick={onRetry}>
            <RefreshCw aria-hidden="true" className="h-3.5 w-3.5" />
            Try again
          </Button>
        ) : null}
        {requestId ? (
          <Button size="sm" variant="ghost" onClick={copyRequestId}>
            <Copy aria-hidden="true" className="h-3.5 w-3.5" />
            {copied ? 'Copied' : `Copy request ID`}
          </Button>
        ) : null}
      </div>

      {requestId ? (
        <p className="break-all font-mono text-xs text-muted-foreground">{requestId}</p>
      ) : null}
    </div>
  );
}
