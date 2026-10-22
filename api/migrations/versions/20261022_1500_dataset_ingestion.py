"""Training-data ingestion: adapters, mapping, background processing.

Adds upload-session columns to ``training_datasets``: source files and their
hashes, the selected adapter and version, the confirmed mapping and its
version, detection results, source metadata (namespace, target type, grain),
feature policies, the retained-context object, acknowledged warnings, an
idempotency key, and processing timestamps/errors.

Nullable and idempotent: the API's start-up adds the same columns
(``api.db.session.ADDITIVE_COLUMNS``), so this often runs against a database
that already has them.
"""
from __future__ import annotations

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision: str = 'c3e9a1f04b27'
down_revision: Union[str, None] = '8d41c0a7e2f3'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

TABLE = 'training_datasets'
JSON = sa.JSON().with_variant(postgresql.JSONB(astext_type=sa.Text()), 'postgresql')

COLUMNS = [
    ('source_files', JSON),
    ('adapter_id', sa.String(length=64)),
    ('adapter_version', sa.String(length=16)),
    ('ingestion_config', JSON),
    ('mapping_version', sa.Integer()),
    ('detection', JSON),
    ('source_namespace', sa.String(length=64)),
    ('target_type', sa.String(length=16)),
    ('source_grain', sa.String(length=32)),
    ('feature_options', JSON),
    ('context_object_key', sa.String(length=255)),
    ('context_object_sha256', sa.String(length=64)),
    ('acknowledged_warnings', JSON),
    ('upload_key', sa.String(length=64)),
    ('processing_error', sa.Text()),
    ('processing_started_at', sa.DateTime(timezone=True)),
    ('processing_finished_at', sa.DateTime(timezone=True)),
]


def _existing() -> set:
    inspector = sa.inspect(op.get_bind())
    if TABLE not in inspector.get_table_names():
        return set()
    return {column['name'] for column in inspector.get_columns(TABLE)}


def upgrade() -> None:
    existing = _existing()
    if not existing:
        return
    for name, type_ in COLUMNS:
        if name not in existing:
            op.add_column(TABLE, sa.Column(name, type_, nullable=True))
    indexes = {item['name'] for item in sa.inspect(op.get_bind()).get_indexes(TABLE)}
    if 'uq_training_datasets_upload_key' not in indexes:
        op.create_index('uq_training_datasets_upload_key', TABLE, ['upload_key'], unique=True)


def downgrade() -> None:
    existing = _existing()
    indexes = {item['name'] for item in sa.inspect(op.get_bind()).get_indexes(TABLE)} if existing else set()
    if 'uq_training_datasets_upload_key' in indexes:
        op.drop_index('uq_training_datasets_upload_key', table_name=TABLE)
    for name, _ in reversed(COLUMNS):
        if name in existing:
            op.drop_column(TABLE, name)
