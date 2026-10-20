'use client';

import { LineChart, Play } from 'lucide-react';

import { Button } from '@/components/ui/button';
import { Card, CardContent } from '@/components/ui/card';

export interface ScenarioEmptyStateProps {
  /** Offered only when sample data is actually available to run. */
  onRunSample?: () => void;
  isSampleAvailable?: boolean;
  isRunning?: boolean;
  /** Why a sample cannot be offered, when it cannot. */
  sampleUnavailableReason?: string | null;
}

export function ScenarioEmptyState({
  onRunSample,
  isSampleAvailable = false,
  isRunning = false,
  sampleUnavailableReason = null,
}: ScenarioEmptyStateProps) {
  const canRunSample = isSampleAvailable && Boolean(onRunSample);

  return (
    <Card className="border-dashed">
      <CardContent className="flex min-h-[320px] flex-col items-center justify-center gap-4 p-8 text-center">
        <div className="rounded-full bg-muted p-3">
          <LineChart aria-hidden="true" className="h-6 w-6 text-muted-foreground" />
        </div>

        <div className="space-y-1.5">
          <h3 className="text-base font-semibold">No scenario yet</h3>
          <p className="mx-auto max-w-sm text-sm text-muted-foreground">
            Choose a store, pick a scenario type, and set your assumptions. The
            comparison against the current baseline forecast appears here.
          </p>
        </div>

        {canRunSample ? (
          <Button onClick={onRunSample} disabled={isRunning}>
            <Play aria-hidden="true" className="h-4 w-4" />
            Run sample scenario
          </Button>
        ) : sampleUnavailableReason ? (
          <p className="max-w-sm text-xs text-muted-foreground">
            {sampleUnavailableReason}
          </p>
        ) : null}
      </CardContent>
    </Card>
  );
}
