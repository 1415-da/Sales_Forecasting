'use client';

import { ArrowRightLeft, Eye } from 'lucide-react';

import { StatusBadge, stageTone } from '@/components/shared/status-badge';
import { Button } from '@/components/ui/button';
import { Card, CardContent, CardDescription, CardHeader, CardTitle } from '@/components/ui/card';
import { formatMetric, formatRelative } from '@/lib/format';
import type { ModelInfo } from '@/lib/types';

export interface VersionTableProps {
  readonly models: readonly ModelInfo[];
  readonly onInspect: (model: ModelInfo) => void;
  readonly onCompare: (model: ModelInfo) => void;
  readonly canViewDetail: boolean;
}

/**
 * The serving version of each configured model.
 *
 * One row per model rather than per version, because that is the question the
 * page answers first: what is serving right now. Version history lives in the
 * detail drawer.
 */
export function VersionTable({
  models,
  onInspect,
  onCompare,
  canViewDetail,
}: VersionTableProps) {
  return (
    <Card>
      <CardHeader className="space-y-1">
        <CardTitle>Registered models</CardTitle>
        <CardDescription>
          The version the API will load for each model.
        </CardDescription>
      </CardHeader>
      <CardContent className="p-0">
        <div className="overflow-x-auto">
          <table className="w-full text-sm">
            <caption className="sr-only">
              Registered model versions with stage and training metrics
            </caption>
            <thead className="bg-muted/60">
              <tr>
                <th scope="col" className="px-4 py-2 text-left font-medium">
                  Model
                </th>
                <th scope="col" className="px-4 py-2 text-left font-medium">
                  Version
                </th>
                <th scope="col" className="px-4 py-2 text-left font-medium">
                  Stage
                </th>
                <th scope="col" className="px-4 py-2 text-right font-medium">
                  RMSE
                </th>
                <th scope="col" className="px-4 py-2 text-right font-medium">
                  MAE
                </th>
                <th scope="col" className="px-4 py-2 text-right font-medium">
                  WAPE
                </th>
                <th scope="col" className="px-4 py-2 text-left font-medium">
                  Trained
                </th>
                <th scope="col" className="px-4 py-2 text-left font-medium">
                  Run
                </th>
                <th scope="col" className="px-4 py-2 text-right font-medium">
                  Actions
                </th>
              </tr>
            </thead>
            <tbody>
              {models.map((model) => (
                <tr
                  key={`${model.model}-${model.version}`}
                  className={
                    model.is_production
                      ? 'border-t bg-emerald-500/5'
                      : 'border-t hover:bg-muted/40'
                  }
                >
                  <td className="whitespace-nowrap px-4 py-2">
                    <span className="font-medium">{model.label}</span>
                    {model.is_default ? (
                      <span className="ml-2 text-xs text-muted-foreground">
                        default
                      </span>
                    ) : null}
                  </td>
                  <td className="px-4 py-2 tabular-nums">v{model.version}</td>
                  <td className="px-4 py-2">
                    <StatusBadge tone={stageTone(model.stage)}>
                      {model.stage}
                    </StatusBadge>
                  </td>
                  <td className="px-4 py-2 text-right tabular-nums">
                    {formatMetric(model.metrics.rmse)}
                  </td>
                  <td className="px-4 py-2 text-right tabular-nums">
                    {formatMetric(model.metrics.mae)}
                  </td>
                  <td className="px-4 py-2 text-right tabular-nums">
                    {model.metrics.wape === null || model.metrics.wape === undefined
                      ? '—'
                      : `${(model.metrics.wape * 100).toFixed(2)}%`}
                  </td>
                  <td className="whitespace-nowrap px-4 py-2 text-xs text-muted-foreground">
                    {model.training_date ? formatRelative(model.training_date) : '—'}
                  </td>
                  <td className="px-4 py-2 font-mono text-xs text-muted-foreground">
                    {model.run_id ? model.run_id.slice(0, 12) : '—'}
                  </td>
                  <td className="px-4 py-2">
                    <div className="flex justify-end gap-1">
                      <Button
                        size="sm"
                        variant="ghost"
                        onClick={() => onInspect(model)}
                        disabled={!canViewDetail}
                        title={
                          canViewDetail
                            ? 'View lineage and artifacts'
                            : 'Requires detailed registry access'
                        }
                      >
                        <Eye aria-hidden="true" className="h-3.5 w-3.5" />
                        Detail
                      </Button>
                      <Button
                        size="sm"
                        variant="ghost"
                        onClick={() => onCompare(model)}
                        disabled={!canViewDetail}
                        title={
                          canViewDetail
                            ? 'Compare with another version'
                            : 'Requires detailed registry access'
                        }
                      >
                        <ArrowRightLeft aria-hidden="true" className="h-3.5 w-3.5" />
                        Compare
                      </Button>
                    </div>
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      </CardContent>
    </Card>
  );
}
