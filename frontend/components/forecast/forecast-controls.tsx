'use client';

import { Loader2, Play } from 'lucide-react';

import { Button } from '@/components/ui/button';
import { Card, CardContent, CardHeader, CardTitle } from '@/components/ui/card';
import { Label } from '@/components/ui/label';
import { Select } from '@/components/ui/select';
import { StatusBadge, stageTone } from '@/components/shared/status-badge';
import { formatMetric } from '@/lib/format';
import {
  CONFIDENCE_OPTIONS,
  HORIZON_OPTIONS,
  type ConfidenceOption,
  type HorizonOption,
  type ModelInfo,
} from '@/lib/types';

export interface ForecastControlsValue {
  readonly storeId: string;
  readonly model: string;
  readonly modelVersion: string;
  readonly horizonDays: HorizonOption;
  readonly confidenceLevel: ConfidenceOption;
}

export interface ForecastControlsProps {
  readonly value: ForecastControlsValue;
  readonly onChange: (patch: Partial<ForecastControlsValue>) => void;
  readonly models: readonly ModelInfo[];
  readonly stores: readonly string[];
  readonly isLoadingModels: boolean;
  readonly onSubmit: () => void;
  readonly isSubmitting: boolean;
  /** Why the run is blocked, when it is. Shown under the button. */
  readonly blockedReason?: string;
}

/**
 * What to forecast, and with which model.
 *
 * Store and version options come from the API, not from a constant: the stores
 * are the ones the selected model's encoder actually recognises, so an option
 * the model would reject is never offered.
 */
export function ForecastControls({
  value,
  onChange,
  models,
  stores,
  isLoadingModels,
  onSubmit,
  isSubmitting,
  blockedReason,
}: ForecastControlsProps) {
  const selected = models.find((model) => model.model === value.model);
  const versionOptions = selected ? [selected.version] : [];

  return (
    <Card>
      <CardHeader>
        <CardTitle>Forecast settings</CardTitle>
      </CardHeader>
      <CardContent className="space-y-4">
        <div className="space-y-2">
          <Label htmlFor="store">Store</Label>
          <Select
            id="store"
            value={value.storeId}
            disabled={isLoadingModels || stores.length === 0}
            onChange={(event) => onChange({ storeId: event.target.value })}
          >
            {stores.length === 0 ? (
              <option value="">No stores available</option>
            ) : (
              stores.map((store) => (
                <option key={store} value={store}>
                  {store}
                </option>
              ))
            )}
          </Select>
          <p className="text-xs text-muted-foreground">
            Stores the selected model was trained on.
          </p>
        </div>

        <div className="space-y-2">
          <Label htmlFor="model">Model</Label>
          <Select
            id="model"
            value={value.model}
            disabled={isLoadingModels || models.length === 0}
            onChange={(event) => {
              const next = models.find((model) => model.model === event.target.value);
              onChange({
                model: event.target.value,
                // Reset the version alongside the model: a version number is
                // only meaningful within one registered model.
                modelVersion: 'latest',
                ...(next && !next.supported_stores.includes(value.storeId)
                  ? { storeId: next.supported_stores[0] ?? value.storeId }
                  : {}),
              });
            }}
          >
            {models.length === 0 ? (
              <option value="">No models available</option>
            ) : (
              models.map((model) => (
                <option key={model.model} value={model.model}>
                  {model.label}
                  {model.is_production ? '' : ` (${model.stage})`}
                </option>
              ))
            )}
          </Select>
          {selected ? (
            <div className="flex flex-wrap items-center gap-2 pt-1">
              <StatusBadge tone={stageTone(selected.stage)}>{selected.stage}</StatusBadge>
              {selected.metrics.rmse != null ? (
                <span className="text-xs text-muted-foreground">
                  RMSE {formatMetric(selected.metrics.rmse)}
                </span>
              ) : null}
              {selected.metrics.mae != null ? (
                <span className="text-xs text-muted-foreground">
                  MAE {formatMetric(selected.metrics.mae)}
                </span>
              ) : null}
            </div>
          ) : null}
        </div>

        <div className="space-y-2">
          <Label htmlFor="version">Model version</Label>
          <Select
            id="version"
            value={value.modelVersion}
            disabled={isLoadingModels || !selected}
            onChange={(event) => onChange({ modelVersion: event.target.value })}
          >
            <option value="latest">Latest Production</option>
            {versionOptions.map((version) => (
              <option key={version} value={version}>
                Version {version}
              </option>
            ))}
          </Select>
          <p className="text-xs text-muted-foreground">
            Only the serving version is listed; the API accepts any registry
            version number.
          </p>
        </div>

        <div className="grid gap-4 sm:grid-cols-2">
          <div className="space-y-2">
            <Label htmlFor="horizon">Horizon</Label>
            <Select
              id="horizon"
              value={String(value.horizonDays)}
              onChange={(event) =>
                onChange({ horizonDays: Number(event.target.value) as HorizonOption })
              }
            >
              {HORIZON_OPTIONS.map((days) => (
                <option key={days} value={days}>
                  {days} days
                </option>
              ))}
            </Select>
          </div>

          <div className="space-y-2">
            <Label htmlFor="confidence">Confidence</Label>
            <Select
              id="confidence"
              value={String(value.confidenceLevel)}
              onChange={(event) =>
                onChange({
                  confidenceLevel: Number(event.target.value) as ConfidenceOption,
                })
              }
            >
              {CONFIDENCE_OPTIONS.map((level) => (
                <option key={level} value={level}>
                  {Math.round(level * 100)}%
                </option>
              ))}
            </Select>
          </div>
        </div>

        <div className="space-y-2 pt-1">
          <Button
            className="w-full"
            onClick={onSubmit}
            disabled={isSubmitting || Boolean(blockedReason) || !selected}
          >
            {isSubmitting ? (
              <>
                <Loader2 aria-hidden="true" className="h-4 w-4 animate-spin" />
                Generating…
              </>
            ) : (
              <>
                <Play aria-hidden="true" className="h-4 w-4" />
                Generate Forecast
              </>
            )}
          </Button>
          {blockedReason ? (
            <p className="text-xs text-muted-foreground">{blockedReason}</p>
          ) : null}
        </div>
      </CardContent>
    </Card>
  );
}
