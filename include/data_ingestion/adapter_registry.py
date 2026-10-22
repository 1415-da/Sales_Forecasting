"""
Which adapters can read a package, and whether one can be chosen without asking.

An adapter is selected automatically only when it is the single ``exact``
match and needs nothing but the confirmations already supplied at upload.
Two exact matches, or only partial/manual matches, require an explicit
choice -- never an unexplained guess.
"""

from __future__ import annotations

from typing import Any, Dict, List, Optional

from data_ingestion.adapters.base import Adapter, Detection
from data_ingestion.adapters.canonical_store_day import CanonicalStoreDayAdapter
from data_ingestion.adapters.mapped_tabular import MappedTabularAdapter
from data_ingestion.adapters.rossmann import RossmannAdapter
from data_ingestion.package_inspector import Package

ADAPTERS: Dict[str, Adapter] = {
    adapter.id: adapter
    for adapter in (CanonicalStoreDayAdapter(), RossmannAdapter(), MappedTabularAdapter())
}


def get_adapter(adapter_id: str) -> Adapter:
    if adapter_id not in ADAPTERS:
        raise KeyError(f"Unknown adapter {adapter_id!r}")
    return ADAPTERS[adapter_id]


def detect_all(package: Package) -> List[Detection]:
    return [adapter.detect(package) for adapter in ADAPTERS.values()]


def choose(detections: List[Detection]) -> Dict[str, Any]:
    """The automatic choice, or why one is not possible."""
    exact = [item for item in detections if item.match == "exact" and item.usable]
    if len(exact) == 1:
        return {"selected": exact[0].adapter_id, "reason": exact[0].evidence[0] if exact[0].evidence else "exact match"}
    if len(exact) > 1:
        return {
            "selected": None,
            "reason": "More than one adapter matches exactly: " + ", ".join(item.label for item in exact) + ". Choose one.",
        }
    return {
        "selected": None,
        "reason": "No adapter recognises this data exactly. Choose an adapter, or map the columns by hand.",
    }


def recommended(detections: List[Detection]) -> Optional[str]:
    return choose(detections)["selected"]
