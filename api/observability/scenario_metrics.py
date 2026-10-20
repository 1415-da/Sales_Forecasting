"""
Metrics for the Scenario Planner.

A small, thread-safe, in-process registry that renders the Prometheus text
exposition format. No client library is required, which keeps the dependency
footprint unchanged; the trade-off is that counters live per process and reset
on restart, which is the normal expectation for a scrape target anyway.

Label cardinality is bounded on purpose: ``scenario_type``, ``status``, and
``error_code`` are all small enumerations. ``store_id`` is deliberately *not* a
label -- a few thousand stores would multiply every series and make the metric
unusable. Per-store detail belongs in the audit table.
"""

from __future__ import annotations

import logging
import math
import threading
import time
from contextlib import contextmanager
from dataclasses import dataclass, field
from typing import Dict, Iterable, Iterator, List, Mapping, Optional, Sequence, Tuple

logger = logging.getLogger(__name__)

LabelKey = Tuple[Tuple[str, str], ...]

#: Latency buckets in seconds. Scenario inference is slow, so the buckets reach
#: well past a second, and the top bucket sits at the frontend's 60s deadline.
DEFAULT_DURATION_BUCKETS: Tuple[float, ...] = (
    0.1,
    0.25,
    0.5,
    1.0,
    2.5,
    5.0,
    10.0,
    30.0,
    60.0,
)


def _normalise(labels: Optional[Mapping[str, object]]) -> LabelKey:
    """Freeze labels into a deterministic, hashable key."""
    if not labels:
        return ()
    return tuple(sorted((str(key), str(value)) for key, value in labels.items()))


def _escape(value: str) -> str:
    """Escape a label value per the Prometheus text format."""
    return value.replace("\\", "\\\\").replace('"', '\\"').replace("\n", "\\n")


def _render_labels(key: LabelKey, extra: Optional[Tuple[str, str]] = None) -> str:
    """Render a label set, or an empty string when there are none."""
    pairs = list(key)
    if extra:
        pairs.append(extra)
    if not pairs:
        return ""
    inner = ",".join(f'{name}="{_escape(value)}"' for name, value in pairs)
    return "{" + inner + "}"


@dataclass
class Counter:
    """A monotonically increasing count."""

    name: str
    help_text: str
    values: Dict[LabelKey, float] = field(default_factory=dict)

    def inc(self, amount: float = 1.0, labels: Optional[Mapping[str, object]] = None) -> None:
        """Add to the count. Negative increments are rejected, not clamped."""
        if amount < 0:
            raise ValueError("Counters may only increase")
        key = _normalise(labels)
        self.values[key] = self.values.get(key, 0.0) + amount

    def get(self, labels: Optional[Mapping[str, object]] = None) -> float:
        """Current value for one label set."""
        return self.values.get(_normalise(labels), 0.0)

    def total(self) -> float:
        """Sum across every label set."""
        return sum(self.values.values())

    def render(self) -> List[str]:
        """Prometheus text lines for this counter."""
        lines = [f"# HELP {self.name} {self.help_text}", f"# TYPE {self.name} counter"]
        if not self.values:
            lines.append(f"{self.name} 0")
        for key, value in sorted(self.values.items()):
            lines.append(f"{self.name}{_render_labels(key)} {value}")
        return lines


