"""
Column-name normalization shared by every adapter.

A source column is matched on its name trimmed and case-folded, so
``" DayOfWeek "``, ``"dayofweek"`` and ``"DAYOFWEEK"`` are the same field.
Two raw columns that collapse to the same normalized name are ambiguous and
are reported, never silently merged or one picked.
"""

from __future__ import annotations

from typing import Dict, List, Sequence, Tuple


def normalize_name(name: str) -> str:
    return str(name).strip().casefold()


def normalize_columns(columns: Sequence[str]) -> Tuple[Dict[str, str], Dict[str, List[str]]]:
    """``(normalized -> raw, normalized -> [raw, ...] for ambiguous names)``."""
    groups: Dict[str, List[str]] = {}
    for column in columns:
        groups.setdefault(normalize_name(column), []).append(str(column))
    mapping = {key: raws[0] for key, raws in groups.items() if len(raws) == 1}
    ambiguous = {key: raws for key, raws in groups.items() if len(raws) > 1}
    return mapping, ambiguous
