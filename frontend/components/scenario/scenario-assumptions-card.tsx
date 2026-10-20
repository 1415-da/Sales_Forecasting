'use client';

import { Card, CardContent, CardHeader, CardTitle } from '@/components/ui/card';
import { Input } from '@/components/ui/input';
import { Label } from '@/components/ui/label';
import { RadioGroup } from '@/components/ui/radio-group';
import { Select } from '@/components/ui/select';
import { Slider } from '@/components/ui/slider';
import {
  HOLIDAY_OPTIONS,
  IMPACT_LEVELS,
  MAX_PROMOTION_DURATION_DAYS,
  TRAFFIC_LIFT_MAX,
  TRAFFIC_LIFT_MIN,
  TRAFFIC_PCT_MAX,
  TRAFFIC_PCT_MIN,
  type AssumptionsDraft,
  type FormErrors,
} from '@/hooks/use-scenario-form';
import { humanizeIdentifier } from '@/lib/utils/format';
import type { ScenarioType } from '@/lib/types/scenario';

export interface ScenarioAssumptionsCardProps {
  scenarioType: ScenarioType;
  assumptions: AssumptionsDraft;
  onChange: (field: keyof AssumptionsDraft, value: string | number) => void;
  onBlur?: (field: string) => void;
  errors?: FormErrors;
  /** Features the model exposes, for the custom-override picker. */
  availableFeatures?: string[];
  maxDurationDays?: number;
}

/** A labelled field with inline error wiring, so every input is described. */
function Field({
  id,
  label,
  hint,
  error,
  children,
}: {
  id: string;
  label: string;
  hint?: string;
  error?: string;
  children: React.ReactNode;
}) {
  const hintId = hint ? `${id}-hint` : undefined;
  const errorId = error ? `${id}-error` : undefined;
  return (
    <div className="space-y-1.5">
      <Label htmlFor={id}>{label}</Label>
      {children}
      {hint ? (
        <p id={hintId} className="text-xs text-muted-foreground">
          {hint}
        </p>
      ) : null}
      {error ? (
        <p id={errorId} role="alert" className="text-xs text-destructive">
          {error}
        </p>
      ) : null}
    </div>
  );
}

