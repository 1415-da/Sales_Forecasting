"""
Canonical store_day_v1 files: ``date, store_id, sales`` (+ optional context).

Behaviour is unchanged from before adapters existed: column names are matched
after trimming and case-folding, SNAP and the 25 December closure are derived
when absent (the platform's own M5-style data), and any other column is
refused by the strict validator rather than silently dropped. Sources with
other columns go through the Rossmann or mapped-tabular adapters, which say
what they retain.
"""

from __future__ import annotations

from typing import Any, Dict

from data_ingestion.adapters.base import (
    MISSING_DATES_CONFIRMATION,
    Adapter,
    Detection,
    Normalized,
    currency_confirmation,
)
from data_ingestion.package_inspector import Package
from training import contract
from training.contract import FeatureOptions, Issue

REQUIRED = set(contract.REQUIRED_COLUMNS)


class CanonicalStoreDayAdapter(Adapter):
    id = "canonical_store_day"
    version = "1"
    label = "Canonical store-day (date, store_id, sales)"

    def _matching(self, package: Package):
        return [data for data in package.files if REQUIRED <= set(data.normalized)]

    def detect(self, package: Package) -> Detection:
        matching = self._matching(package)
        detection = Detection(self.id, self.version, self.label, "none")
        if not matching:
            return detection
        detection.columns = {data.name: data.columns for data in matching}
        if len(matching) > 1:
            detection.match = "partial"
            detection.ambiguities.append(
                "Several files have date, store_id and sales: "
                + ", ".join(data.name for data in matching)
                + ". Upload one canonical file, or use append mode for more history."
            )
            return detection
        data = matching[0]
        extra = sorted(set(data.normalized) - REQUIRED - set(contract.CONTEXT_COLUMNS))
        detection.match = "partial" if extra else "exact"
        detection.evidence.append(f"{data.name} has date, store_id and sales.")
        if extra:
            detection.evidence.append(
                f"It also has {', '.join(extra)}, which store_day_v1 does not accept; "
                "map it with the generic adapter to retain them as context."
            )
        detection.files_used.append({"name": data.name, "role": "store-day history"})
        for other in package.files:
            if other is not data:
                detection.files_excluded.append({"name": other.name, "reason": "not a canonical store-day file"})
        detection.confirmations = [currency_confirmation(), MISSING_DATES_CONFIRMATION]
        detection.proposed_mapping = {name: data.normalized[name] for name in sorted(REQUIRED) if name in data.normalized}
        return detection

    def normalize(self, package: Package, config: Dict[str, Any], max_rows: int) -> Normalized:
        options = FeatureOptions(missing_dates=config.get("missing_dates", "reject"))
        matching = self._matching(package)
        if len(matching) != 1:
            return Normalized(
                None, options, "canonical", contract.TARGET_TYPE, contract.SOURCE_GRAIN,
                issues=[Issue("MISSING_REQUIRED_FILE", "Exactly one canonical store-day file is needed.", stage="detection")],
            )
        data = matching[0]
        frame = self.read_text_table(data, list(data.normalized.values()), max_rows)
        limit = self.row_limit_issue(frame, max_rows, data.name)
        if limit:
            return Normalized(None, options, "canonical", contract.TARGET_TYPE, contract.SOURCE_GRAIN, issues=[limit])
        frame.columns = [str(column).strip().casefold() for column in frame.columns]
        frame["_source_row"] = frame.index + 2
        return Normalized(
            frame=frame,
            options=options,
            source_namespace="canonical",
            target_type=contract.TARGET_TYPE,
            source_grain=contract.SOURCE_GRAIN,
            rows_received=len(frame),
            transformations=[
                {"source": data.normalized[name], "target": name, "rule": "unchanged"}
                for name in contract.REQUIRED_COLUMNS
            ]
            + [
                {"source": data.normalized[name], "target": name, "rule": "unchanged (0/1)"}
                for name in contract.CONTEXT_COLUMNS
                if name in data.normalized
            ],
            mapping={"file": data.name},
        )
