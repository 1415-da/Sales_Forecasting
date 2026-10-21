"""
Accuracy and drift calculations.

Pure functions over plain sequences: no database, no model, no HTTP. That is
what makes them testable against hand-checked numbers, and it is why the
monitoring DAG and the API can share one implementation instead of each
growing its own.

Metric choices worth stating
----------------------------
**WAPE is the headline, not MAPE.** MAPE divides by the actual, so a single
near-zero store-day sends it to thousands of percent and a day of exactly zero
makes it undefined. The M5-derived data has many such days. WAPE -- total
absolute error over total actual -- is bounded, interpretable as "we were off
by N% of volume", and defined whenever any sales occurred.

**Bias is reported separately from error.** A model that is 10% high every day
and one that alternates ±10% have the same MAE and very different problems.
Only the first is fixable by a recalibration.

**Interval coverage is checked, not assumed.** A 95% interval that contains the
actual 60% of the time is not a 95% interval. Coverage is the only way to find
that out, and it is the one number that tests the uncertainty band rather than
the point forecast.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from enum import Enum
from typing import Dict, List, Optional, Sequence, Tuple

# -- metric names, used as database keys and API field names ----------------
METRIC_WAPE = "wape"
METRIC_MAE = "mae"
METRIC_RMSE = "rmse"
METRIC_BIAS = "bias"
METRIC_MEAN_ERROR_PCT = "mean_error_pct"
METRIC_MAPE = "mape"
METRIC_COVERAGE = "interval_coverage"
METRIC_SAMPLE_SIZE = "sample_size"
METRIC_R2 = "r2"

ACCURACY_METRIC_NAMES = (
    METRIC_WAPE,
    METRIC_MAE,
    METRIC_RMSE,
    METRIC_BIAS,
    METRIC_MEAN_ERROR_PCT,
    METRIC_MAPE,
    METRIC_COVERAGE,
    METRIC_R2,
)


class DriftMethod(str, Enum):
    """Supported drift tests."""

    PSI = "psi"
    KS = "kolmogorov_smirnov"


#: Population Stability Index convention. Below 0.1 is considered stable, 0.1
#: to 0.25 worth watching, above 0.25 a material shift.
PSI_WARNING_THRESHOLD = 0.10
PSI_DRIFT_THRESHOLD = 0.25

#: The KS statistic is a maximum CDF gap, so it is on its own scale.
KS_WARNING_THRESHOLD = 0.10
KS_DRIFT_THRESHOLD = 0.20

#: Below this many observations a drift score is too noisy to act on.
MIN_DRIFT_SAMPLE = 20


@dataclass
class AccuracyResult:
    """Accuracy over one window, with the sample it was computed from."""

    sample_size: int
    wape: Optional[float] = None
    mae: Optional[float] = None
    rmse: Optional[float] = None
    bias: Optional[float] = None
    mean_error_pct: Optional[float] = None
    mape: Optional[float] = None
    interval_coverage: Optional[float] = None
    r2: Optional[float] = None

    @property
    def is_empty(self) -> bool:
        """Whether there was nothing to score."""
        return self.sample_size == 0

    def as_metric_map(self) -> Dict[str, float]:
        """
        The computed metrics as a name/value map, omitting anything undefined.

        A metric that could not be computed is absent rather than zero: zero
        error and unmeasurable error must not look the same in storage.
        """
        candidates = {
            METRIC_WAPE: self.wape,
            METRIC_MAE: self.mae,
            METRIC_RMSE: self.rmse,
            METRIC_BIAS: self.bias,
            METRIC_MEAN_ERROR_PCT: self.mean_error_pct,
            METRIC_MAPE: self.mape,
            METRIC_COVERAGE: self.interval_coverage,
            METRIC_R2: self.r2,
        }
        return {
            name: float(value)
            for name, value in candidates.items()
            if value is not None and math.isfinite(value)
        }


@dataclass
class DriftResult:
    """One feature's drift verdict."""

    feature_name: str
    method: DriftMethod
    score: float
    threshold: float
    status: str
    reference_size: int
    current_size: int
    insufficient_data: bool = False