export function ScenarioAssumptionsCard({
  scenarioType,
  assumptions,
  onChange,
  onBlur,
  errors = {},
  availableFeatures = [],
  maxDurationDays = MAX_PROMOTION_DURATION_DAYS,
}: ScenarioAssumptionsCardProps) {
  const describedBy = (field: string, hasHint: boolean) =>
    [errors[field] ? `${field}-error` : null, hasHint ? `${field}-hint` : null]
      .filter(Boolean)
      .join(' ') || undefined;

  const startDateField = (label = 'Start date') => (
    <Field id="startDate" label={label} error={errors.startDate}>
      <Input
        id="startDate"
        type="date"
        value={assumptions.startDate}
        aria-invalid={Boolean(errors.startDate)}
        aria-describedby={describedBy('startDate', false)}
        onChange={(event) => onChange('startDate', event.target.value)}
        onBlur={() => onBlur?.('startDate')}
      />
    </Field>
  );

  const durationField = (
    <Field
      id="durationDays"
      label="Duration (days)"
      hint={`1 to ${maxDurationDays} days, within the forecast horizon.`}
      error={errors.durationDays}
    >
      <Input
        id="durationDays"
        type="number"
        inputMode="numeric"
        min={1}
        max={maxDurationDays}
        value={assumptions.durationDays}
        aria-invalid={Boolean(errors.durationDays)}
        aria-describedby={describedBy('durationDays', true)}
        onChange={(event) => onChange('durationDays', Number(event.target.value))}
        onBlur={() => onBlur?.('durationDays')}
      />
    </Field>
  );

  return (
    <Card>
      <CardHeader>
        <CardTitle>Assumptions</CardTitle>
      </CardHeader>

      <CardContent className="space-y-4">
        {scenarioType === 'promotion' ? (
          <>
            {startDateField()}
            {durationField}

            <fieldset className="space-y-1.5">
              <legend className="text-sm font-medium leading-none">Intensity</legend>
              <RadioGroup
                name="promotion-intensity"
                aria-label="Promotion intensity"
                value={assumptions.intensity}
                onValueChange={(value) => onChange('intensity', value)}
                options={IMPACT_LEVELS.map((level) => ({
                  value: level,
                  label: humanizeIdentifier(level),
                }))}
              />
            </fieldset>

            <Field
              id="discountPct"
              label={`Discount depth: ${assumptions.discountPct}%`}
              error={errors.discountPct}
            >
              <Slider
                id="discountPct"
                min={0}
                max={100}
                step={5}
                value={assumptions.discountPct}
                aria-label="Discount depth, percent"
                aria-valuetext={`${assumptions.discountPct} percent`}
                onValueChange={(value) => onChange('discountPct', value)}
              />
            </Field>

            <Field
              id="trafficLiftPct"
              label={`Expected traffic lift: +${assumptions.trafficLiftPct}%`}
              hint="Applied only when the model includes a customer-traffic feature."
              error={errors.trafficLiftPct}
            >
              <Slider
                id="trafficLiftPct"
                min={TRAFFIC_LIFT_MIN}
                max={TRAFFIC_LIFT_MAX}
                step={5}
                value={assumptions.trafficLiftPct}
                aria-label="Expected traffic lift, percent"
                aria-valuetext={`plus ${assumptions.trafficLiftPct} percent`}
                onValueChange={(value) => onChange('trafficLiftPct', value)}
              />
            </Field>
          </>
        ) : null}

        {scenarioType === 'traffic' ? (
          <>
            {startDateField()}
            {durationField}

            <Field
              id="trafficChangePct"
              label={`Traffic change: ${assumptions.trafficChangePct > 0 ? '+' : ''}${assumptions.trafficChangePct}%`}
              hint={`From ${TRAFFIC_PCT_MIN}% to +${TRAFFIC_PCT_MAX}%.`}
              error={errors.trafficChangePct}
            >
              <Slider
                id="trafficChangePct"
                min={TRAFFIC_PCT_MIN}
                max={TRAFFIC_PCT_MAX}
                step={5}
                value={assumptions.trafficChangePct}
                aria-label="Traffic change, percent"
                aria-valuetext={`${assumptions.trafficChangePct} percent`}
                onValueChange={(value) => onChange('trafficChangePct', value)}
              />
            </Field>
          </>
        ) : null}

        {scenarioType === 'closure' ? (
          <>
            {startDateField('Closure start')}
            <Field id="endDate" label="Closure end" error={errors.endDate}>
              <Input
                id="endDate"
                type="date"
                value={assumptions.endDate}
                min={assumptions.startDate || undefined}
                aria-invalid={Boolean(errors.endDate)}
                aria-describedby={describedBy('endDate', false)}
                onChange={(event) => onChange('endDate', event.target.value)}
                onBlur={() => onBlur?.('endDate')}
              />
            </Field>
          </>
        ) : null}

        {scenarioType === 'holiday' ? (
          <>
            <Field id="holidayId" label="Holiday" error={errors.holidayId}>
              <Select
                id="holidayId"
                value={assumptions.holidayId}
                aria-invalid={Boolean(errors.holidayId)}
                aria-describedby={describedBy('holidayId', false)}
                onChange={(event) => onChange('holidayId', event.target.value)}
                onBlur={() => onBlur?.('holidayId')}
              >
                <option value="">Select a holiday…</option>
                {HOLIDAY_OPTIONS.map((holiday) => (
                  <option key={holiday.id} value={holiday.id}>
                    {holiday.label}
                  </option>
                ))}
              </Select>
            </Field>

            <fieldset className="space-y-1.5">
              <legend className="text-sm font-medium leading-none">Impact level</legend>
              <RadioGroup
                name="holiday-impact"
                aria-label="Holiday impact level"
                value={assumptions.intensity}
                onValueChange={(value) => onChange('intensity', value)}
                options={IMPACT_LEVELS.map((level) => ({
                  value: level,
                  label: humanizeIdentifier(level),
                }))}
              />
            </fieldset>
          </>
        ) : null}

        {scenarioType === 'price' ? (
          <>
            {startDateField()}
            {durationField}
            <Field
              id="discountPct"
              label={`Price change: -${assumptions.discountPct}%`}
              error={errors.discountPct}
            >
              <Slider
                id="discountPct"
                min={0}
                max={100}
                step={5}
                value={assumptions.discountPct}
                aria-label="Price reduction, percent"
                onValueChange={(value) => onChange('discountPct', value)}
              />
            </Field>
          </>
        ) : null}

        {scenarioType === 'custom' ? (
          <>
            {startDateField()}
            {durationField}

            <Field
              id="customFeature"
              label="Feature"
              hint="Only features the selected model was trained on can be overridden."
              error={errors.customFeature}
            >
              <Select
                id="customFeature"
                value={assumptions.customFeature}
                aria-invalid={Boolean(errors.customFeature)}
                aria-describedby={describedBy('customFeature', true)}
                onChange={(event) => onChange('customFeature', event.target.value)}
                onBlur={() => onBlur?.('customFeature')}
              >
                <option value="">Select a feature…</option>
                {availableFeatures.map((feature) => (
                  <option key={feature} value={feature}>
                    {humanizeIdentifier(feature)}
                  </option>
                ))}
              </Select>
            </Field>

            <Field id="customValue" label="Value" error={errors.customValue}>
              <Input
                id="customValue"
                type="number"
                inputMode="decimal"
                value={assumptions.customValue}
                aria-invalid={Boolean(errors.customValue)}
                aria-describedby={describedBy('customValue', false)}
                onChange={(event) => onChange('customValue', Number(event.target.value))}
                onBlur={() => onBlur?.('customValue')}
              />
            </Field>
          </>
        ) : null}
      </CardContent>
    </Card>
  );
}