@dataclass
class Histogram:
    """Cumulative buckets, a sum, and a count, as Prometheus expects."""

    name: str
    help_text: str
    buckets: Tuple[float, ...] = DEFAULT_DURATION_BUCKETS
    counts: Dict[LabelKey, List[int]] = field(default_factory=dict)
    sums: Dict[LabelKey, float] = field(default_factory=dict)
    totals: Dict[LabelKey, int] = field(default_factory=dict)

    def observe(
        self, value: float, labels: Optional[Mapping[str, object]] = None
    ) -> None:
        """Record one observation. Non-finite values are ignored, not recorded."""
        if value is None or not math.isfinite(value):
            logger.debug("Ignoring non-finite observation for %s", self.name)
            return

        key = _normalise(labels)
        if key not in self.counts:
            self.counts[key] = [0] * len(self.buckets)
            self.sums[key] = 0.0
            self.totals[key] = 0

        for index, bound in enumerate(self.buckets):
            if value <= bound:
                self.counts[key][index] += 1
        self.sums[key] += value
        self.totals[key] += 1

    def count(self, labels: Optional[Mapping[str, object]] = None) -> int:
        """Number of observations for one label set."""
        return self.totals.get(_normalise(labels), 0)

    def sum(self, labels: Optional[Mapping[str, object]] = None) -> float:
        """Sum of observations for one label set."""
        return self.sums.get(_normalise(labels), 0.0)

    def total_count(self) -> int:
        """Observations across every label set."""
        return sum(self.totals.values())

    def total_sum(self) -> float:
        """Sum of observations across every label set."""
        return sum(self.sums.values())

    def average(self, labels: Optional[Mapping[str, object]] = None) -> Optional[float]:
        """Mean observation, or None when nothing has been recorded."""
        total = self.count(labels)
        return self.sum(labels) / total if total else None

    def render(self) -> List[str]:
        """Prometheus text lines for this histogram."""
        lines = [f"# HELP {self.name} {self.help_text}", f"# TYPE {self.name} histogram"]
        for key in sorted(self.counts):
            cumulative = 0
            for index, bound in enumerate(self.buckets):
                cumulative = self.counts[key][index]
                lines.append(
                    f"{self.name}_bucket"
                    f"{_render_labels(key, ('le', _format_bound(bound)))} {cumulative}"
                )
            lines.append(
                f"{self.name}_bucket{_render_labels(key, ('le', '+Inf'))} "
                f"{self.totals[key]}"
            )
            lines.append(f"{self.name}_sum{_render_labels(key)} {self.sums[key]}")
            lines.append(f"{self.name}_count{_render_labels(key)} {self.totals[key]}")
        if not self.counts:
            lines.append(f"{self.name}_count 0")
        return lines


def _format_bound(bound: float) -> str:
    """Render a bucket bound without a trailing ``.0`` for whole numbers."""
    return str(int(bound)) if float(bound).is_integer() else str(bound)


