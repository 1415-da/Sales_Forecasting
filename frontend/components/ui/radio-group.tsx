import * as React from 'react';

import { cn } from '@/lib/utils';

/**
 * A segmented single-choice control.
 *
 * Implemented as a radio group rather than a row of buttons, so arrow keys move
 * between options and assistive tech announces "1 of 4".
 */
export interface RadioOption<T extends string | number> {
  value: T;
  label: string;
  disabled?: boolean;
  hint?: string;
}

export interface RadioGroupProps<T extends string | number> {
  name: string;
  value: T;
  options: RadioOption<T>[];
  onValueChange: (value: T) => void;
  className?: string;
  'aria-label'?: string;
  'aria-describedby'?: string;
}

export function RadioGroup<T extends string | number>({
  name,
  value,
  options,
  onValueChange,
  className,
  ...aria
}: RadioGroupProps<T>) {
  return (
    <div
      role="radiogroup"
      aria-label={aria['aria-label']}
      aria-describedby={aria['aria-describedby']}
      className={cn('flex flex-wrap gap-2', className)}
    >
      {options.map((option) => {
        const id = `${name}-${option.value}`;
        const selected = option.value === value;
        return (
          <label
            key={id}
            htmlFor={id}
            title={option.hint}
            className={cn(
              'cursor-pointer rounded-md border px-3 py-1.5 text-sm transition-colors',
              'focus-within:ring-2 focus-within:ring-ring focus-within:ring-offset-2',
              selected
                ? 'border-primary bg-primary text-primary-foreground'
                : 'border-input bg-background hover:bg-accent hover:text-accent-foreground',
              option.disabled && 'cursor-not-allowed opacity-50 hover:bg-background',
            )}
          >
            <input
              id={id}
              type="radio"
              name={name}
              className="sr-only"
              checked={selected}
              disabled={option.disabled}
              onChange={() => onValueChange(option.value)}
            />
            {option.label}
          </label>
        );
      })}
    </div>
  );
}
