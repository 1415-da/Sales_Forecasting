"""Initial schema: users, sessions, audit, forecasts, scenarios, monitoring.

Creates all thirteen application tables and their indexes.

Written to be **idempotent**. The API also calls ``create_all()`` at start-up so
a fresh local database works without a migration step, which means this
migration will often run against a database where some or all of these objects
already exist. A bare ``op.create_table`` or ``op.create_index`` fails with
"already exists" in exactly that case, so every create is guarded by an
existence check against the live schema.

``downgrade`` drops the tables. It exists so the migration is reversible in
development; dropping the user and audit tables is destructive and is never the
right move in a real deployment.
"""
from __future__ import annotations

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision: str = '196a4d7f9440'
down_revision: Union[str, None] = None
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def _inspector():
    """Inspector over the connection this migration is running on."""
    return sa.inspect(op.get_bind())


def _tables() -> set:
    """Table names already present in the target schema."""
    return set(_inspector().get_table_names())


def _create_table(name: str, *columns, **kwargs) -> None:
    """Create a table unless it already exists."""
    if name in _tables():
        return
    op.create_table(name, *columns, **kwargs)


def _create_index(index_name: str, table_name: str, columns, **kwargs) -> None:
    """
    Create an index unless its table is absent or the index already exists.

    Both guards matter: a table created by ``create_all`` already carries its
    indexes, and an index name is unique per schema on PostgreSQL, so a second
    CREATE INDEX is an error rather than a no-op.
    """
    if table_name not in _tables():
        return
    if index_name in {item["name"] for item in _inspector().get_indexes(table_name)}:
        return
    op.create_index(index_name, table_name, columns, **kwargs)


def _drop_index(index_name: str, table_name: str) -> None:
    """Drop an index, tolerating its absence."""
    if table_name not in _tables():
        return
    if index_name in {item["name"] for item in _inspector().get_indexes(table_name)}:
        op.drop_index(index_name, table_name=table_name)


def _drop_table(name: str) -> None:
    """Drop a table, tolerating its absence."""
    if name in _tables():
        op.drop_table(name)


