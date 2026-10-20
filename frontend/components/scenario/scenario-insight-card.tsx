'use client';

import { useMemo } from 'react';
import { Lightbulb } from 'lucide-react';

import { Card, CardContent, CardHeader, CardTitle } from '@/components/ui/card';
import { Skeleton } from '@/components/ui/skeleton';
import {
  buildInsightDetails,
  buildInsightSentence,
} from '@/lib/utils/scenario-results';
import type { ScenarioComparisonResponse } from '@/lib/types/scenario';

export interface ScenarioInsightCardProps {
  comparison: ScenarioComparisonResponse;
  /** Supply only when the dataset's values are genuinely monetary. */
  currency?: string;
  locale?: string;
}

/**
 * The narrative summary.
 *
 * Wording is associative by construction ("is associated with"), never causal:
 * the model finds correlations in historical patterns and cannot attribute a
 * forecast change to the scenario. `buildInsightSentence` is covered by a test
 * asserting no causal phrasing appears.
 */
export function ScenarioInsightCard({
  comparison,
  currency,
  locale,
}: ScenarioInsightCardProps) {
  const options = useMemo(() => ({ currency, locale }), [currency, locale]);
  const headline = useMemo(
    () => buildInsightSentence(comparison, options),
    [comparison, options],
  );
  const details = useMemo(
    () => buildInsightDetails(comparison, options),
    [comparison, options],
  );

  return (
    <Card>
      <CardHeader>
        <CardTitle className="flex items-center gap-2">
          <Lightbulb aria-hidden="true" className="h-4 w-4" />
          What this suggests
        </CardTitle>
      </CardHeader>
      <CardContent className="space-y-3">
        <p className="text-sm font-medium leading-relaxed">{headline}</p>
        {details.length > 0 ? (
          <ul className="space-y-1.5 text-sm text-muted-foreground">
            {details.map((detail) => (
              <li key={detail} className="flex gap-2">
                <span aria-hidden="true">•</span>
                <span>{detail}</span>
              </li>
            ))}
          </ul>
        ) : null}
      </CardContent>
    </Card>
  );
}

/** Loading placeholder matching the insight card. */
export function ScenarioInsightCardSkeleton() {
  return (
    <Card aria-busy="true" aria-label="Loading insight">
      <CardHeader>
        <Skeleton className="h-5 w-40" />
      </CardHeader>
      <CardContent className="space-y-2">
        <Skeleton className="h-4 w-full" />
        <Skeleton className="h-4 w-5/6" />
        <Skeleton className="h-4 w-3/4" />
      </CardContent>
    </Card>
  );
}
