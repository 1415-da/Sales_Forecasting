"""
Approved training configurations.

The browser picks one of these by id and nothing else: no hyper-parameters,
paths, or DAG options travel from a user to Airflow. The DAG resolves the id
against this same table, so an id the API accepted is one the DAG knows.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass
from typing import Any, Dict, Optional, Tuple

#: The only deployment policy for dataset-driven runs: candidates are
#: registered for review and never promoted by the DAG.
DEPLOYMENT_POLICY = "manual_review"

#: Models every configuration trains and registers, in registration order.
MODEL_FAMILIES: Tuple[str, ...] = ("xgboost", "lightgbm", "ensemble")


@dataclass(frozen=True)
class TrainingConfig:
    id: str
    label: str
    description: str
    use_optuna: bool
    optuna_trials: int
    #: Permission needed to request it. ``request_training`` is the Analyst
    #: baseline; the expensive search needs ``trigger_retraining``.
    required_permission: str
    #: Smallest scored test set the quality gate accepts.
    min_test_rows: int
    evaluation_policy: str
    expected_duration: str

    def as_dict(self) -> Dict[str, Any]:
        data = asdict(self)
        data["models"] = list(MODEL_FAMILIES)
        data["deployment_policy"] = DEPLOYMENT_POLICY
        return data


EVALUATION_POLICY = (
    "Chronological split by calendar date: the earliest 70% of dates train the "
    "models, the next 10% choose ensemble weights (and tune, when enabled), and "
    "the latest 20% is an untouched test window scored once. Preprocessing is "
    "fitted on the training window only."
)

TRAINING_CONFIGS: Dict[str, TrainingConfig] = {
    config.id: config
    for config in (
        TrainingConfig(
            id="standard_v1",
            label="Standard (fixed hyper-parameters)",
            description=(
                "XGBoost and LightGBM with the configured hyper-parameters, plus their "
                "validation-weighted ensemble."
            ),
            use_optuna=False,
            optuna_trials=0,
            required_permission="request_training",
            min_test_rows=100,
            evaluation_policy=EVALUATION_POLICY,
            expected_duration="a few minutes",
        ),
        TrainingConfig(
            id="tuned_v1",
            label="Tuned (Optuna search, 20 trials per model)",
            description=(
                "Hyper-parameters searched on the validation window before the "
                "final fit. Slower; reserved for ML engineers."
            ),
            use_optuna=True,
            optuna_trials=20,
            required_permission="trigger_retraining",
            min_test_rows=100,
            evaluation_policy=EVALUATION_POLICY,
            expected_duration="15 to 30 minutes",
        ),
    )
}


def get_config(config_id: Optional[str]) -> TrainingConfig:
    """The approved configuration, or ``KeyError`` for anything else."""
    if not config_id or config_id not in TRAINING_CONFIGS:
        raise KeyError(f"Unknown training configuration {config_id!r}")
    return TRAINING_CONFIGS[config_id]
