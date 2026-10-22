'use client';

import { Card, CardContent, CardHeader, CardTitle } from '@/components/ui/card';
import { Label } from '@/components/ui/label';
import { RadioGroup } from '@/components/ui/radio-group';
import { Select } from '@/components/ui/select';
import { Skeleton } from '@/components/ui/skeleton';
import { Badge } from '@/components/ui/badge';
import {
  CONFIDENCE_OPTIONS,
  HORIZON_OPTIONS,
  type ConfidenceLevel,
  type FormErrors,
  type HorizonDays,
} from '@/hooks/use-scenario-form';
import { formatDate, humanizeIdentifier } from '@/lib/utils/format';

export interface StoreOption {
  id: string;
  label: string;
}

export interface ModelOption {
  version: string;
  label: string;
  stage?: string | null;
}

export interface ModelFamilyOption {
  id: string;
  label: string;
}

export interface ScenarioContextCardProps {
  stores: StoreOption[];
  /** Model families to choose from (Ensemble, XGBoost, LightGBM). */
  modelFamilies?: ModelFamilyOption[];
  model?: string;
  onModelFamilyChange?: (value: string) => void;
  models: ModelOption[];
  storeId: string;
  horizonDays: HorizonDays;
  modelVersion: string;
  confidenceLevel: ConfidenceLevel;
  onStoreChange: (value: string) => void;
  onHorizonChange: (value: HorizonDays) => void;
  onModelChange: (value: string) => void;
  onConfidenceChange: (value: ConfidenceLevel) => void;
  errors?: FormErrors;
  /** Resolved model metadata, from the capabilities response. */
  resolvedModelVersion?: string | null;
  modelStage?: string | null;
  /** Training-data end date, from a comparison's dataset lineage. */
  dataFreshness?: string | null;
  isLoading?: boolean;
}

export function ScenarioContextCard({
  stores,
  modelFamilies = [],
  model,
  onModelFamilyChange,
  models,
  storeId,
  horizonDays,
  modelVersion,
  confidenceLevel,
  onStoreChange,
  onHorizonChange,
  onModelChange,
  onConfidenceChange,
  errors = {},
  resolvedModelVersion,
  modelStage,
  dataFreshness,
  isLoading = false,
}: ScenarioContextCardProps) {
  return (
    <Card>
      <CardHeader>
        <CardTitle>Forecast context</CardTitle>
      </CardHeader>

      <CardContent className="space-y-4">
        <div className="space-y-1.5">
          <Label htmlFor="scenario-store">Store</Label>
          <Select
            id="scenario-store"
            value={storeId}
            onChange={(event) => onStoreChange(event.target.value)}
            aria-invalid={Boolean(errors.storeId)}
            aria-describedby={errors.storeId ? 'scenario-store-error' : undefined}
            disabled={isLoading}
          >
            <option value="">Select a store…</option>
            {stores.map((store) => (
              <option key={store.id} value={store.id}>
                {store.label}
              </option>
            ))}
          </Select>
          {errors.storeId ? (
            <p id="scenario-store-error" role="alert" className="text-xs text-destructive">
              {errors.storeId}
            </p>
          ) : null}
        </div>

        <fieldset className="space-y-1.5">
          <legend className="text-sm font-medium leading-none">Forecast horizon</legend>
          <RadioGroup
            name="scenario-horizon"
            aria-label="Forecast horizon in days"
            value={horizonDays}
            onValueChange={(value) => onHorizonChange(value as HorizonDays)}
            options={HORIZON_OPTIONS.map((days) => ({
              value: days,
              label: `${days}d`,
              hint: `${days}-day forecast`,
            }))}
          />
        </fieldset>

        {modelFamilies.length > 0 && onModelFamilyChange ? (
          <div className="space-y-1.5">
            <Label htmlFor="scenario-model-family">Model</Label>
            <Select
              id="scenario-model-family"
              value={model}
              onChange={(event) => onModelFamilyChange(event.target.value)}
              disabled={isLoading}
            >
              {modelFamilies.map((family) => (
                <option key={family.id} value={family.id}>
                  {family.label}
                </option>
              ))}
            </Select>
            <p className="text-xs text-muted-foreground">
              Run the same scenario on another model to see whether they agree.
            </p>
          </div>
        ) : null}

        <div className="space-y-1.5">
          <Label htmlFor="scenario-model">Model version</Label>
          <Select
            id="scenario-model"
            value={modelVersion}
            onChange={(event) => onModelChange(event.target.value)}
            disabled={isLoading}
          >
            {models.map((model) => (
              <option key={model.version} value={model.version}>
                {model.label}
              </option>
            ))}
          </Select>
        </div>

        <fieldset className="space-y-1.5">
          <legend className="text-sm font-medium leading-none">Confidence level</legend>
          <RadioGroup
            name="scenario-confidence"
            aria-label="Forecast confidence level"
            value={confidenceLevel}
            onValueChange={(value) => onConfidenceChange(value as ConfidenceLevel)}
            options={CONFIDENCE_OPTIONS.map((level) => ({
              value: level,
              label: `${level}%`,
              hint: level === 80 ? 'P10 to P90 band' : 'P2.5 to P97.5 band',
            }))}
          />
        </fieldset>

        <dl
          className="space-y-2 rounded-md bg-muted/50 p-3 text-xs"
          aria-busy={isLoading}
          aria-label="Selected model details"
        >
          {isLoading ? (
            <>
              <Skeleton className="h-3 w-32" />
              <Skeleton className="h-3 w-24" />
              <Skeleton className="h-3 w-40" />
            </>
          ) : (
            <>
              <div className="flex items-center justify-between gap-2">
                <dt className="text-muted-foreground">Model version</dt>
                <dd className="font-medium">{resolvedModelVersion ?? '—'}</dd>
              </div>
              <div className="flex items-center justify-between gap-2">
                <dt className="text-muted-foreground">Stage</dt>
                <dd>
                  {modelStage ? (
                    <Badge variant={modelStage === 'Production' ? 'success' : 'warning'}>
                      {humanizeIdentifier(modelStage)}
                    </Badge>
                  ) : (
                    '—'
                  )}
                </dd>
              </div>
              <div className="flex items-center justify-between gap-2">
                <dt className="text-muted-foreground">Data through</dt>
                <dd className="font-medium">
                  {dataFreshness ? formatDate(dataFreshness) : '—'}
                </dd>
              </div>
            </>
          )}
        </dl>
      </CardContent>
    </Card>
  );
}
