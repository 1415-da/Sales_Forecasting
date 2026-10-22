"""Training datasets, training requests, and deployment decisions.

Backs the Monitoring "Data & Retraining" workspace: immutable dataset versions,
the Airflow runs trained on them, and the approvals that deploy a candidate.

Idempotent like the earlier revisions: the API's start-up ``create_all`` may
already have created these tables, so each create is guarded.
"""
from __future__ import annotations

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision: str = '8d41c0a7e2f3'
down_revision: Union[str, None] = '5b2c7e1d9a04'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

JSON = sa.JSON().with_variant(postgresql.JSONB(astext_type=sa.Text()), 'postgresql')


def _tables() -> set:
    return set(sa.inspect(op.get_bind()).get_table_names())


def _create_index(name: str, table: str, columns, unique: bool = False) -> None:
    existing = {item['name'] for item in sa.inspect(op.get_bind()).get_indexes(table)}
    if name not in existing:
        op.create_index(name, table, columns, unique=unique)


def upgrade() -> None:
    tables = _tables()
    if 'training_datasets' not in tables:
        op.create_table(
            'training_datasets',
            sa.Column('id', sa.Uuid(), nullable=False),
            sa.Column('name', sa.String(length=80), nullable=False),
            sa.Column('version', sa.Integer(), nullable=False),
            sa.Column('status', sa.String(length=16), nullable=False),
            sa.Column('mode', sa.String(length=16), nullable=False),
            sa.Column('base_dataset_id', sa.Uuid(), nullable=True),
            sa.Column('conflict_policy', sa.String(length=16), nullable=True),
            sa.Column('schema_version', sa.String(length=32), nullable=False),
            sa.Column('target', sa.String(length=32), nullable=False),
            sa.Column('currency', sa.String(length=3), nullable=False),
            sa.Column('frequency', sa.String(length=8), nullable=False),
            sa.Column('source_types', JSON, nullable=False),
            sa.Column('original_filename', sa.String(length=255), nullable=True),
            sa.Column('file_format', sa.String(length=16), nullable=False),
            sa.Column('raw_object_key', sa.String(length=255), nullable=False),
            sa.Column('raw_sha256', sa.String(length=64), nullable=False),
            sa.Column('raw_size_bytes', sa.BigInteger(), nullable=False),
            sa.Column('processed_object_key', sa.String(length=255), nullable=True),
            sa.Column('processed_object_sha256', sa.String(length=64), nullable=True),
            sa.Column('content_sha256', sa.String(length=64), nullable=True),
            sa.Column('manifest_object_key', sa.String(length=255), nullable=True),
            sa.Column('manifest_sha256', sa.String(length=64), nullable=True),
            sa.Column('date_min', sa.Date(), nullable=True),
            sa.Column('date_max', sa.Date(), nullable=True),
            sa.Column('row_count', sa.Integer(), nullable=True),
            sa.Column('store_count', sa.Integer(), nullable=True),
            sa.Column('stores', JSON, nullable=False),
            sa.Column('validation_report', JSON, nullable=False),
            sa.Column('created_at', sa.DateTime(timezone=True), nullable=False),
            sa.Column('created_by_id', sa.Uuid(), nullable=True),
            sa.Column('created_by_email', sa.String(length=320), nullable=True),
            sa.Column('sealed_at', sa.DateTime(timezone=True), nullable=True),
            sa.ForeignKeyConstraint(['base_dataset_id'], ['training_datasets.id'], ondelete='RESTRICT'),
            sa.ForeignKeyConstraint(['created_by_id'], ['users.id'], ondelete='SET NULL'),
            sa.PrimaryKeyConstraint('id'),
            sa.UniqueConstraint('name', 'version', name='uq_training_dataset_name_version'),
        )
    _create_index('ix_training_datasets_name', 'training_datasets', ['name'])
    _create_index('ix_training_datasets_status', 'training_datasets', ['status'])
    _create_index('ix_training_datasets_raw_sha256', 'training_datasets', ['raw_sha256'])
    _create_index('ix_training_datasets_content_sha256', 'training_datasets', ['content_sha256'])

    if 'training_requests' not in tables:
        op.create_table(
            'training_requests',
            sa.Column('id', sa.Uuid(), nullable=False),
            sa.Column('idempotency_key', sa.String(length=64), nullable=False),
            sa.Column('dataset_id', sa.Uuid(), nullable=False),
            sa.Column('dataset_name', sa.String(length=80), nullable=False),
            sa.Column('dataset_version', sa.Integer(), nullable=False),
            sa.Column('dataset_content_sha256', sa.String(length=64), nullable=False),
            sa.Column('training_config_id', sa.String(length=32), nullable=False),
            sa.Column('training_config', JSON, nullable=False),
            sa.Column('deployment_policy', sa.String(length=32), nullable=False),
            sa.Column('dag_id', sa.String(length=128), nullable=False),
            sa.Column('dag_run_id', sa.String(length=128), nullable=False),
            sa.Column('status', sa.String(length=32), nullable=False),
            sa.Column('airflow_state', sa.String(length=32), nullable=True),
            sa.Column('task_states', JSON, nullable=False),
            sa.Column('failure_reason', sa.Text(), nullable=True),
            sa.Column('submitted_at', sa.DateTime(timezone=True), nullable=True),
            sa.Column('started_at', sa.DateTime(timezone=True), nullable=True),
            sa.Column('finished_at', sa.DateTime(timezone=True), nullable=True),
            sa.Column('last_synced_at', sa.DateTime(timezone=True), nullable=True),
            sa.Column('mlflow_run_id', sa.String(length=64), nullable=True),
            sa.Column('candidates', JSON, nullable=False),
            sa.Column('evaluation', JSON, nullable=False),
            sa.Column('deployment_state', sa.String(length=32), nullable=False),
            sa.Column('deployed_model', sa.String(length=32), nullable=True),
            sa.Column('deployed_version', sa.String(length=32), nullable=True),
            sa.Column('created_at', sa.DateTime(timezone=True), nullable=False),
            sa.Column('created_by_id', sa.Uuid(), nullable=True),
            sa.Column('created_by_email', sa.String(length=320), nullable=True),
            sa.Column('created_by_role', sa.String(length=32), nullable=True),
            sa.ForeignKeyConstraint(['dataset_id'], ['training_datasets.id'], ondelete='RESTRICT'),
            sa.ForeignKeyConstraint(['created_by_id'], ['users.id'], ondelete='SET NULL'),
            sa.PrimaryKeyConstraint('id'),
            sa.UniqueConstraint('idempotency_key'),
            sa.UniqueConstraint('dag_run_id'),
        )
    _create_index('ix_training_requests_dataset_id', 'training_requests', ['dataset_id'])
    _create_index('ix_training_requests_status', 'training_requests', ['status'])
    _create_index('ix_training_requests_created_at', 'training_requests', ['created_at'])
    _create_index('ix_training_requests_mlflow_run_id', 'training_requests', ['mlflow_run_id'])
    _create_index('ix_training_requests_status_created', 'training_requests', ['status', 'created_at'])

    if 'deployment_decisions' not in tables:
        op.create_table(
            'deployment_decisions',
            sa.Column('id', sa.Uuid(), nullable=False),
            sa.Column('training_request_id', sa.Uuid(), nullable=False),
            sa.Column('decision', sa.String(length=16), nullable=False),
            sa.Column('model', sa.String(length=32), nullable=False),
            sa.Column('version', sa.String(length=32), nullable=False),
            sa.Column('reason', sa.Text(), nullable=False),
            sa.Column('previous_production_version', sa.String(length=32), nullable=True),
            sa.Column('checks', JSON, nullable=False),
            sa.Column('decided_at', sa.DateTime(timezone=True), nullable=False),
            sa.Column('decided_by_id', sa.Uuid(), nullable=True),
            sa.Column('decided_by_email', sa.String(length=320), nullable=True),
            sa.Column('decided_by_role', sa.String(length=32), nullable=True),
            sa.ForeignKeyConstraint(['training_request_id'], ['training_requests.id'], ondelete='CASCADE'),
            sa.ForeignKeyConstraint(['decided_by_id'], ['users.id'], ondelete='SET NULL'),
            sa.PrimaryKeyConstraint('id'),
        )
    _create_index('ix_deployment_decisions_training_request_id', 'deployment_decisions', ['training_request_id'])


def downgrade() -> None:
    tables = _tables()
    for table in ('deployment_decisions', 'training_requests', 'training_datasets'):
        if table in tables:
            op.drop_table(table)
