"""
Persistence layer: SQLAlchemy models and repositories.

The declarative ``Base`` currently lives in
``api.services.forecast_checkpoint_service`` because that module defined the
first table. Every model here imports it so all tables share one metadata
registry and a single ``create_all`` builds the whole schema. Moving ``Base``
into ``api/db/base.py`` is a worthwhile follow-up.
"""
