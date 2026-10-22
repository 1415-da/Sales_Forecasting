"""
The connected ingestion path: package -> adapter -> canonical -> validation.

``detect`` answers "what is this and what do you need from me?";
``process`` runs a confirmed adapter and the strict contract. Both return a
report with every issue tagged by stage, so the UI can show which stage
failed and say "row validation not performed" when it never ran.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

import pandas as pd

from data_ingestion import adapter_registry
from data_ingestion.adapters.base import Detection, Normalized
from data_ingestion.package_inspector import Package
from training import contract
from training.contract import Issue


@dataclass
class DetectionResult:
    detections: List[Detection]
    selected: Optional[str]
    reason: str
    needs_input: List[str] = field(default_factory=list)

    def as_dict(self) -> Dict[str, Any]:
        return {
            "candidates": [item.as_dict() for item in self.detections if item.match != "none"],
            "selected": self.selected,
            "reason": self.reason,
            "needs_input": self.needs_input,
        }


@dataclass
class ProcessResult:
    status: str  # "needs_mapping" | "invalid" | "validated"
    report: Dict[str, Any]
    frame: Optional[pd.DataFrame] = None
    context: Optional[pd.DataFrame] = None
    normalized: Optional[Normalized] = None


def missing_confirmations(detection: Detection, config: Dict[str, Any]) -> List[str]:
    """Required confirmations the configuration does not answer yet."""
    missing = []
    for item in detection.confirmations:
        if not item.required:
            continue
        if item.key in ("file", "date", "sales") and detection.adapter_id == "mapped_tabular":
            if not config.get(item.key) and not (item.key == "file" and detection.proposed_mapping.get("file")):
                missing.append(item.key)
            continue
        if config.get(item.key) in (None, ""):
            missing.append(item.key)
    return missing


def detect(package: Package, config: Dict[str, Any]) -> DetectionResult:
    detections = adapter_registry.detect_all(package)
    requested = config.get("adapter_id")
    if requested:
        selected = requested if requested in adapter_registry.ADAPTERS else None
        reason = "Chosen explicitly." if selected else f"Unknown adapter {requested!r}."
    else:
        choice = adapter_registry.choose(detections)
        selected, reason = choice["selected"], choice["reason"]
    result = DetectionResult(detections, selected, reason)
    if selected:
        detection = next(item for item in detections if item.adapter_id == selected)
        result.needs_input = missing_confirmations(detection, config)
    return result


def _stage_summary(issues: List[Dict[str, Any]], performed: Dict[str, bool]) -> Dict[str, Dict[str, Any]]:
    stages: Dict[str, Dict[str, Any]] = {}
    for stage in contract.STAGES:
        mine = [issue for issue in issues if issue.get("stage") == stage]
        if any(issue["severity"] == "error" for issue in mine):
            status = "failed"
        elif not performed.get(stage, False):
            status = "not_run"
        elif mine:
            status = "warning"
        else:
            status = "passed"
        stages[stage] = {"status": status, "issues": len(mine)}
    return stages


def report_for(
    *,
    package: Package,
    detection: Optional[DetectionResult],
    normalized: Optional[Normalized],
    validation: Optional[Dict[str, Any]],
    extra_issues: Optional[List[Issue]] = None,
) -> Dict[str, Any]:
    """One report across all stages; row counts stay null where not computed."""
    issues = [issue.as_dict() for issue in package.issues]
    if normalized is not None:
        issues += [issue.as_dict() for issue in normalized.issues]
    issues += [issue.as_dict() for issue in (extra_issues or [])]
    base: Dict[str, Any] = dict(validation or {})
    validation_issues = (base.pop("errors", None) or []) + (base.pop("warnings", None) or [])
    for item in validation_issues:
        item.setdefault("stage", "rows")
    issues += validation_issues
    errors = [issue for issue in issues if issue.get("severity", "error") == "error"]
    warnings = [issue for issue in issues if issue.get("severity") != "error"]
    performed = {
        "file": True,
        "detection": detection is not None,
        "mapping": normalized is not None,
        "schema": validation is not None,
        "rows": bool(validation and validation.get("row_validation_performed")),
        "readiness": bool(validation and validation.get("row_validation_performed")),
    }
    report = {
        "schema_version": contract.SCHEMA_VERSION,
        "rows_received": normalized.rows_received if normalized else None,
        "rows_valid": None,
        "rows_invalid": None,
        "row_validation_performed": False,
        **base,
        "status": "invalid" if errors else ("valid" if validation and validation.get("status") == "valid" else "incomplete"),
        "errors": errors,
        "warnings": warnings,
        "stages": _stage_summary(issues, performed),
        "files": {
            "data": [item.describe() for item in package.files],
            "excluded": package.excluded + (normalized.excluded_files if normalized else []),
        },
        "detection": detection.as_dict() if detection else None,
    }
    if normalized is not None:
        report.update(
            {
                "adapter_message": normalized.message,
                "transformations": normalized.transformations,
                "retained_columns": normalized.retained_columns,
                "store_metadata": normalized.store_metadata,
                "source_namespace": normalized.source_namespace,
                "target_type": normalized.target_type,
                "source_grain": normalized.source_grain,
                "mapping": normalized.mapping,
            }
        )
    return report


def process(
    package: Package,
    config: Dict[str, Any],
    policy: contract.ValidationPolicy,
    *,
    max_rows: int,
    source_type: str,
    require_history: bool = True,
) -> ProcessResult:
    """Detect, normalize with the confirmed adapter, and validate strictly."""
    if not package.ok:
        return ProcessResult("invalid", report_for(package=package, detection=None, normalized=None, validation=None))
    detection = detect(package, config)
    if not detection.selected:
        # A recognised source that lacks its training file (e.g. Rossmann
        # test.csv alone) is an error with a fix, not a mapping question.
        blockers = [
            Issue(
                "MISSING_REQUIRED_FILE",
                f"{item.label}: missing {missing}. " + " ".join(item.evidence),
                stage="detection",
                action="Upload the training file (with the target column).",
            )
            for item in detection.detections
            if item.match in ("exact", "partial")
            for missing in item.missing_files
        ]
        if blockers and not any(item.match == "exact" for item in detection.detections):
            return ProcessResult(
                "invalid",
                report_for(package=package, detection=detection, normalized=None, validation=None, extra_issues=blockers),
            )
    if not detection.selected or detection.needs_input:
        return ProcessResult(
            "needs_mapping",
            report_for(package=package, detection=detection, normalized=None, validation=None),
        )
    chosen = next(item for item in detection.detections if item.adapter_id == detection.selected)
    if not chosen.usable and chosen.adapter_id != "mapped_tabular":
        issues = [
            Issue("MISSING_REQUIRED_FILE", f"Missing: {missing}.", stage="detection") for missing in chosen.missing_files
        ] + [Issue("AMBIGUOUS_MAPPING", text, stage="detection") for text in chosen.ambiguities]
        return ProcessResult(
            "invalid",
            report_for(package=package, detection=detection, normalized=None, validation=None, extra_issues=issues),
        )

    adapter = adapter_registry.get_adapter(detection.selected)
    normalized = adapter.normalize(package, config, max_rows)
    if normalized.frame is None:
        needs = any(issue.code == "CONFIRMATION_REQUIRED" for issue in normalized.issues)
        return ProcessResult(
            "needs_mapping" if needs and not any(i.severity == "error" and i.code != "CONFIRMATION_REQUIRED" for i in normalized.issues) else "invalid",
            report_for(package=package, detection=detection, normalized=normalized, validation=None),
            normalized=normalized,
        )
    result = contract.validate(
        normalized.frame,
        policy,
        currency=str(config.get("currency") or ""),
        source_type=source_type,
        require_history=require_history,
        options=normalized.options,
        file=normalized.mapping.get("train_file") or normalized.mapping.get("file"),
    )
    report = report_for(package=package, detection=detection, normalized=normalized, validation=result.report)
    adapter_errors = any(issue.severity == "error" for issue in normalized.issues)
    if result.frame is None or adapter_errors:
        return ProcessResult("invalid", report, normalized=normalized)
    context = normalized.context
    if context is not None and report.get("stores_excluded"):
        context = context[~context["store_id"].isin(report["stores_excluded"])]
    return ProcessResult("validated", report, frame=result.frame, context=context, normalized=normalized)
