'use client';

import { Info } from 'lucide-react';

import { Card, CardContent, CardHeader, CardTitle } from '@/components/ui/card';
import { formatDate, humanizeIdentifier } from '@/lib/utils/format';
import { modelFamilyLabel, scenarioTypeLabel } from '@/lib/utils/scenario-results';
import type { ScenarioComparisonResponse } from '@/lib/types/scenario';

/**
 * Limitations shown even when the API sends none.
 *
 * The backend attaches its four limitation strings to every comparison, but
 * this card must never render empty: a what-if figure without its caveats
 * reads as a forecast guarantee.
 */
export const FALLBACK_LIMITATIONS = [
  'This is a predictive what-if estimate based on the selected production model.',
  'It does not establish causal impact.',
  'Assumes scenario effects resemble historical patterns.',
  'Uncertainty reflects model error and does not include external shocks.',
];

export interface ScenarioLimitationsCardProps {
  comparison: ScenarioComparisonResponse;
  locale?: string;
}

export function ScenarioLimitationsCard({
  comparison,
  locale,
}: ScenarioLimitationsCardProps) {
  const limitations =
    comparison.limitations.length > 0 ? comparison.limitations : FALLBACK_LIMITATIONS;
  const assumptions = comparison.assumptions;
  const lineage = comparison.dataset_lineage;

  return (
    <Card>
      <CardHeader>
        <CardTitle className="flex items-center gap-2">
          <Info aria-hidden="true" className="h-4 w-4" />
          Assumptions and limitations
        </CardTitle>
      </CardHeader>

      <CardContent className="space-y-4 text-sm">
        <section className="space-y-1.5">
          <h4 className="text-xs font-semibold uppercase tracking-wide text-muted-foreground">
            Your assumptions
          </h4>
          <dl className="space-y-1 text-xs">
            <div className="flex justify-between gap-3">
              <dt className="text-muted-foreground">Scenario</dt>
              <dd className="text-right font-medium">
                {scenarioTypeLabel(comparison.scenario_type)}
              </dd>
            </div>
            {assumptions?.start_date ? (
              <div className="flex justify-between gap-3">
                <dt className="text-muted-foreground">Window</dt>
                <dd className="text-right font-medium">
                  {formatDate(assumptions.start_date, { locale })}
                  {assumptions.end_date && assumptions.end_date !== assumptions.start_date
                    ? ` – ${formatDate(assumptions.end_date, { locale })}`
                    : ''}
                </dd>
              </div>
            ) : null}
            {assumptions?.discount_depth !== null && assumptions?.discount_depth !== undefined ? (
              <div className="flex justify-between gap-3">
                <dt className="text-muted-foreground">Discount depth</dt>
                <dd className="text-right font-medium">
                  {Math.round(assumptions.discount_depth * 100)}%
                </dd>
              </div>
            ) : null}
            {assumptions?.traffic_multiplier ? (
              <div className="flex justify-between gap-3">
                <dt className="text-muted-foreground">Traffic change</dt>
                <dd className="text-right font-medium">
                  {Math.round((assumptions.traffic_multiplier - 1) * 100)}%
                </dd>
              </div>
            ) : null}
            <div className="flex justify-between gap-3">
              <dt className="text-muted-foreground">Days modified</dt>
              <dd className="text-right font-medium">{comparison.modified_dates.length}</dd>
            </div>
            <div className="flex justify-between gap-3">
              <dt className="text-muted-foreground">Features changed</dt>
              <dd className="text-right font-medium">
                {comparison.modified_features.length > 0
                  ? comparison.modified_features.map(humanizeIdentifier).join(', ')
                  : 'None'}
              </dd>
            </div>
          </dl>
        </section>

        <section className="space-y-1.5">
          <h4 className="text-xs font-semibold uppercase tracking-wide text-muted-foreground">
            Model and uncertainty
          </h4>
          <dl className="space-y-1 text-xs">
            <div className="flex justify-between gap-3">
              <dt className="text-muted-foreground">Model</dt>
              <dd className="text-right font-medium">
                {modelFamilyLabel(comparison.model)} v{comparison.model_version} (
                {humanizeIdentifier(comparison.model_stage)})
              </dd>
            </div>
            <div className="flex justify-between gap-3">
              <dt className="text-muted-foreground">Interval method</dt>
              <dd className="text-right font-medium">
                {humanizeIdentifier(comparison.baseline.uncertainty_method ?? 'unknown')}
              </dd>
            </div>
            {lineage ? (
              <div className="flex justify-between gap-3">
                <dt className="text-muted-foreground">Training data</dt>
                <dd className="text-right font-medium">
                  {lineage.lineage_status === 'unavailable'
                    ? 'Lineage unavailable'
                    : `${lineage.data_source ?? '—'} ${lineage.dataset_version ?? ''}`.trim()}
                </dd>
              </div>
            ) : null}
          </dl>
        </section>

        <section className="space-y-1.5">
          <h4 className="text-xs font-semibold uppercase tracking-wide text-muted-foreground">
            Limitations
          </h4>
          <ul className="space-y-1 text-xs text-muted-foreground">
            {limitations.map((limitation) => (
              <li key={limitation} className="flex gap-2">
                <span aria-hidden="true">•</span>
                <span>{limitation}</span>
              </li>
            ))}
          </ul>
        </section>

        {comparison.warnings.length > 0 ? (
          <section className="space-y-1.5">
            <h4 className="text-xs font-semibold uppercase tracking-wide text-muted-foreground">
              Notes
            </h4>
            <ul className="space-y-1 text-xs text-muted-foreground">
              {comparison.warnings.map((warning) => (
                <li key={warning} className="flex gap-2">
                  <span aria-hidden="true">•</span>
                  <span>{warning}</span>
                </li>
              ))}
            </ul>
          </section>
        ) : null}
      </CardContent>
    </Card>
  );
}