class ScenarioMetrics:
    """
    The Scenario Planner's metric set.

    Every mutator takes the registry lock, so metrics can be updated from
    request handlers running on different threads without losing increments.
    """

    def __init__(self, duration_buckets: Sequence[float] = DEFAULT_DURATION_BUCKETS) -> None:
        self._lock = threading.Lock()

        self.requests_total = Counter(
            "scenario_requests_total",
            "Scenario comparison requests received, by scenario type and outcome.",
        )
        self.requests_failed_total = Counter(
            "scenario_requests_failed_total",
            "Scenario comparison requests that failed, by scenario type and error code.",
        )
        self.duration_seconds = Histogram(
            "scenario_duration_seconds",
            "End-to-end scenario comparison duration in seconds.",
            buckets=tuple(duration_buckets),
        )
        self.rollback_total = Counter(
            "scenario_rollback_total",
            "Scenario rollbacks, by outcome.",
        )
        self.baseline_reuse_total = Counter(
            "baseline_reuse_total",
            "Baseline forecasts reused rather than regenerated.",
        )
        self.prediction_failures_total = Counter(
            "scenario_prediction_failures_total",
            "Prediction-stage failures, by error code.",
        )
        self.capability_checks_total = Counter(
            "scenario_capability_checks_total",
            "Scenario capability checks, by scenario type and outcome.",
        )

        self._all = (
            self.requests_total,
            self.requests_failed_total,
            self.duration_seconds,
            self.rollback_total,
            self.baseline_reuse_total,
            self.prediction_failures_total,
            self.capability_checks_total,
        )

    # ------------------------------------------------------------------
    # Recorders
    # ------------------------------------------------------------------

    def record_request(self, scenario_type: str, status: str) -> None:
        """Count one comparison request and its outcome."""
        with self._lock:
            self.requests_total.inc(
                labels={"scenario_type": scenario_type, "status": status}
            )

    def record_failure(
        self, scenario_type: str, error_code: str, stage: Optional[str] = None
    ) -> None:
        """
        Count a failed comparison.

        A prediction-stage failure is also counted under
        ``scenario_prediction_failures_total``, so model problems can be
        alerted on separately from client errors.
        """
        with self._lock:
            self.requests_failed_total.inc(
                labels={"scenario_type": scenario_type, "error_code": error_code}
            )
            if stage == "prediction" or error_code in (
                "PREDICTION_FAILED",
                "FORECAST_ALIGNMENT_ERROR",
            ):
                self.prediction_failures_total.inc(labels={"error_code": error_code})

    def record_duration(self, scenario_type: str, seconds: float) -> None:
        """Record how long a comparison took."""
        with self._lock:
            self.duration_seconds.observe(seconds, labels={"scenario_type": scenario_type})

    def record_rollback(self, outcome: str = "succeeded") -> None:
        """Count a rollback."""
        with self._lock:
            self.rollback_total.inc(labels={"outcome": outcome})

    def record_baseline(self, reused: bool) -> None:
        """
        Count baseline resolution.

        Both outcomes are recorded under one metric with a ``reused`` label, so
        a reuse *rate* is computable rather than just a reuse count.
        """
        with self._lock:
            self.baseline_reuse_total.inc(labels={"reused": str(reused).lower()})

    def record_capability_check(self, scenario_type: str, supported: bool) -> None:
        """Count a capability check and whether it passed."""
        with self._lock:
            self.capability_checks_total.inc(
                labels={
                    "scenario_type": scenario_type,
                    "outcome": "supported" if supported else "unsupported",
                }
            )

    @contextmanager
    def time_scenario(self, scenario_type: str) -> Iterator[None]:
        """
        Time a comparison, recording the duration whether or not it succeeds.

        A failed scenario's latency matters as much as a successful one's: a
        timeout that is never measured hides the problem.
        """
        started = time.perf_counter()
        try:
            yield
        finally:
            self.record_duration(scenario_type, time.perf_counter() - started)

    # ------------------------------------------------------------------
    # Exposition
    # ------------------------------------------------------------------

    def render(self) -> str:
        """Render every metric in the Prometheus text exposition format."""
        with self._lock:
            lines: List[str] = []
            for metric in self._all:
                lines.extend(metric.render())
            return "\n".join(lines) + "\n"

    def snapshot(self) -> Dict[str, object]:
        """Plain-dict view, for health payloads and tests."""
        with self._lock:
            return {
                "scenario_requests_total": self.requests_total.total(),
                "scenario_requests_failed_total": self.requests_failed_total.total(),
                # Across every label set: a per-label lookup here would read
                # the empty label key and always report zero.
                "scenario_duration_seconds_count": self.duration_seconds.total_count(),
                "scenario_duration_seconds_sum": self.duration_seconds.total_sum(),
                "scenario_rollback_total": self.rollback_total.total(),
                "baseline_reuse_total": self.baseline_reuse_total.total(),
                "scenario_prediction_failures_total": (
                    self.prediction_failures_total.total()
                ),
            }

    def reset(self) -> None:
        """Clear every metric. Intended for tests."""
        with self._lock:
            for metric in self._all:
                if isinstance(metric, Counter):
                    metric.values.clear()
                else:
                    metric.counts.clear()
                    metric.sums.clear()
                    metric.totals.clear()


#: Process-wide metrics. A module-level singleton because a scrape endpoint has
#: to read the same registry the request handlers wrote to.
METRICS = ScenarioMetrics()


def get_metrics() -> ScenarioMetrics:
    """The process metrics registry."""
    return METRICS


CONTENT_TYPE_PROMETHEUS = "text/plain; version=0.0.4; charset=utf-8"


def render_metrics(metrics: Optional[ScenarioMetrics] = None) -> str:
    """Render the registry for a scrape."""
    return (metrics or METRICS).render()


__all__ = [
    "CONTENT_TYPE_PROMETHEUS",
    "DEFAULT_DURATION_BUCKETS",
    "METRICS",
    "Counter",
    "Histogram",
    "ScenarioMetrics",
    "get_metrics",
    "render_metrics",
]
