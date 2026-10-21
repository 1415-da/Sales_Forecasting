import { Skeleton } from '@/components/ui/skeleton';
import { Card, CardContent, CardHeader } from '@/components/ui/card';

/**
 * Skeletons that match the layout they stand in for.
 *
 * Same card count, same heights, same grid: a skeleton with different
 * dimensions causes the page to jump when real content arrives, which reads as
 * a glitch.
 */
export function KpiSkeleton({ count = 4 }: { readonly count?: number }) {
  return (
    <div className="grid gap-4 sm:grid-cols-2 xl:grid-cols-4">
      {Array.from({ length: count }, (_, index) => (
        <Card key={index}>
          <CardContent className="space-y-2 p-5">
            <Skeleton className="h-3 w-24" />
            <Skeleton className="h-7 w-32" />
            <Skeleton className="h-3 w-20" />
          </CardContent>
        </Card>
      ))}
    </div>
  );
}

export function ChartSkeleton() {
  return (
    <Card>
      <CardHeader className="space-y-2">
        <Skeleton className="h-4 w-40" />
        <Skeleton className="h-3 w-64" />
      </CardHeader>
      <CardContent>
        <Skeleton className="h-[320px] w-full" />
      </CardContent>
    </Card>
  );
}

export function TableSkeleton({ rows = 6 }: { readonly rows?: number }) {
  return (
    <Card>
      <CardHeader>
        <Skeleton className="h-4 w-32" />
      </CardHeader>
      <CardContent className="space-y-2">
        {Array.from({ length: rows }, (_, index) => (
          <Skeleton key={index} className="h-9 w-full" />
        ))}
      </CardContent>
    </Card>
  );
}

/** The whole results panel, while a forecast is running. */
export function ForecastResultsSkeleton() {
  return (
    <div className="space-y-6">
      <KpiSkeleton />
      <ChartSkeleton />
      <TableSkeleton />
    </div>
  );
}