def upgrade() -> None:
    _create_table('drift_metrics',
    sa.Column('id', sa.Uuid(), nullable=False),
    sa.Column('model_version', sa.String(length=64), nullable=False),
    sa.Column('feature_name', sa.String(length=128), nullable=False),
    sa.Column('drift_method', sa.String(length=32), nullable=False),
    sa.Column('drift_score', sa.Float(), nullable=False),
    sa.Column('threshold', sa.Float(), nullable=False),
    sa.Column('status', sa.String(length=32), nullable=False),
    sa.Column('reference_size', sa.Integer(), nullable=True),
    sa.Column('current_size', sa.Integer(), nullable=True),
    sa.Column('evaluation_date', sa.Date(), nullable=False),
    sa.Column('created_at', sa.DateTime(timezone=True), nullable=False),
    sa.PrimaryKeyConstraint('id'),
    sa.UniqueConstraint('model_version', 'feature_name', 'drift_method', 'evaluation_date', name='uq_drift_metric_evaluation')
    )
    _create_index(op.f('ix_drift_metrics_evaluation_date'), 'drift_metrics', ['evaluation_date'], unique=False)
    _create_index(op.f('ix_drift_metrics_feature_name'), 'drift_metrics', ['feature_name'], unique=False)
    _create_index(op.f('ix_drift_metrics_model_version'), 'drift_metrics', ['model_version'], unique=False)
    _create_index(op.f('ix_drift_metrics_status'), 'drift_metrics', ['status'], unique=False)
    _create_index('ix_drift_metrics_version_date', 'drift_metrics', ['model_version', 'evaluation_date'], unique=False)
    _create_table('monitoring_metrics',
    sa.Column('id', sa.Uuid(), nullable=False),
    sa.Column('model_version', sa.String(length=64), nullable=False),
    sa.Column('model_name', sa.String(length=64), nullable=True),
    sa.Column('store_id', sa.String(length=128), nullable=True),
    sa.Column('metric_name', sa.String(length=64), nullable=False),
    sa.Column('metric_value', sa.Float(), nullable=False),
    sa.Column('sample_size', sa.Integer(), nullable=True),
    sa.Column('evaluation_date', sa.Date(), nullable=False),
    sa.Column('window_start', sa.Date(), nullable=False),
    sa.Column('window_end', sa.Date(), nullable=False),
    sa.Column('created_at', sa.DateTime(timezone=True), nullable=False),
    sa.PrimaryKeyConstraint('id'),
    sa.UniqueConstraint('model_version', 'store_id', 'metric_name', 'evaluation_date', 'window_start', 'window_end', name='uq_monitoring_metric_window')
    )
    _create_index(op.f('ix_monitoring_metrics_evaluation_date'), 'monitoring_metrics', ['evaluation_date'], unique=False)
    _create_index(op.f('ix_monitoring_metrics_metric_name'), 'monitoring_metrics', ['metric_name'], unique=False)
    _create_index(op.f('ix_monitoring_metrics_model_version'), 'monitoring_metrics', ['model_version'], unique=False)
    _create_index('ix_monitoring_metrics_name_date', 'monitoring_metrics', ['metric_name', 'evaluation_date'], unique=False)
    _create_index(op.f('ix_monitoring_metrics_store_id'), 'monitoring_metrics', ['store_id'], unique=False)
    _create_index('ix_monitoring_metrics_version_date', 'monitoring_metrics', ['model_version', 'evaluation_date'], unique=False)
    _create_table('scenario_audit_events',
    sa.Column('id', sa.Uuid(), nullable=False),
    sa.Column('request_id', sa.String(length=64), nullable=False),
    sa.Column('scenario_id', sa.String(length=64), nullable=True),
    sa.Column('baseline_forecast_id', sa.String(length=64), nullable=True),
    sa.Column('store_id', sa.String(length=128), nullable=False),
    sa.Column('model_version', sa.String(length=64), nullable=False),
    sa.Column('scenario_type', sa.String(length=32), nullable=False),
    sa.Column('status', sa.String(length=32), nullable=False),
    sa.Column('error_code', sa.String(length=64), nullable=True),
    sa.Column('error_message', sa.Text(), nullable=True),
    sa.Column('request_payload', sa.JSON().with_variant(postgresql.JSONB(astext_type=sa.Text()), 'postgresql'), nullable=False),
    sa.Column('response_summary', sa.JSON().with_variant(postgresql.JSONB(astext_type=sa.Text()), 'postgresql'), nullable=True),
    sa.Column('duration_ms', sa.Integer(), nullable=True),
    sa.Column('created_at', sa.DateTime(timezone=True), nullable=False),
    sa.Column('idempotency_key', sa.String(length=255), nullable=True),
    sa.Column('request_hash', sa.String(length=64), nullable=True),
    sa.PrimaryKeyConstraint('id')
    )
    _create_index(op.f('ix_scenario_audit_events_idempotency_key'), 'scenario_audit_events', ['idempotency_key'], unique=False)
    _create_index(op.f('ix_scenario_audit_events_request_id'), 'scenario_audit_events', ['request_id'], unique=False)
    _create_index(op.f('ix_scenario_audit_events_scenario_id'), 'scenario_audit_events', ['scenario_id'], unique=False)
    _create_index(op.f('ix_scenario_audit_events_status'), 'scenario_audit_events', ['status'], unique=False)
    _create_index(op.f('ix_scenario_audit_events_store_id'), 'scenario_audit_events', ['store_id'], unique=False)
    _create_table('scenario_baseline_forecasts',
    sa.Column('id', sa.Uuid(), nullable=False),
    sa.Column('forecast_id', sa.String(length=64), nullable=False),
    sa.Column('store_id', sa.String(length=128), nullable=False),
    sa.Column('model_version', sa.String(length=64), nullable=False),
    sa.Column('model_stage', sa.String(length=32), nullable=False),
    sa.Column('horizon_days', sa.Integer(), nullable=False),
    sa.Column('history_start_date', sa.Date(), nullable=False),
    sa.Column('history_end_date', sa.Date(), nullable=False),
    sa.Column('forecast_start_date', sa.Date(), nullable=False),
    sa.Column('forecast_end_date', sa.Date(), nullable=False),
    sa.Column('data_source', sa.String(length=64), nullable=False),
    sa.Column('dataset_version', sa.String(length=64), nullable=False),
    sa.Column('feature_schema_version', sa.String(length=32), nullable=False),
    sa.Column('status', sa.String(length=16), nullable=False),
    sa.Column('created_at', sa.DateTime(timezone=True), nullable=False),
    sa.Column('expires_at', sa.DateTime(timezone=True), nullable=True),
    sa.Column('error_code', sa.String(length=64), nullable=True),
    sa.Column('error_message', sa.String(length=2000), nullable=True),
    sa.PrimaryKeyConstraint('id')
    )
    _create_index(op.f('ix_scenario_baseline_forecasts_forecast_id'), 'scenario_baseline_forecasts', ['forecast_id'], unique=True)
    _create_index(op.f('ix_scenario_baseline_forecasts_status'), 'scenario_baseline_forecasts', ['status'], unique=False)
    _create_index(op.f('ix_scenario_baseline_forecasts_store_id'), 'scenario_baseline_forecasts', ['store_id'], unique=False)
    _create_table('users',
    sa.Column('id', sa.Uuid(), nullable=False),
    sa.Column('email', sa.String(length=320), nullable=False),
    sa.Column('full_name', sa.String(length=200), nullable=False),
    sa.Column('hashed_password', sa.String(length=255), nullable=False),
    sa.Column('role', sa.String(length=32), nullable=False),
    sa.Column('is_active', sa.Boolean(), nullable=False),
    sa.Column('must_change_password', sa.Boolean(), nullable=False),
    sa.Column('failed_login_count', sa.Integer(), nullable=False),
    sa.Column('locked_until', sa.DateTime(timezone=True), nullable=True),
    sa.Column('created_at', sa.DateTime(timezone=True), nullable=False),
    sa.Column('updated_at', sa.DateTime(timezone=True), nullable=False),
    sa.Column('last_login_at', sa.DateTime(timezone=True), nullable=True),
    sa.Column('created_by_id', sa.Uuid(), nullable=True),
    sa.ForeignKeyConstraint(['created_by_id'], ['users.id'], ondelete='SET NULL'),
    sa.PrimaryKeyConstraint('id')
    )
    _create_index('ix_users_created_at', 'users', ['created_at'], unique=False)
    _create_index(op.f('ix_users_email'), 'users', ['email'], unique=True)
    _create_index(op.f('ix_users_is_active'), 'users', ['is_active'], unique=False)
    _create_index(op.f('ix_users_role'), 'users', ['role'], unique=False)
    _create_index('ix_users_role_active', 'users', ['role', 'is_active'], unique=False)
    _create_table('actuals',
    sa.Column('id', sa.Uuid(), nullable=False),
    sa.Column('store_id', sa.String(length=128), nullable=False),
    sa.Column('date', sa.Date(), nullable=False),
    sa.Column('actual_sales', sa.Float(), nullable=False),
    sa.Column('source', sa.String(length=64), nullable=False),
    sa.Column('created_at', sa.DateTime(timezone=True), nullable=False),
    sa.Column('updated_at', sa.DateTime(timezone=True), nullable=False),
    sa.Column('created_by_id', sa.Uuid(), nullable=True),
    sa.ForeignKeyConstraint(['created_by_id'], ['users.id'], ondelete='SET NULL'),
    sa.PrimaryKeyConstraint('id'),
    sa.UniqueConstraint('store_id', 'date', name='uq_actuals_store_date')
    )
    _create_index(op.f('ix_actuals_date'), 'actuals', ['date'], unique=False)
    _create_index('ix_actuals_store_date', 'actuals', ['store_id', 'date'], unique=False)
    _create_index(op.f('ix_actuals_store_id'), 'actuals', ['store_id'], unique=False)
    _create_table('alerts',
    sa.Column('id', sa.Uuid(), nullable=False),
    sa.Column('model_version', sa.String(length=64), nullable=True),
    sa.Column('store_id', sa.String(length=128), nullable=True),
    sa.Column('severity', sa.String(length=16), nullable=False),
    sa.Column('alert_type', sa.String(length=64), nullable=False),
    sa.Column('title', sa.String(length=255), nullable=False),
    sa.Column('message', sa.Text(), nullable=False),
    sa.Column('recommended_action', sa.Text(), nullable=True),
    sa.Column('status', sa.String(length=16), nullable=False),
    sa.Column('fingerprint', sa.String(length=128), nullable=False),
    sa.Column('occurrence_count', sa.Integer(), nullable=False),
    sa.Column('details', sa.JSON().with_variant(postgresql.JSONB(astext_type=sa.Text()), 'postgresql'), nullable=False),
    sa.Column('created_at', sa.DateTime(timezone=True), nullable=False),
    sa.Column('updated_at', sa.DateTime(timezone=True), nullable=False),
    sa.Column('last_seen_at', sa.DateTime(timezone=True), nullable=False),
    sa.Column('acknowledged_at', sa.DateTime(timezone=True), nullable=True),
    sa.Column('acknowledged_by_id', sa.Uuid(), nullable=True),
    sa.Column('resolved_at', sa.DateTime(timezone=True), nullable=True),
    sa.Column('resolved_by_id', sa.Uuid(), nullable=True),
    sa.Column('resolution_note', sa.Text(), nullable=True),
    sa.ForeignKeyConstraint(['acknowledged_by_id'], ['users.id'], ondelete='SET NULL'),
    sa.ForeignKeyConstraint(['resolved_by_id'], ['users.id'], ondelete='SET NULL'),
    sa.PrimaryKeyConstraint('id')
    )
    _create_index(op.f('ix_alerts_alert_type'), 'alerts', ['alert_type'], unique=False)
    _create_index(op.f('ix_alerts_created_at'), 'alerts', ['created_at'], unique=False)
    _create_index(op.f('ix_alerts_fingerprint'), 'alerts', ['fingerprint'], unique=False)
    _create_index('ix_alerts_fingerprint_status', 'alerts', ['fingerprint', 'status'], unique=False)
    _create_index(op.f('ix_alerts_model_version'), 'alerts', ['model_version'], unique=False)
    _create_index(op.f('ix_alerts_severity'), 'alerts', ['severity'], unique=False)
    _create_index(op.f('ix_alerts_status'), 'alerts', ['status'], unique=False)
    _create_index('ix_alerts_status_created', 'alerts', ['status', 'created_at'], unique=False)
    _create_index(op.f('ix_alerts_store_id'), 'alerts', ['store_id'], unique=False)
    _create_table('audit_logs',
    sa.Column('id', sa.Uuid(), nullable=False),
    sa.Column('user_id', sa.Uuid(), nullable=True),
    sa.Column('user_email', sa.String(length=320), nullable=True),
    sa.Column('user_role', sa.String(length=32), nullable=True),
    sa.Column('action', sa.String(length=64), nullable=False),
    sa.Column('resource_type', sa.String(length=32), nullable=False),
    sa.Column('resource_id', sa.String(length=128), nullable=True),
    sa.Column('outcome', sa.String(length=16), nullable=False),
    sa.Column('event_metadata', sa.JSON().with_variant(postgresql.JSONB(astext_type=sa.Text()), 'postgresql'), nullable=False),
    sa.Column('ip_address', sa.String(length=64), nullable=True),
    sa.Column('user_agent', sa.Text(), nullable=True),
    sa.Column('request_id', sa.String(length=128), nullable=True),
    sa.Column('created_at', sa.DateTime(timezone=True), nullable=False),
    sa.ForeignKeyConstraint(['user_id'], ['users.id'], ondelete='SET NULL'),
    sa.PrimaryKeyConstraint('id')
    )
    _create_index(op.f('ix_audit_logs_action'), 'audit_logs', ['action'], unique=False)
    _create_index('ix_audit_logs_action_created', 'audit_logs', ['action', 'created_at'], unique=False)
    _create_index(op.f('ix_audit_logs_created_at'), 'audit_logs', ['created_at'], unique=False)
    _create_index(op.f('ix_audit_logs_request_id'), 'audit_logs', ['request_id'], unique=False)
    _create_index('ix_audit_logs_resource', 'audit_logs', ['resource_type', 'resource_id', 'created_at'], unique=False)
    _create_index(op.f('ix_audit_logs_resource_id'), 'audit_logs', ['resource_id'], unique=False)
    _create_index(op.f('ix_audit_logs_resource_type'), 'audit_logs', ['resource_type'], unique=False)
    _create_index('ix_audit_logs_user_created', 'audit_logs', ['user_id', 'created_at'], unique=False)
    _create_index(op.f('ix_audit_logs_user_email'), 'audit_logs', ['user_email'], unique=False)
    _create_index(op.f('ix_audit_logs_user_id'), 'audit_logs', ['user_id'], unique=False)
    _create_table('forecast_requests',
    sa.Column('id', sa.Uuid(), nullable=False),
    sa.Column('forecast_id', sa.String(length=64), nullable=False),
    sa.Column('store_id', sa.String(length=128), nullable=False),
    sa.Column('model_name', sa.String(length=64), nullable=False),
    sa.Column('model_version', sa.String(length=64), nullable=False),
    sa.Column('model_stage', sa.String(length=32), nullable=False),
    sa.Column('run_id', sa.String(length=64), nullable=True),
    sa.Column('horizon_days', sa.Integer(), nullable=False),
    sa.Column('confidence_level', sa.Float(), nullable=False),
    sa.Column('history_start_date', sa.Date(), nullable=False),
    sa.Column('history_end_date', sa.Date(), nullable=False),
    sa.Column('history_days', sa.Integer(), nullable=False),
    sa.Column('forecast_metadata', sa.JSON().with_variant(postgresql.JSONB(astext_type=sa.Text()), 'postgresql'), nullable=False),
    sa.Column('created_at', sa.DateTime(timezone=True), nullable=False),
    sa.Column('created_by_id', sa.Uuid(), nullable=True),
    sa.Column('created_by_email', sa.String(length=320), nullable=True),
    sa.ForeignKeyConstraint(['created_by_id'], ['users.id'], ondelete='SET NULL'),
    sa.PrimaryKeyConstraint('id')
    )
    _create_index(op.f('ix_forecast_requests_created_at'), 'forecast_requests', ['created_at'], unique=False)
    _create_index(op.f('ix_forecast_requests_created_by_id'), 'forecast_requests', ['created_by_id'], unique=False)
    _create_index(op.f('ix_forecast_requests_forecast_id'), 'forecast_requests', ['forecast_id'], unique=True)
    _create_index(op.f('ix_forecast_requests_model_version'), 'forecast_requests', ['model_version'], unique=False)
    _create_index('ix_forecast_requests_store_created', 'forecast_requests', ['store_id', 'created_at'], unique=False)
    _create_index(op.f('ix_forecast_requests_store_id'), 'forecast_requests', ['store_id'], unique=False)
    _create_index('ix_forecast_requests_version_created', 'forecast_requests', ['model_version', 'created_at'], unique=False)
    _create_table('refresh_tokens',
    sa.Column('id', sa.Uuid(), nullable=False),
    sa.Column('jti', sa.String(length=64), nullable=False),
    sa.Column('token_hash', sa.String(length=64), nullable=False),
    sa.Column('user_id', sa.Uuid(), nullable=False),
    sa.Column('expires_at', sa.DateTime(timezone=True), nullable=False),
    sa.Column('created_at', sa.DateTime(timezone=True), nullable=False),
    sa.Column('revoked_at', sa.DateTime(timezone=True), nullable=True),
    sa.Column('revoked_reason', sa.String(length=64), nullable=True),
    sa.Column('replaced_by_jti', sa.String(length=64), nullable=True),
    sa.Column('user_agent', sa.Text(), nullable=True),
    sa.Column('ip_address', sa.String(length=64), nullable=True),
    sa.ForeignKeyConstraint(['user_id'], ['users.id'], ondelete='CASCADE'),
    sa.PrimaryKeyConstraint('id')
    )
    _create_index('ix_refresh_tokens_expires_at', 'refresh_tokens', ['expires_at'], unique=False)
    _create_index(op.f('ix_refresh_tokens_jti'), 'refresh_tokens', ['jti'], unique=True)
    _create_index(op.f('ix_refresh_tokens_token_hash'), 'refresh_tokens', ['token_hash'], unique=True)
    _create_index('ix_refresh_tokens_user_active', 'refresh_tokens', ['user_id', 'revoked_at'], unique=False)
    _create_index(op.f('ix_refresh_tokens_user_id'), 'refresh_tokens', ['user_id'], unique=False)
    _create_table('scenario_forecasts',
    sa.Column('id', sa.Uuid(), nullable=False),
    sa.Column('scenario_id', sa.String(length=64), nullable=False),
    sa.Column('baseline_forecast_id', sa.String(length=64), nullable=True),
    sa.Column('store_id', sa.String(length=128), nullable=False),
    sa.Column('model_version', sa.String(length=64), nullable=False),
    sa.Column('model_stage', sa.String(length=32), nullable=False),
    sa.Column('scenario_type', sa.String(length=32), nullable=False),
    sa.Column('horizon_days', sa.Integer(), nullable=False),
    sa.Column('status', sa.String(length=16), nullable=False),
    sa.Column('forecast_type', sa.String(length=32), nullable=False),
    sa.Column('request_payload', sa.JSON().with_variant(postgresql.JSONB(astext_type=sa.Text()), 'postgresql'), nullable=False),
    sa.Column('response_summary', sa.JSON().with_variant(postgresql.JSONB(astext_type=sa.Text()), 'postgresql'), nullable=True),
    sa.Column('created_at', sa.DateTime(timezone=True), nullable=False),
    sa.Column('completed_at', sa.DateTime(timezone=True), nullable=True),
    sa.Column('expires_at', sa.DateTime(timezone=True), nullable=True),
    sa.Column('error_code', sa.String(length=64), nullable=True),
    sa.Column('error_message', sa.Text(), nullable=True),
    sa.Column('created_by_id', sa.Uuid(), nullable=True),
    sa.Column('created_by_email', sa.String(length=320), nullable=True),
    sa.ForeignKeyConstraint(['created_by_id'], ['users.id'], ondelete='SET NULL'),
    sa.PrimaryKeyConstraint('id')
    )
    _create_index(op.f('ix_scenario_forecasts_created_by_id'), 'scenario_forecasts', ['created_by_id'], unique=False)
    _create_index('ix_scenario_forecasts_creator_created', 'scenario_forecasts', ['created_by_id', 'created_at'], unique=False)
    _create_index(op.f('ix_scenario_forecasts_expires_at'), 'scenario_forecasts', ['expires_at'], unique=False)
    _create_index(op.f('ix_scenario_forecasts_scenario_id'), 'scenario_forecasts', ['scenario_id'], unique=True)
    _create_index(op.f('ix_scenario_forecasts_status'), 'scenario_forecasts', ['status'], unique=False)
    _create_index('ix_scenario_forecasts_status_expires', 'scenario_forecasts', ['status', 'expires_at'], unique=False)
    _create_index('ix_scenario_forecasts_store_created', 'scenario_forecasts', ['store_id', 'created_at'], unique=False)
    _create_index(op.f('ix_scenario_forecasts_store_id'), 'scenario_forecasts', ['store_id'], unique=False)
    _create_table('forecasts',
    sa.Column('id', sa.Uuid(), nullable=False),
    sa.Column('forecast_request_id', sa.Uuid(), nullable=False),
    sa.Column('store_id', sa.String(length=128), nullable=False),
    sa.Column('model_name', sa.String(length=64), nullable=False),
    sa.Column('model_version', sa.String(length=64), nullable=False),
    sa.Column('forecast_date', sa.Date(), nullable=False),
    sa.Column('horizon_step', sa.Integer(), nullable=False),
    sa.Column('predicted_sales', sa.Float(), nullable=False),
    sa.Column('lower_bound', sa.Float(), nullable=True),
    sa.Column('upper_bound', sa.Float(), nullable=True),
    sa.Column('confidence_level', sa.Float(), nullable=True),
    sa.Column('created_at', sa.DateTime(timezone=True), nullable=False),
    sa.Column('created_by_id', sa.Uuid(), nullable=True),
    sa.ForeignKeyConstraint(['created_by_id'], ['users.id'], ondelete='SET NULL'),
    sa.ForeignKeyConstraint(['forecast_request_id'], ['forecast_requests.id'], ondelete='CASCADE'),
    sa.PrimaryKeyConstraint('id')
    )
    _create_index('ix_forecasts_date_created', 'forecasts', ['forecast_date', 'created_at'], unique=False)
    _create_index(op.f('ix_forecasts_forecast_date'), 'forecasts', ['forecast_date'], unique=False)
    _create_index(op.f('ix_forecasts_forecast_request_id'), 'forecasts', ['forecast_request_id'], unique=False)
    _create_index(op.f('ix_forecasts_model_version'), 'forecasts', ['model_version'], unique=False)
    _create_index('ix_forecasts_store_date', 'forecasts', ['store_id', 'forecast_date'], unique=False)
    _create_index(op.f('ix_forecasts_store_id'), 'forecasts', ['store_id'], unique=False)
    _create_index('ix_forecasts_version_date', 'forecasts', ['model_version', 'forecast_date'], unique=False)
    _create_table('scenario_daily_forecasts',
    sa.Column('id', sa.Uuid(), nullable=False),
    sa.Column('scenario_id', sa.String(length=64), nullable=False),
    sa.Column('forecast_date', sa.Date(), nullable=False),
    sa.Column('baseline_p10', sa.Float(), nullable=True),
    sa.Column('baseline_p50', sa.Float(), nullable=True),
    sa.Column('baseline_p90', sa.Float(), nullable=True),
    sa.Column('scenario_p10', sa.Float(), nullable=True),
    sa.Column('scenario_p50', sa.Float(), nullable=True),
    sa.Column('scenario_p90', sa.Float(), nullable=True),
    sa.Column('difference_p50', sa.Float(), nullable=True),
    sa.Column('difference_pct_p50', sa.Float(), nullable=True),
    sa.Column('created_at', sa.DateTime(timezone=True), nullable=False),
    sa.ForeignKeyConstraint(['scenario_id'], ['scenario_forecasts.scenario_id'], ),
    sa.PrimaryKeyConstraint('id')
    )
    _create_index(op.f('ix_scenario_daily_forecasts_scenario_id'), 'scenario_daily_forecasts', ['scenario_id'], unique=False)
    _create_index('ux_scenario_daily_forecasts_scenario_date', 'scenario_daily_forecasts', ['scenario_id', 'forecast_date'], unique=True)


