'use client';

import { useCallback, useState } from 'react';
import { AlertTriangle, Check, Copy, RefreshCw } from 'lucide-react';

import { Button } from '@/components/ui/button';
import { Card, CardContent } from '@/components/ui/card';
import { humanizeIdentifier } from '@/lib/utils/format';
import type { ErrorPresentation } from '@/lib/utils/errors';

/** Read the missing-feature list the API attaches to an unsupported scenario. */
function missingFeatures(presentation: ErrorPresentation): string[] {
  const direct = presentation.details.missing_features;
  if (Array.isArray(direct)) return direct.map(String);

  // The override engine reports them nested under `unsupported` instead.
  const nested = presentation.details.unsupported;
  if (Array.isArray(nested)) {
    return nested
      .map((item) =>
        item && typeof item === 'object' && 'feature_name' in item
          ? String((item as { feature_name: unknown }).feature_name)
          : null,
      )
      .filter((name): name is string => Boolean(name));
  }
  return [];
}

/** Scenario types the selected model does support, when the API listed them. */
function supportedTypes(presentation: ErrorPresentation): string[] {
  const types = presentation.details.supported_scenario_types;
  return Array.isArray(types) ? types.map(String) : [];
}

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
  /** Offered when the failure is the model lacking required features. */
  onChangeModel?: () => void;
}

export function ScenarioErrorState({
  presentation,
  onRetry,
  isRetrying = false,
  onChangeModel,
}: ScenarioErrorStateProps) {
  const [copied, setCopied] = useState(false);
  const showRetry = presentation.retryable && Boolean(onRetry);
  const missing = missingFeatures(presentation);
  const supported = supportedTypes(presentation);
  const isCapabilityFailure =
    presentation.code === 'SCENARIO_NOT_SUPPORTED' ||
    presentation.code === 'FEATURE_NOT_AVAILABLE' ||
    presentation.code === 'FEATURE_SCHEMA_MISSING';

  const copyRequestId = useCallback(async () => {
    if (!presentation.requestId) return;
    try {
      await navigator.clipboard.writeText(presentation.requestId);
      setCopied(true);
      setTimeout(() => setCopied(false), 2000);
    } catch {
      // Clipboard access can be denied; the id stays selectable as text.
      setCopied(false);
    }
  }, [presentation.requestId]);

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

        {isCapabilityFailure && missing.length > 0 ? (
          <div className="w-full max-w-sm space-y-1.5 rounded-md border bg-muted/40 p-3 text-left">
            <p className="text-xs font-medium">Features this model is missing</p>
            <ul className="space-y-0.5 text-xs text-muted-foreground">
              {missing.map((feature) => (
                <li key={feature} className="font-mono">
                  {feature}
                </li>
              ))}
            </ul>
            {supported.length > 0 ? (
              <p className="text-xs text-muted-foreground">
                This model supports: {supported.map(humanizeIdentifier).join(', ')}.
              </p>
            ) : null}
            <p className="text-xs text-muted-foreground">
              Select a different model version, or choose a scenario type this model
              can support.
            </p>
          </div>
        ) : null}

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
              <button
                type="button"
                onClick={copyRequestId}
                aria-label="Copy request ID"
                className="rounded-sm p-0.5 opacity-70 transition-opacity hover:opacity-100 focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring"
              >
                {copied ? (
                  <Check aria-hidden="true" className="h-3.5 w-3.5" />
                ) : (
                  <Copy aria-hidden="true" className="h-3.5 w-3.5" />
                )}
              </button>
              <span aria-live="polite" className="sr-only">
                {copied ? 'Request ID copied' : ''}
              </span>
            </div>
          ) : null}
        </dl>

        <div className="flex flex-wrap items-center justify-center gap-2">
          {showRetry ? (
            <Button variant="outline" onClick={onRetry} disabled={isRetrying}>
              <RefreshCw
                aria-hidden="true"
                className={`h-4 w-4 ${isRetrying ? 'animate-spin' : ''}`}
              />
              {isRetrying ? 'Retrying…' : 'Retry'}
            </Button>
          ) : null}
          {isCapabilityFailure && onChangeModel ? (
            <Button variant="outline" onClick={onChangeModel}>
              Choose another model
            </Button>
          ) : null}
        </div>
        {!showRetry && !isCapabilityFailure ? (
          <p className="text-xs text-muted-foreground">
            Adjust your inputs and run the scenario again.
          </p>
        ) : null}
      </CardContent>
    </Card>
  );
}
