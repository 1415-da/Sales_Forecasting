'use client';

import { AlertTriangle, CheckCircle2, Info, XCircle } from 'lucide-react';

import { Alert, AlertDescription } from '@/components/ui/alert';
import { Card, CardContent, CardHeader, CardTitle } from '@/components/ui/card';
import { Skeleton } from '@/components/ui/skeleton';
import { humanizeIdentifier } from '@/lib/utils/format';
import { scenarioTypeLabel } from '@/lib/utils/scenario-results';
import type { ScenarioCapabilityStatus, ScenarioType } from '@/lib/types/scenario';

/** The disclosure the API attaches to every comparison, surfaced up front. */
export const WHAT_IF_DISCLAIMER = 'Predictive what-if analysis; not causal impact.';

export interface ModelSupportCardProps {
  capabilities: ScenarioCapabilityStatus[];
  selectedType: ScenarioType;
  isLoading?: boolean;
  /** Set when capabilities could not be read at all. */
  errorMessage?: string | null;
  /** Warnings from the capabilities response, e.g. missing dataset lineage. */
  warnings?: string[];
}

type SupportTone = 'supported' | 'partial' | 'unsupported';

/**
 * Classify a capability for display.
 *
 * "Partial" exists because a scenario can be runnable while still missing some
 * of its optional levers -- showing that as a plain green check would overstate
 * what the model can actually simulate.
 */
function toneFor(capability: ScenarioCapabilityStatus): SupportTone {
  if (!capability.supported) return 'unsupported';
  const missingOptional =
    capability.required_features.length > capability.available_features.length;
  return missingOptional ? 'partial' : 'supported';
}

const TONE_META: Record<
  SupportTone,
  { Icon: typeof CheckCircle2; className: string; srLabel: string }
> = {
  supported: {
    Icon: CheckCircle2,
    className: 'text-emerald-600',
    srLabel: 'Supported',
  },
  partial: {
    Icon: AlertTriangle,
    className: 'text-amber-600',
    srLabel: 'Partially supported',
  },
  unsupported: { Icon: XCircle, className: 'text-destructive', srLabel: 'Unavailable' },
};

export function ModelSupportCard({
  capabilities,
  selectedType,
  isLoading = false,
  errorMessage = null,
  warnings = [],
}: ModelSupportCardProps) {
  return (
    <Card>
      <CardHeader>
        <CardTitle>Model support</CardTitle>
      </CardHeader>

      <CardContent className="space-y-3">
        {isLoading ? (
          <div className="space-y-2" aria-busy="true" aria-label="Loading model support">
            <Skeleton className="h-4 w-full" />
            <Skeleton className="h-4 w-5/6" />
            <Skeleton className="h-4 w-4/6" />
          </div>
        ) : errorMessage ? (
          <Alert variant="destructive" role="alert">
            <XCircle aria-hidden="true" className="h-4 w-4" />
            <AlertDescription>{errorMessage}</AlertDescription>
          </Alert>
        ) : (
          <ul className="space-y-2">
            {capabilities.map((capability) => {
              const tone = toneFor(capability);
              const { Icon, className, srLabel } = TONE_META[tone];
              const isSelected = capability.scenario_type === selectedType;

              return (
                <li
                  key={capability.scenario_type}
                  className={`flex items-start gap-2 rounded-md p-1.5 text-sm ${
                    isSelected ? 'bg-accent' : ''
                  }`}
                  aria-current={isSelected ? 'true' : undefined}
                >
                  <Icon aria-hidden="true" className={`mt-0.5 h-4 w-4 shrink-0 ${className}`} />
                  <span className="sr-only">{srLabel}:</span>
                  <span className="min-w-0 space-y-0.5">
                    <span className="block font-medium">
                      {scenarioTypeLabel(capability.scenario_type)}
                    </span>
                    {capability.missing_features.length > 0 ? (
                      <span className="block break-words text-xs text-muted-foreground">
                        Missing: {capability.missing_features.map(humanizeIdentifier).join(', ')}
                      </span>
                    ) : capability.available_features.length > 0 ? (
                      <span className="block break-words text-xs text-muted-foreground">
                        Using: {capability.available_features.map(humanizeIdentifier).join(', ')}
                      </span>
                    ) : null}
                    {capability.reason ? (
                      <span className="block break-words text-xs text-muted-foreground">
                        {capability.reason}
                      </span>
                    ) : null}
                  </span>
                </li>
              );
            })}
          </ul>
        )}

        {warnings.map((warning) => (
          <Alert key={warning} variant="warning">
            <AlertTriangle aria-hidden="true" className="h-4 w-4" />
            <AlertDescription className="text-xs">{warning}</AlertDescription>
          </Alert>
        ))}

        <Alert variant="info">
          <Info aria-hidden="true" className="h-4 w-4" />
          <AlertDescription className="text-xs font-medium">
            {WHAT_IF_DISCLAIMER}
          </AlertDescription>
        </Alert>
      </CardContent>
    </Card>
  );
}
