"""Record the model family on scenario baseline checkpoints.

The Scenario Planner can now forecast with any configured model (ensemble,
xgboost, lightgbm), so a checkpoint must say which one produced it -- otherwise
reusing a baseline could compare a scenario from one model against a baseline
from another.

Nullable and idempotent, like the initial migration: the API's start-up also
adds this column (``api.db.session.ADDITIVE_COLUMNS``), so this revision often
runs against a database that already has it. Rows without a value predate model
choice and were all produced by the default model.
"""
from __future__ import annotations

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

revision: str = '5b2c7e1d9a04'
down_revision: Union[str, None] = '196a4d7f9440'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

TABLE = 'scenario_baseline_forecasts'


def _columns() -> set:
    """Column names currently on the checkpoint table, empty if it is absent."""
    inspector = sa.inspect(op.get_bind())
    if TABLE not in inspector.get_table_names():
        return set()
    return {column['name'] for column in inspector.get_columns(TABLE)}


def upgrade() -> None:
    columns = _columns()
    if columns and 'model' not in columns:
        op.add_column(TABLE, sa.Column('model', sa.String(length=32), nullable=True))


def downgrade() -> None:
    if 'model' in _columns():
        op.drop_column(TABLE, 'model')
