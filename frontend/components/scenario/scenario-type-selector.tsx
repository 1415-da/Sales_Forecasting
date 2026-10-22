'use client';

import { CalendarDays, DoorClosed, HandCoins, PercentCircle, Sliders, Users } from 'lucide-react';
import type { LucideIcon } from 'lucide-react';

import { Card, CardContent, CardHeader, CardTitle } from '@/components/ui/card';
import { cn } from '@/lib/utils';
import type { ScenarioType } from '@/lib/types/scenario';

interface ScenarioTypeOption {
  type: ScenarioType;
  label: string;
  description: string;
  icon: LucideIcon;
}

/** Order matches the planner's intended workflow, not the API enum. */
const OPTIONS: ScenarioTypeOption[] = [
  {
    type: 'promotion',
    label: 'Promotion',
    description: 'Discount or campaign window',
    icon: PercentCircle,
  },
  {
    type: 'traffic',
    label: 'Traffic change',
    description: 'More or fewer customers',
    icon: Users,
  },
  {
    type: 'closure',
    label: 'Store closure',
    description: 'Days with no trading',
    icon: DoorClosed,
  },
  {
    type: 'holiday',
    label: 'Holiday impact',
    description: 'Treat days as a holiday',
    icon: CalendarDays,
  },
  {
    type: 'snap',
    label: 'SNAP benefit days',
    description: 'Add or remove benefit-payment days',
    icon: HandCoins,
  },
  {
    type: 'custom',
    label: 'Advanced: custom override',
    description: 'Set a model feature directly',
    icon: Sliders,
  },
];

export interface ScenarioTypeSelectorProps {
  value: ScenarioType;
  onChange: (value: ScenarioType) => void;
  /**
   * Types the selected model supports. Others are not shown at all: a lever
   * the model cannot express is not a choice, and showing it disabled only
   * invites "why can't I use this?". A future model trained on, say, real
   * promotion data brings its lever back with no code change.
   */
  supportedTypes?: ScenarioType[];
  unsupportedReason?: (type: ScenarioType) => string | null;
  isLoading?: boolean;
}

export function ScenarioTypeSelector({
  value,
  onChange,
  supportedTypes,
  unsupportedReason,
  isLoading = false,
}: ScenarioTypeSelectorProps) {
  // Until capabilities load, every option is listed (disabled by isLoading).
  const visible = OPTIONS.filter(
    (option) => !supportedTypes || supportedTypes.includes(option.type),
  );

  const isSupported = (type: ScenarioType) =>
    !supportedTypes || supportedTypes.includes(type);

  return (
    <Card>
      <CardHeader>
        <CardTitle>Scenario type</CardTitle>
      </CardHeader>
      <CardContent>
        <div role="radiogroup" aria-label="Scenario type" className="grid gap-2">
          {visible.map((option) => {
            const Icon = option.icon;
            const supported = isSupported(option.type);
            const selected = option.type === value;
            const reason = supported ? null : unsupportedReason?.(option.type) ?? null;
            const id = `scenario-type-${option.type}`;

            return (
              <label
                key={option.type}
                htmlFor={id}
                title={reason ?? option.description}
                className={cn(
                  'flex cursor-pointer items-start gap-3 rounded-md border p-3 text-left transition-colors',
                  'focus-within:ring-2 focus-within:ring-ring focus-within:ring-offset-2',
                  selected ? 'border-primary bg-accent' : 'border-input hover:bg-accent/50',
                  (!supported || isLoading) &&
                    'cursor-not-allowed opacity-50 hover:bg-transparent',
                )}
              >
                <input
                  id={id}
                  type="radio"
                  name="scenario-type"
                  className="sr-only"
                  checked={selected}
                  disabled={!supported || isLoading}
                  aria-describedby={reason ? `${id}-reason` : undefined}
                  onChange={() => onChange(option.type)}
                />
                <Icon aria-hidden="true" className="mt-0.5 h-4 w-4 shrink-0" />
                <span className="space-y-0.5">
                  <span className="block text-sm font-medium">{option.label}</span>
                  <span
                    id={reason ? `${id}-reason` : undefined}
                    className="block text-xs text-muted-foreground"
                  >
                    {reason ?? option.description}
                  </span>
                </span>
              </label>
            );
          })}
        </div>
      </CardContent>
    </Card>
  );
}
