'use client';

import { Card, CardContent, CardHeader, CardTitle } from '@/components/ui/card';
import { Tabs, TabsContent, TabsList, TabsTrigger } from '@/components/ui/tabs';
import { ScenarioCalendarHeatmap } from '@/components/scenario/scenario-calendar-heatmap';
import { ScenarioComparisonChartCard } from '@/components/scenario/scenario-comparison-chart-card';
import { ScenarioImpactTable } from '@/components/scenario/scenario-impact-table';
import type { HistoryPoint, ScenarioComparisonResponse } from '@/lib/types/scenario';

export interface ScenarioImpactTabsCardProps {
  comparison: ScenarioComparisonResponse;
  history?: HistoryPoint[];
  locale?: string;
}

/**
 * Three views of the same comparison.
 *
 * The chart tab renders its own card, so this wrapper drops its chrome there to
 * avoid a card inside a card.
 */
export function ScenarioImpactTabsCard({
  comparison,
  history = [],
  locale,
}: ScenarioImpactTabsCardProps) {
  return (
    <Tabs defaultValue="chart" className="w-full">
      <TabsList aria-label="Impact views">
        <TabsTrigger value="chart">Impact over time</TabsTrigger>
        <TabsTrigger value="table">Impact table</TabsTrigger>
        <TabsTrigger value="calendar">Calendar</TabsTrigger>
      </TabsList>

      <TabsContent value="chart">
        <ScenarioComparisonChartCard
          comparison={comparison}
          history={history}
          locale={locale}
        />
      </TabsContent>

      <TabsContent value="table">
        <Card>
          <CardHeader>
            <CardTitle>Daily impact</CardTitle>
          </CardHeader>
          <CardContent>
            <ScenarioImpactTable
              points={comparison.daily_forecasts}
              modifiedDates={comparison.modified_dates}
              locale={locale}
            />
          </CardContent>
        </Card>
      </TabsContent>

      <TabsContent value="calendar">
        <Card>
          <CardHeader>
            <CardTitle>Impact calendar</CardTitle>
          </CardHeader>
          <CardContent>
            <ScenarioCalendarHeatmap
              points={comparison.daily_forecasts}
              locale={locale}
            />
          </CardContent>
        </Card>
      </TabsContent>
    </Tabs>
  );
}
