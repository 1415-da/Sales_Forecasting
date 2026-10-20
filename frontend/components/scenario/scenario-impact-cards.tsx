'use client';

import { Minus, TrendingDown, TrendingUp } from 'lucide-react';

import { Card, CardContent } from '@/components/ui/card';
import { Skeleton } from '@/components/ui/skeleton';
import { cn } from '@/lib/utils';
import {
  formatCompactValue,
  formatSignedCompactValue,
  formatSignedPercent,
} from '@/lib/utils/format';
import { TONE_CLASSES, impactTone } from '@/lib/utils/scenario-results';
import type { ScenarioComparisonResponse } from '@/lib/types/scenario';

export interface ScenarioImpactCardsProps {
  comparison: ScenarioComparisonResponse;
  /** Supply only when the dataset's values are genuinely monetary. */
  currency?: string;
  locale?: string;
}

/** A P10-P90 range line, rendered only when the model produced quantiles. */
function QuantileRange({
  p10,
  p50,
  p90,
  currency,
  locale,
}: {
  p10: number | null;
  p50: number | null;
  p90: number | null;
  currency?: string;
  locale?: string;
}) {
  if (p10 === null || p90 === null) return null;
  const options = { currency, locale };
  return (
    <dl className="flex items-center gap-3 text-xs text-muted-foreground">
      <div className="flex gap-1">
        <dt>P10</dt>
        <dd className="font-medium tabular-nums">{formatCompactValue(p10, options)}</dd>
      </div>
      {p50 !== null ? (
        <div className="flex gap-1">
          <dt>P50</dt>
          <dd className="font-medium tabular-nums">{formatCompactValue(p50, options)}</dd>
        </div>
      ) : null}
      <div className="flex gap-1">
        <dt>P90</dt>
        <dd className="font-medium tabular-nums">{formatCompactValue(p90, options)}</dd>
      </div>
    </dl>
  );
}

export function ScenarioImpactCards({
  comparison,
  currency,
  locale,
}: ScenarioImpactCardsProps) {
  const options = { currency, locale };
  const { impact } = comparison;
  const tone = impactTone(impact.total_difference);
  const toneClasses = TONE_CLASSES[tone];
  const ToneIcon =
    tone === 'positive' ? TrendingUp : tone === 'negative' ? TrendingDown : Minus;

  // Quantile totals come from the API's own per-quantile aggregation, never
  // from summing the daily columns here.
  const byQuantile = new Map(impact.quantile_impacts.map((item) => [item.quantile, item]));

  return (
    <div className="grid gap-3 sm:grid-cols-2 xl:grid-cols-3">
      <Card>
        <CardContent className="space-y-2 p-4">
          <p className="text-xs font-medium uppercase tracking-wide text-muted-foreground">
            Baseline forecast
          </p>
          <p className="text-2xl font-semibold tabular-nums">
            {formatCompactValue(impact.total_baseline_forecast, options)}
          </p>
          <QuantileRange
            p10={byQuantile.get(0.1)?.total_baseline_forecast ?? null}
            p50={byQuantile.get(0.5)?.total_baseline_forecast ?? null}
            p90={byQuantile.get(0.9)?.total_baseline_forecast ?? null}
            currency={currency}
            locale={locale}
          />
        </CardContent>
      </Card>

      <Card>
        <CardContent className="space-y-2 p-4">
          <p className="text-xs font-medium uppercase tracking-wide text-muted-foreground">
            Scenario forecast
          </p>
          <p className="text-2xl font-semibold tabular-nums">
            {formatCompactValue(impact.total_scenario_forecast, options)}
          </p>
          <QuantileRange
            p10={byQuantile.get(0.1)?.total_scenario_forecast ?? null}
            p50={byQuantile.get(0.5)?.total_scenario_forecast ?? null}
            p90={byQuantile.get(0.9)?.total_scenario_forecast ?? null}
            currency={currency}
            locale={locale}
          />
        </CardContent>
      </Card>

      <Card className={cn('sm:col-span-2 xl:col-span-1', toneClasses.border, toneClasses.bg)}>
        <CardContent className="space-y-2 p-4">
          <p className="text-xs font-medium uppercase tracking-wide text-muted-foreground">
            Estimated impact
          </p>
          <p className={cn('flex items-center gap-2 text-2xl font-semibold tabular-nums', toneClasses.text)}>
            <ToneIcon aria-hidden="true" className="h-5 w-5" />
            {formatSignedCompactValue(impact.total_difference, options)}
            <span className="text-base font-medium">
              {/* Null when the API declined to divide by a zero or negative
                  baseline; a dash is shown rather than a fabricated 0%. */}
              {impact.total_difference_pct === null
                ? ''
                : `(${formatSignedPercent(impact.total_difference_pct, options)})`}
            </span>
          </p>
          <QuantileRange
            p10={impact.impact_range.total_difference_p10}
            p50={impact.impact_range.total_difference_p50}
            p90={impact.impact_range.total_difference_p90}
            currency={currency}
            locale={locale}
          />
        </CardContent>
      </Card>
    </div>
  );
}

/** Loading placeholder matching the card grid. */
export function ScenarioImpactCardsSkeleton() {
  return (
    <div
      className="grid gap-3 sm:grid-cols-2 xl:grid-cols-3"
      aria-busy="true"
      aria-label="Loading impact summary"
    >
      {[0, 1, 2].map((index) => (
        <Card key={index}>
          <CardContent className="space-y-3 p-4">
            <Skeleton className="h-3 w-24" />
            <Skeleton className="h-7 w-28" />
            <Skeleton className="h-3 w-40" />
          </CardContent>
        </Card>
      ))}
    </div>
  );
}
