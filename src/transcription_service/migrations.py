from __future__ import annotations

import sqlite3
from collections.abc import Callable
from datetime import UTC, datetime


def _columns(connection: sqlite3.Connection, table: str) -> set[str]:
    return {row[1] for row in connection.execute(f"PRAGMA table_info({table})")}  # noqa: S608


def _stage2_schema(connection: sqlite3.Connection) -> None:
    if "role" not in _columns(connection, "users"):
        connection.execute(
            "ALTER TABLE users ADD COLUMN role TEXT NOT NULL DEFAULT 'user' "
            "CHECK(role IN ('user','admin'))"
        )
    if "duration_ms" not in _columns(connection, "jobs"):
        connection.execute("ALTER TABLE jobs ADD COLUMN duration_ms INTEGER")

    connection.executescript(
        """
        CREATE TABLE IF NOT EXISTS attribution_runs (
            id TEXT PRIMARY KEY,
            job_id TEXT NOT NULL REFERENCES jobs(id) ON DELETE CASCADE,
            user_id TEXT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
            status TEXT NOT NULL CHECK(status IN
                ('queued','running','completed','failed','blocked_budget')),
            consent_at TEXT,
            consent_revoked_at TEXT,
            budget_amount TEXT,
            budget_currency TEXT NOT NULL DEFAULT 'RUB',
            profile_snapshot_json TEXT NOT NULL,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL,
            started_at TEXT,
            completed_at TEXT,
            error_code TEXT,
            error_message TEXT
        );
        CREATE UNIQUE INDEX IF NOT EXISTS idx_attribution_one_active
            ON attribution_runs(job_id)
            WHERE status IN ('queued','running');
        CREATE INDEX IF NOT EXISTS idx_attribution_runs_status_created
            ON attribution_runs(status, created_at);

        CREATE TABLE IF NOT EXISTS transcript_segments (
            id TEXT PRIMARY KEY,
            run_id TEXT NOT NULL REFERENCES attribution_runs(id) ON DELETE CASCADE,
            ordinal INTEGER NOT NULL,
            source_anchor TEXT NOT NULL,
            source_label TEXT NOT NULL,
            start_ms INTEGER NOT NULL,
            end_ms INTEGER NOT NULL,
            text TEXT NOT NULL,
            UNIQUE(run_id, ordinal), UNIQUE(run_id, source_anchor)
        );
        CREATE INDEX IF NOT EXISTS idx_segments_run ON transcript_segments(run_id, ordinal);

        CREATE TABLE IF NOT EXISTS evidence_frames (
            id TEXT PRIMARY KEY,
            run_id TEXT NOT NULL REFERENCES attribution_runs(id) ON DELETE CASCADE,
            segment_id TEXT NOT NULL REFERENCES transcript_segments(id) ON DELETE CASCADE,
            requested_ms INTEGER NOT NULL,
            actual_ms INTEGER NOT NULL,
            relative_path TEXT NOT NULL,
            width INTEGER,
            height INTEGER,
            sha256 TEXT NOT NULL
        );
        CREATE INDEX IF NOT EXISTS idx_frames_segment ON evidence_frames(segment_id);

        CREATE TABLE IF NOT EXISTS external_model_calls (
            id TEXT PRIMARY KEY,
            run_id TEXT REFERENCES attribution_runs(id) ON DELETE SET NULL,
            job_id TEXT REFERENCES jobs(id) ON DELETE SET NULL,
            user_id TEXT REFERENCES users(id) ON DELETE SET NULL,
            provider TEXT NOT NULL DEFAULT 'routerai',
            requested_model TEXT NOT NULL,
            actual_model TEXT,
            actual_provider TEXT,
            service_tier TEXT,
            status TEXT NOT NULL,
            attempt INTEGER NOT NULL DEFAULT 1,
            is_retry INTEGER NOT NULL DEFAULT 0,
            image_count INTEGER NOT NULL DEFAULT 0,
            input_units INTEGER,
            output_units INTEGER,
            total_units INTEGER,
            provider_cost TEXT,
            estimated_cost TEXT,
            currency TEXT NOT NULL DEFAULT 'RUB',
            cost_source TEXT NOT NULL DEFAULT 'unavailable',
            generation_id TEXT,
            provider_request_id TEXT,
            created_at TEXT NOT NULL,
            completed_at TEXT,
            error_code TEXT,
            error_message TEXT
        );
        CREATE INDEX IF NOT EXISTS idx_calls_created ON external_model_calls(created_at);
        CREATE INDEX IF NOT EXISTS idx_calls_user_created
            ON external_model_calls(user_id, created_at);
        CREATE INDEX IF NOT EXISTS idx_calls_model_created
            ON external_model_calls(actual_model, created_at);

        CREATE TABLE IF NOT EXISTS segment_attributions (
            segment_id TEXT PRIMARY KEY REFERENCES transcript_segments(id) ON DELETE CASCADE,
            status TEXT NOT NULL,
            speaker_label TEXT,
            confidence REAL,
            highlight_bbox_json TEXT,
            label_bbox_json TEXT,
            reason TEXT,
            invocation_id TEXT REFERENCES external_model_calls(id) ON DELETE SET NULL,
            manual_label TEXT,
            manual_by TEXT REFERENCES users(id) ON DELETE SET NULL,
            manual_at TEXT,
            updated_at TEXT NOT NULL
        );

        CREATE TABLE IF NOT EXISTS provider_credentials (
            provider TEXT PRIMARY KEY,
            ciphertext BLOB NOT NULL,
            key_hint TEXT NOT NULL,
            enabled INTEGER NOT NULL DEFAULT 1,
            last_checked_at TEXT,
            last_check_ok INTEGER,
            updated_at TEXT NOT NULL,
            updated_by TEXT REFERENCES users(id) ON DELETE SET NULL
        );

        CREATE TABLE IF NOT EXISTS system_settings (
            key TEXT PRIMARY KEY,
            value_json TEXT NOT NULL,
            updated_at TEXT NOT NULL,
            updated_by TEXT REFERENCES users(id) ON DELETE SET NULL
        );

        CREATE TABLE IF NOT EXISTS model_catalog_snapshots (
            id TEXT PRIMARY KEY,
            fetched_at TEXT NOT NULL,
            status TEXT NOT NULL,
            error_message TEXT
        );
        CREATE TABLE IF NOT EXISTS model_catalog_entries (
            snapshot_id TEXT NOT NULL REFERENCES model_catalog_snapshots(id) ON DELETE CASCADE,
            model_id TEXT NOT NULL,
            name TEXT NOT NULL,
            author TEXT NOT NULL,
            is_alias INTEGER NOT NULL DEFAULT 0,
            input_modalities_json TEXT NOT NULL,
            output_modalities_json TEXT NOT NULL,
            supported_parameters_json TEXT NOT NULL,
            pricing_json TEXT NOT NULL,
            compatible INTEGER NOT NULL DEFAULT 0,
            PRIMARY KEY(snapshot_id, model_id)
        );
        CREATE INDEX IF NOT EXISTS idx_catalog_compatible
            ON model_catalog_entries(snapshot_id, compatible, name);

        CREATE TABLE IF NOT EXISTS admin_audit_events (
            id TEXT PRIMARY KEY,
            admin_user_id TEXT REFERENCES users(id) ON DELETE SET NULL,
            action TEXT NOT NULL,
            target_type TEXT NOT NULL,
            target_id TEXT,
            before_json TEXT,
            after_json TEXT,
            created_at TEXT NOT NULL
        );
        CREATE INDEX IF NOT EXISTS idx_audit_created ON admin_audit_events(created_at DESC);
        """
    )


MIGRATIONS: tuple[tuple[int, Callable[[sqlite3.Connection], None]], ...] = ((1, _stage2_schema),)


def apply_migrations(connection: sqlite3.Connection) -> None:
    connection.execute(
        "CREATE TABLE IF NOT EXISTS schema_migrations "
        "(version INTEGER PRIMARY KEY, applied_at TEXT NOT NULL)"
    )
    applied = {
        row[0] for row in connection.execute("SELECT version FROM schema_migrations").fetchall()
    }
    for version, migration in MIGRATIONS:
        if version in applied:
            continue
        migration(connection)
        connection.execute(
            "INSERT INTO schema_migrations(version, applied_at) VALUES (?, ?)",
            (version, datetime.now(UTC).isoformat()),
        )