def compute_accuracy(
    pairs: Sequence[Dict[str, object]],
) -> AccuracyResult:
    """
    Score forecast/actual pairs.

    Each pair needs ``predicted`` and ``actual``; ``lower_bound`` and
    ``upper_bound`` enable coverage. An empty input returns an empty result
    rather than zeros -- "no data" and "perfect" must be distinguishable.
    """
    predicted: List[float] = []
    actual: List[float] = []
    covered = 0
    coverable = 0

    for pair in pairs:
        try:
            forecast_value = float(pair["predicted"])  # type: ignore[arg-type]
            observed = float(pair["actual"])  # type: ignore[arg-type]
        except (KeyError, TypeError, ValueError):
            continue
        if not (math.isfinite(forecast_value) and math.isfinite(observed)):
            continue

        predicted.append(forecast_value)
        actual.append(observed)

        lower = pair.get("lower_bound")
        upper = pair.get("upper_bound")
        if lower is not None and upper is not None:
            try:
                coverable += 1
                if float(lower) <= observed <= float(upper):  # type: ignore[arg-type]
                    covered += 1
            except (TypeError, ValueError):
                coverable -= 1

    count = len(actual)
    if count == 0:
        return AccuracyResult(sample_size=0)

    errors = [f - a for f, a in zip(predicted, actual)]
    absolute = [abs(error) for error in errors]
    actual_total = sum(abs(value) for value in actual)

    result = AccuracyResult(
        sample_size=count,
        mae=sum(absolute) / count,
        rmse=math.sqrt(sum(error * error for error in errors) / count),
        bias=sum(errors) / count,
    )

    # WAPE needs some volume to divide by; a window of all-zero actuals has no
    # meaningful percentage error.
    if actual_total > 0:
        result.wape = sum(absolute) / actual_total
        result.mean_error_pct = sum(errors) / actual_total

    # MAPE over non-zero actuals only, and reported as a fraction. Zero-sales
    # days would otherwise make it infinite.
    non_zero = [(f, a) for f, a in zip(predicted, actual) if a != 0]
    if non_zero:
        result.mape = sum(abs((f - a) / a) for f, a in non_zero) / len(non_zero)

    if coverable > 0:
        result.interval_coverage = covered / coverable

    # R² needs variance in the actuals: a constant series has none, and the
    # denominator would be zero.
    actual_mean = sum(actual) / count
    total_variance = sum((value - actual_mean) ** 2 for value in actual)
    if total_variance > 0:
        residual = sum(error * error for error in errors)
        result.r2 = 1.0 - (residual / total_variance)

    return result


def population_stability_index(
    reference: Sequence[float],
    current: Sequence[float],
    *,
    buckets: int = 10,
) -> Tuple[float, int, int]:
    """
    PSI between a reference and a current sample.

    Returns ``(score, reference_size, current_size)``.

    Bucket edges come from the reference distribution's quantiles, so the
    reference is uniform across buckets by construction and the score measures
    how far the current sample departs from it. Empty buckets are floored at a
    small epsilon: the log ratio is otherwise infinite, which would report a
    single unseen bucket as total drift.
    """
    reference_values = [float(v) for v in reference if _is_number(v)]
    current_values = [float(v) for v in current if _is_number(v)]
    if not reference_values or not current_values:
        return 0.0, len(reference_values), len(current_values)

    # A constant reference has no spread to bucket by quantile. Detected here
    # rather than inferred from the edge list, which never comes back empty for
    # a non-empty sample -- so this case used to fall through to the general
    # path and produce an enormous, uninterpretable score instead of a bounded
    # one. Drift against a constant is a binary question: does the current
    # sample still sit on that value?
    if min(reference_values) == max(reference_values):
        constant = reference_values[0]
        off_value = sum(1 for v in current_values if v != constant) / len(
            current_values
        )
        return float(off_value), len(reference_values), len(current_values)

    edges = _quantile_edges(reference_values, buckets)
    if len(edges) < 2:
        # Defensive: a sample with spread should always yield usable edges.
        return 0.0, len(reference_values), len(current_values)

    reference_shares = _bucket_shares(reference_values, edges)
    current_shares = _bucket_shares(current_values, edges)

    epsilon = 1e-6
    score = 0.0
    for expected, observed in zip(reference_shares, current_shares):
        expected = max(expected, epsilon)
        observed = max(observed, epsilon)
        score += (observed - expected) * math.log(observed / expected)

    return float(abs(score)), len(reference_values), len(current_values)


def kolmogorov_smirnov(
    reference: Sequence[float], current: Sequence[float]
) -> Tuple[float, int, int]:
    """
    Two-sample KS statistic: the largest gap between the two empirical CDFs.

    Returns ``(statistic, reference_size, current_size)``. Implemented directly
    rather than via SciPy so this module stays dependency-free and usable in
    the API container, which has no need for the rest of SciPy.
    """
    reference_values = sorted(float(v) for v in reference if _is_number(v))
    current_values = sorted(float(v) for v in current if _is_number(v))
    if not reference_values or not current_values:
        return 0.0, len(reference_values), len(current_values)

    n_ref = len(reference_values)
    n_cur = len(current_values)
    i = j = 0
    cdf_ref = cdf_cur = 0.0
    statistic = 0.0

    # Walk both sorted samples together, advancing past all ties at the current
    # value before measuring -- comparing mid-tie would overstate the gap.
    while i < n_ref and j < n_cur:
        if reference_values[i] <= current_values[j]:
            value = reference_values[i]
            while i < n_ref and reference_values[i] == value:
                i += 1
                cdf_ref = i / n_ref
            while j < n_cur and current_values[j] == value:
                j += 1
                cdf_cur = j / n_cur
        else:
            value = current_values[j]
            while j < n_cur and current_values[j] == value:
                j += 1
                cdf_cur = j / n_cur
        statistic = max(statistic, abs(cdf_ref - cdf_cur))

    statistic = max(statistic, abs(1.0 - cdf_cur), abs(cdf_ref - 1.0))
    return float(min(statistic, 1.0)), n_ref, n_cur


