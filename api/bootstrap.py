"""
Application start-up tasks.

Two things, both of which must be safe to run on every boot and on every
replica:

1. ensure the schema exists, so a fresh local database works without a
   migration step;
2. create the first ADMIN account when, and only when, no account exists.

Neither may crash the process. A database that is not ready yet is reported by
``/health`` as unavailable, which is more useful than a container that restarts
in a loop and never serves the health endpoint at all.
"""

from __future__ import annotations

import logging
import os

from api.db.session import ensure_tables_once, session_scope
from api.services.auth_service import AuthService

logger = logging.getLogger(__name__)

BANNER = "=" * 72


def bootstrap_database() -> None:
    """Create any missing tables. Safe to call repeatedly."""
    ensure_tables_once()


def bootstrap_admin_user() -> None:
    """
    Create the initial ADMIN account if the user table is empty.

    When no ``BOOTSTRAP_ADMIN_PASSWORD`` is configured a strong one is
    generated and written to the log **once**, at WARNING, framed so it is hard
    to miss. That is deliberate: the alternative is a well-known default
    password, which is worse than a one-time secret in a local log.

    Concurrency: two replicas starting together both see an empty table, and
    the second insert fails on the unique email constraint. That is caught and
    logged rather than raised -- the outcome (exactly one admin) is correct
    either way.
    """
    try:
        with session_scope() as session:
            outcome = AuthService(session).bootstrap_admin()
    except Exception as exc:  # noqa: BLE001 - start-up must not fail on this
        logger.warning("Could not run the admin bootstrap: %s", exc)
        return

    if outcome is None:
        logger.info("Users already exist; skipping the admin bootstrap.")
        return

    user, generated_password = outcome
    if generated_password is None:
        logger.warning(
            "Created the initial ADMIN account %s using BOOTSTRAP_ADMIN_PASSWORD. "
            "A password change is required at first sign-in.",
            user.email,
        )
        return

    logger.warning(
        "\n%s\nINITIAL ADMIN ACCOUNT CREATED\n  email:    %s\n"
        "  password: %s\nThis password is shown once and a change is required at "
        "first sign-in.\nSet BOOTSTRAP_ADMIN_PASSWORD to choose it yourself.\n%s",
        BANNER,
        user.email,
        generated_password,
        BANNER,
    )


def bootstrap_service_accounts() -> None:
    """
    Create the monitoring service account when one is configured.

    Only when ``MONITORING_SERVICE_PASSWORD`` is set: a service account with no
    password would be an account that cannot be used, and creating one by
    default would add a privileged row nobody asked for.
    """
    try:
        with session_scope() as session:
            outcome = AuthService(session).bootstrap_service_account()
    except Exception as exc:  # noqa: BLE001 - start-up must not fail on this
        logger.warning("Could not provision the monitoring service account: %s", exc)
        return

    if outcome is None:
        logger.info(
            "MONITORING_SERVICE_PASSWORD is not set; the monitoring DAG will "
            "have no credentials and its evaluation step will be skipped."
        )
        return

    user, created = outcome
    logger.info(
        "Monitoring service account %s %s",
        user.email,
        "created" if created else "credentials refreshed",
    )


def run_startup_tasks() -> None:
    """Everything that must happen before the first request is served."""
    if _disabled("SKIP_DB_BOOTSTRAP"):
        logger.info("Database bootstrap skipped by configuration.")
        return
    bootstrap_database()
    recover_interrupted_uploads()
    if _disabled("SKIP_ADMIN_BOOTSTRAP"):
        logger.info("Admin bootstrap skipped by configuration.")
        return
    bootstrap_admin_user()
    bootstrap_service_accounts()


def recover_interrupted_uploads() -> None:
    """Training uploads left mid-processing by a restart become retryable failures."""
    try:
        from api.services.ingestion_jobs import recover_interrupted

        recover_interrupted()
    except Exception as exc:  # noqa: BLE001 - start-up must not fail on this
        logger.warning("Could not recover interrupted training uploads: %s", exc)


def _disabled(name: str) -> bool:
    """Whether an opt-out flag is set."""
    return os.getenv(name, "false").strip().lower() in ("1", "true", "yes", "on")


__all__ = [
    "bootstrap_admin_user",
    "bootstrap_database",
    "bootstrap_service_accounts",
    "run_startup_tasks",
]