def downgrade() -> None:
    _drop_index('ux_scenario_daily_forecasts_scenario_date', 'scenario_daily_forecasts')
    _drop_index(op.f('ix_scenario_daily_forecasts_scenario_id'), 'scenario_daily_forecasts')
    _drop_table('scenario_daily_forecasts')
    _drop_index('ix_forecasts_version_date', 'forecasts')
    _drop_index(op.f('ix_forecasts_store_id'), 'forecasts')
    _drop_index('ix_forecasts_store_date', 'forecasts')
    _drop_index(op.f('ix_forecasts_model_version'), 'forecasts')
    _drop_index(op.f('ix_forecasts_forecast_request_id'), 'forecasts')
    _drop_index(op.f('ix_forecasts_forecast_date'), 'forecasts')
    _drop_index('ix_forecasts_date_created', 'forecasts')
    _drop_table('forecasts')
    _drop_index(op.f('ix_scenario_forecasts_store_id'), 'scenario_forecasts')
    _drop_index('ix_scenario_forecasts_store_created', 'scenario_forecasts')
    _drop_index('ix_scenario_forecasts_status_expires', 'scenario_forecasts')
    _drop_index(op.f('ix_scenario_forecasts_status'), 'scenario_forecasts')
    _drop_index(op.f('ix_scenario_forecasts_scenario_id'), 'scenario_forecasts')
    _drop_index(op.f('ix_scenario_forecasts_expires_at'), 'scenario_forecasts')
    _drop_index('ix_scenario_forecasts_creator_created', 'scenario_forecasts')
    _drop_index(op.f('ix_scenario_forecasts_created_by_id'), 'scenario_forecasts')
    _drop_table('scenario_forecasts')
    _drop_index(op.f('ix_refresh_tokens_user_id'), 'refresh_tokens')
    _drop_index('ix_refresh_tokens_user_active', 'refresh_tokens')
    _drop_index(op.f('ix_refresh_tokens_token_hash'), 'refresh_tokens')
    _drop_index(op.f('ix_refresh_tokens_jti'), 'refresh_tokens')
    _drop_index('ix_refresh_tokens_expires_at', 'refresh_tokens')
    _drop_table('refresh_tokens')
    _drop_index('ix_forecast_requests_version_created', 'forecast_requests')
    _drop_index(op.f('ix_forecast_requests_store_id'), 'forecast_requests')
    _drop_index('ix_forecast_requests_store_created', 'forecast_requests')
    _drop_index(op.f('ix_forecast_requests_model_version'), 'forecast_requests')
    _drop_index(op.f('ix_forecast_requests_forecast_id'), 'forecast_requests')
    _drop_index(op.f('ix_forecast_requests_created_by_id'), 'forecast_requests')
    _drop_index(op.f('ix_forecast_requests_created_at'), 'forecast_requests')
    _drop_table('forecast_requests')
    _drop_index(op.f('ix_audit_logs_user_id'), 'audit_logs')
    _drop_index(op.f('ix_audit_logs_user_email'), 'audit_logs')
    _drop_index('ix_audit_logs_user_created', 'audit_logs')
    _drop_index(op.f('ix_audit_logs_resource_type'), 'audit_logs')
    _drop_index(op.f('ix_audit_logs_resource_id'), 'audit_logs')
    _drop_index('ix_audit_logs_resource', 'audit_logs')
    _drop_index(op.f('ix_audit_logs_request_id'), 'audit_logs')
    _drop_index(op.f('ix_audit_logs_created_at'), 'audit_logs')
    _drop_index('ix_audit_logs_action_created', 'audit_logs')
    _drop_index(op.f('ix_audit_logs_action'), 'audit_logs')
    _drop_table('audit_logs')
    _drop_index(op.f('ix_alerts_store_id'), 'alerts')
    _drop_index('ix_alerts_status_created', 'alerts')
    _drop_index(op.f('ix_alerts_status'), 'alerts')
    _drop_index(op.f('ix_alerts_severity'), 'alerts')
    _drop_index(op.f('ix_alerts_model_version'), 'alerts')
    _drop_index('ix_alerts_fingerprint_status', 'alerts')
    _drop_index(op.f('ix_alerts_fingerprint'), 'alerts')
    _drop_index(op.f('ix_alerts_created_at'), 'alerts')
    _drop_index(op.f('ix_alerts_alert_type'), 'alerts')
    _drop_table('alerts')
    _drop_index(op.f('ix_actuals_store_id'), 'actuals')
    _drop_index('ix_actuals_store_date', 'actuals')
    _drop_index(op.f('ix_actuals_date'), 'actuals')
    _drop_table('actuals')
    _drop_index('ix_users_role_active', 'users')
    _drop_index(op.f('ix_users_role'), 'users')
    _drop_index(op.f('ix_users_is_active'), 'users')
    _drop_index(op.f('ix_users_email'), 'users')
    _drop_index('ix_users_created_at', 'users')
    _drop_table('users')
    _drop_index(op.f('ix_scenario_baseline_forecasts_store_id'), 'scenario_baseline_forecasts')
    _drop_index(op.f('ix_scenario_baseline_forecasts_status'), 'scenario_baseline_forecasts')
    _drop_index(op.f('ix_scenario_baseline_forecasts_forecast_id'), 'scenario_baseline_forecasts')
    _drop_table('scenario_baseline_forecasts')
    _drop_index(op.f('ix_scenario_audit_events_store_id'), 'scenario_audit_events')
    _drop_index(op.f('ix_scenario_audit_events_status'), 'scenario_audit_events')
    _drop_index(op.f('ix_scenario_audit_events_scenario_id'), 'scenario_audit_events')
    _drop_index(op.f('ix_scenario_audit_events_request_id'), 'scenario_audit_events')
    _drop_index(op.f('ix_scenario_audit_events_idempotency_key'), 'scenario_audit_events')
    _drop_table('scenario_audit_events')
    _drop_index('ix_monitoring_metrics_version_date', 'monitoring_metrics')
    _drop_index(op.f('ix_monitoring_metrics_store_id'), 'monitoring_metrics')
    _drop_index('ix_monitoring_metrics_name_date', 'monitoring_metrics')
    _drop_index(op.f('ix_monitoring_metrics_model_version'), 'monitoring_metrics')
    _drop_index(op.f('ix_monitoring_metrics_metric_name'), 'monitoring_metrics')
    _drop_index(op.f('ix_monitoring_metrics_evaluation_date'), 'monitoring_metrics')
    _drop_table('monitoring_metrics')
    _drop_index('ix_drift_metrics_version_date', 'drift_metrics')
    _drop_index(op.f('ix_drift_metrics_status'), 'drift_metrics')
    _drop_index(op.f('ix_drift_metrics_model_version'), 'drift_metrics')
    _drop_index(op.f('ix_drift_metrics_feature_name'), 'drift_metrics')
    _drop_index(op.f('ix_drift_metrics_evaluation_date'), 'drift_metrics')
    _drop_table('drift_metrics')
