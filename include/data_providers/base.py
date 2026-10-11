"""
Pluggable training data providers.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass
from typing import Any, Dict, List


@dataclass
class DataProviderResult:
    """Output of a data provider: paths plus lineage for MLflow."""

    data_output_dir: str
    file_paths: Dict[str, List[str]]
    total_files: int
    lineage: Dict[str, Any]


class DataProvider(ABC):
    """Load or materialize versioned training data."""

    def __init__(self, data_config: Dict[str, Any]):
        self.data_config = data_config

    @property
    @abstractmethod
    def source_name(self) -> str:
        pass

    @abstractmethod
    def provide(self) -> DataProviderResult:
        """Return standardized file_paths layout for the training DAG."""
        pass