def classify_drift(score: float, method: DriftMethod) -> str:
    """Turn a drift score into a verdict using that method's thresholds."""
    from api.db.models.monitoring import DriftStatus

    warning, drift = drift_thresholds(method)
    if score >= drift:
        return DriftStatus.DRIFT_DETECTED.value
    if score >= warning:
        return DriftStatus.WARNING.value
    return DriftStatus.STABLE.value


def drift_thresholds(method: DriftMethod) -> Tuple[float, float]:
    """``(warning, drift)`` thresholds for a method."""
    if method is DriftMethod.KS:
        return KS_WARNING_THRESHOLD, KS_DRIFT_THRESHOLD
    return PSI_WARNING_THRESHOLD, PSI_DRIFT_THRESHOLD


def evaluate_drift(
    *,
    feature_name: str,
    reference: Sequence[float],
    current: Sequence[float],
    method: DriftMethod = DriftMethod.PSI,
) -> DriftResult:
    """
    Score one feature and classify it.

    A sample below ``MIN_DRIFT_SAMPLE`` is marked ``insufficient_data`` and
    reported as stable: a PSI from twelve observations is noise, and raising an
    alert on it would train people to ignore drift alerts.
    """
    from api.db.models.monitoring import DriftStatus

    if method is DriftMethod.KS:
        score, reference_size, current_size = kolmogorov_smirnov(reference, current)
    else:
        score, reference_size, current_size = population_stability_index(
            reference, current
        )

    warning, drift = drift_thresholds(method)
    insufficient = (
        reference_size < MIN_DRIFT_SAMPLE or current_size < MIN_DRIFT_SAMPLE
    )
    status = (
        DriftStatus.STABLE.value if insufficient else classify_drift(score, method)
    )

    return DriftResult(
        feature_name=feature_name,
        method=method,
        score=score,
        threshold=drift,
        status=status,
        reference_size=reference_size,
        current_size=current_size,
        insufficient_data=insufficient,
    )


def relative_change(current: Optional[float], previous: Optional[float]) -> Optional[float]:
    """
    Fractional change from ``previous`` to ``current``.

    ``None`` when either value is missing or the baseline is zero -- a
    percentage change from zero is not a number, and reporting it as infinite
    or as 100% would both be wrong.
    """
    if current is None or previous is None:
        return None
    if previous == 0:
        return None
    if not (math.isfinite(current) and math.isfinite(previous)):
        return None
    return (current - previous) / abs(previous)


def _is_number(value: object) -> bool:
    """Whether a value is a usable finite number."""
    if value is None or isinstance(value, bool):
        return False
    try:
        return math.isfinite(float(value))  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return False


def _quantile_edges(values: Sequence[float], buckets: int) -> List[float]:
    """
    Interior bucket edges from a sample's quantiles, deduplicated.

    Duplicates are dropped so a heavily tied distribution produces fewer, wider
    buckets instead of several zero-width ones.
    """
    ordered = sorted(values)
    count = len(ordered)
    edges: List[float] = []
    for index in range(1, max(2, buckets)):
        position = index / max(2, buckets)
        edge = ordered[min(count - 1, int(position * count))]
        if not edges or edge > edges[-1]:
            edges.append(edge)
    return [-math.inf, *edges, math.inf] if edges else []


def _bucket_shares(values: Sequence[float], edges: Sequence[float]) -> List[float]:
    """Fraction of a sample falling into each bucket."""
    counts = [0] * (len(edges) - 1)
    for value in values:
        for index in range(len(edges) - 1):
            if edges[index] < value <= edges[index + 1]:
                counts[index] += 1
                break
        else:
            # Values equal to the lowest edge fall through the loop above.
            counts[0] += 1
    total = max(1, len(values))
    return [count / total for count in counts]


__all__ = [
    "ACCURACY_METRIC_NAMES",
    "KS_DRIFT_THRESHOLD",
    "KS_WARNING_THRESHOLD",
    "METRIC_BIAS",
    "METRIC_COVERAGE",
    "METRIC_MAE",
    "METRIC_MAPE",
    "METRIC_MEAN_ERROR_PCT",
    "METRIC_R2",
    "METRIC_RMSE",
    "METRIC_SAMPLE_SIZE",
    "METRIC_WAPE",
    "MIN_DRIFT_SAMPLE",
    "PSI_DRIFT_THRESHOLD",
    "PSI_WARNING_THRESHOLD",
    "AccuracyResult",
    "DriftMethod",
    "DriftResult",
    "classify_drift",
    "compute_accuracy",
    "drift_thresholds",
    "evaluate_drift",
    "kolmogorov_smirnov",
    "population_stability_index",
    "relative_change",
]
