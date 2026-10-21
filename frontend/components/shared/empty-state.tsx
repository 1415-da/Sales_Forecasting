import type { ReactNode } from 'react';

import { cn } from '@/lib/utils';

export interface EmptyStateProps {
  readonly icon?: ReactNode;
  readonly title: string;
  readonly description?: ReactNode;
  readonly action?: ReactNode;
  readonly className?: string;
}

/**
 * The "nothing here yet" panel.
 *
 * Always carries a next step: an empty state without one tells the user what is
 * missing but not what to do, which is where a console loses people.
 */
export function EmptyState({
  icon,
  title,
  description,
  action,
  className,
}: EmptyStateProps) {
  return (
    <div
      className={cn(
        'flex flex-col items-center justify-center gap-3 rounded-lg border border-dashed p-10 text-center',
        className,
      )}
    >
      {icon ? <span className="text-muted-foreground">{icon}</span> : null}
      <h3 className="text-base font-semibold">{title}</h3>
      {description ? (
        <p className="max-w-md text-sm text-muted-foreground">{description}</p>
      ) : null}
      {action}
    </div>
  );
}
