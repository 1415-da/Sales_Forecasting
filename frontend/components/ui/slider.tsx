import * as React from 'react';

import { cn } from '@/lib/utils';

/**
 * Native range input, styled like shadcn's slider.
 *
 * `<input type="range">` is already operable by arrow keys and exposes
 * `aria-valuenow` without extra wiring.
 */
export interface SliderProps
  extends Omit<React.InputHTMLAttributes<HTMLInputElement>, 'type' | 'value' | 'onChange'> {
  value: number;
  onValueChange: (value: number) => void;
}

const Slider = React.forwardRef<HTMLInputElement, SliderProps>(
  ({ className, value, onValueChange, ...props }, ref) => (
    <input
      ref={ref}
      type="range"
      value={value}
      onChange={(event) => onValueChange(Number(event.target.value))}
      className={cn(
        'h-2 w-full cursor-pointer appearance-none rounded-full bg-secondary accent-primary focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring focus-visible:ring-offset-2 disabled:cursor-not-allowed disabled:opacity-50',
        className,
      )}
      {...props}
    />
  ),
);
Slider.displayName = 'Slider';

export { Slider };
