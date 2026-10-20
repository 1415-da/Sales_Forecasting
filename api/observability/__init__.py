"""
Observability for the Scenario Planner: metrics, structured logs, health probes.

Deliberately dependency-free. The metrics registry renders the Prometheus text
exposition format directly, so a scrape endpoint works without adding a client
library; if ``prometheus_client`` is adopted later, ``scenario_metrics`` is the
single place that needs to change.
"""
