import * as React from 'react';

import { cn } from '@/lib/utils';

/** A loading placeholder. Hidden from assistive tech; pair with aria-busy. */
function Skeleton({ className, ...props }: React.HTMLAttributes<HTMLDivElement>) {
  return (
    <div
      aria-hidden="true"
      className={cn('animate-pulse rounded-md bg-muted', className)}
      {...props}
    />
  );
}

export { Skeleton };
