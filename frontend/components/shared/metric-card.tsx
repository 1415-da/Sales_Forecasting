import type { ReactNode } from 'react';

import { Card, CardContent } from '@/components/ui/card';
import { cn } from '@/lib/utils';

export interface MetricCardProps {
  readonly label: string;
  readonly value: ReactNode;
  /** Secondary line: a range, a unit, a date span. */
  readonly hint?: ReactNode;
  readonly icon?: ReactNode;
  readonly className?: string;
}

/**
 * One headline number.
 *
 * The value renders in a tabular-numeral font so a row of cards does not
 * shift horizontally as digits change.
 */
export function MetricCard({ label, value, hint, icon, className }: MetricCardProps) {
  return (
    <Card className={cn('overflow-hidden', className)}>
      <CardContent className="flex items-start justify-between gap-3 p-5">
        <div className="min-w-0 space-y-1">
          <p className="text-xs font-medium uppercase tracking-wide text-muted-foreground">
            {label}
          </p>
          <p className="truncate text-2xl font-semibold tabular-nums tracking-tight">
            {value}
          </p>
          {hint ? <p className="text-xs text-muted-foreground">{hint}</p> : null}
        </div>
        {icon ? (
          <span className="shrink-0 rounded-md bg-muted p-2 text-muted-foreground">
            {icon}
          </span>
        ) : null}
      </CardContent>
    </Card>
  );
}
