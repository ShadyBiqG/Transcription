from __future__ import annotations

import json
import sqlite3
import uuid
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import UTC, datetime
from pathlib import Path
from typing import Any


def _now() -> str:
    return datetime.now(UTC).isoformat()


class AttributionRepository:
    def __init__(self, database_path: Path) -> None:
        self.database_path = database_path

    @contextmanager
    def _connect(self) -> Iterator[sqlite3.Connection]:
        connection = sqlite3.connect(self.database_path, timeout=30)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA journal_mode=WAL")
        connection.execute("PRAGMA foreign_keys=ON")
        try:
            yield connection
            connection.commit()
        except Exception:
            connection.rollback()
            raise
        finally:
            connection.close()

    def create_run(
        self,
        job_id: str,
        user_id: str,
        consent: bool,
        processing_mode: str,
        profile_snapshot: dict[str, Any],
        budget_amount: str | None,
        budget_currency: str,
    ) -> dict[str, Any]:
        run_id = str(uuid.uuid4())
        now = _now()
        with self._connect() as connection:
            connection.execute(
                """
                INSERT INTO attribution_runs (
                    id, job_id, user_id, status, consent_at, processing_mode, budget_amount,
                    budget_currency, profile_snapshot_json, created_at, updated_at
                ) VALUES (?, ?, ?, 'queued', ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    run_id,
                    job_id,
                    user_id,
                    now if consent else None,
                    processing_mode,
                    budget_amount,
                    budget_currency.upper(),
                    json.dumps(profile_snapshot, ensure_ascii=False),
                    now,
                    now,
                ),
            )
        return self.get_run(run_id)

    def get_run(self, run_id: str, user_id: str | None = None) -> dict[str, Any]:
        with self._connect() as connection:
            if user_id is None:
                row = connection.execute(
                    "SELECT * FROM attribution_runs WHERE id = ?", (run_id,)
                ).fetchone()
            else:
                row = connection.execute(
                    "SELECT * FROM attribution_runs WHERE id = ? AND user_id = ?",
                    (run_id, user_id),
                ).fetchone()
        if row is None:
            raise KeyError(run_id)
        result = dict(row)
        result["profile_snapshot"] = json.loads(result.pop("profile_snapshot_json"))
        return result

    def latest_runs(
        self, job_ids: list[str], user_id: str, *, ready_only: bool = False
    ) -> dict[str, dict[str, Any]]:
        if not job_ids:
            return {}
        placeholders = ",".join("?" for _ in job_ids)
        status_filter = (
            "AND a.status IN ('completed','blocked_budget')" if ready_only else ""
        )
        with self._connect() as connection:
            rows = connection.execute(
                f"""
                SELECT * FROM (
                    SELECT a.*, ROW_NUMBER() OVER (
                        PARTITION BY a.job_id ORDER BY a.created_at DESC, a.id DESC
                    ) AS position
                    FROM attribution_runs a
                    WHERE a.user_id=? AND a.job_id IN ({placeholders})
                    {status_filter}
                ) WHERE position=1
                """,  # noqa: S608
                (user_id, *job_ids),
            ).fetchall()
        results: dict[str, dict[str, Any]] = {}
        for row in rows:
            item = dict(row)
            item.pop("position", None)
            item["profile_snapshot"] = json.loads(item.pop("profile_snapshot_json"))
            results[item["job_id"]] = item
        return results

    def claim_next(self) -> dict[str, Any] | None:
        now = _now()
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                "SELECT id FROM attribution_runs WHERE status = 'queued' "
                "ORDER BY created_at LIMIT 1"
            ).fetchone()
            if row is None:
                return None
            connection.execute(
                "UPDATE attribution_runs SET status='running', started_at=?, updated_at=? "
                "WHERE id=? AND status='queued'",
                (now, now, row["id"]),
            )
        return self.get_run(row["id"])

    def recover_running(self) -> int:
        now = _now()
        recovered = 0
        with self._connect() as connection:
            rows = connection.execute(
                """
                SELECT id, job_id FROM attribution_runs
                WHERE status='running'
                    OR (status='failed' AND error_code='service_restarted')
                ORDER BY created_at DESC
                """,
            ).fetchall()
            for row in rows:
                another_active = connection.execute(
                    """
                    SELECT 1 FROM attribution_runs
                    WHERE job_id=? AND id<>? AND status IN ('queued','running')
                    LIMIT 1
                    """,
                    (row["job_id"], row["id"]),
                ).fetchone()
                if another_active:
                    continue
                cursor = connection.execute(
                    """
                    UPDATE attribution_runs SET status='queued', started_at=NULL,
                        completed_at=NULL, updated_at=?, error_code=NULL, error_message=NULL
                    WHERE id=? AND (
                        status='running'
                        OR (status='failed' AND error_code='service_restarted')
                    )
                    """,
                    (now, row["id"]),
                )
                recovered += cursor.rowcount
        return recovered

    def finish(
        self,
        run_id: str,
        status: str,
        error_code: str | None = None,
        error_message: str | None = None,
    ) -> dict[str, Any]:
        now = _now()
        with self._connect() as connection:
            connection.execute(
                """
                UPDATE attribution_runs SET status=?, completed_at=?, updated_at=?,
                    error_code=?, error_message=? WHERE id=?
                """,
                (status, now, now, error_code, error_message, run_id),
            )
        return self.get_run(run_id)

    def revoke_consent(self, run_id: str, user_id: str) -> None:
        with self._connect() as connection:
            cursor = connection.execute(
                "UPDATE attribution_runs SET consent_revoked_at=?, updated_at=? "
                "WHERE id=? AND user_id=?",
                (_now(), _now(), run_id, user_id),
            )
            if cursor.rowcount != 1:
                raise KeyError(run_id)

    def replace_segments(
        self, run_id: str, segments: list[dict[str, Any]]
    ) -> list[dict[str, Any]]:
        with self._connect() as connection:
            existing = connection.execute(
                "SELECT COUNT(*) FROM transcript_segments WHERE run_id=?", (run_id,)
            ).fetchone()[0]
            if existing:
                return self.list_segments(run_id)
            for segment in segments:
                connection.execute(
                    """
                    INSERT INTO transcript_segments
                        (id, run_id, ordinal, source_anchor, source_label, start_ms, end_ms, text)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        segment["id"],
                        run_id,
                        segment["ordinal"],
                        segment["source_anchor"],
                        segment["source_label"],
                        segment["start_ms"],
                        segment["end_ms"],
                        segment["text"],
                    ),
                )
        return self.list_segments(run_id)

    def list_segments(self, run_id: str) -> list[dict[str, Any]]:
        with self._connect() as connection:
            rows = connection.execute(
                """
                SELECT s.*, a.status AS attribution_status, a.speaker_label, a.confidence,
                    a.manual_label, a.highlight_bbox_json, a.label_bbox_json, a.reason
                FROM transcript_segments s
                LEFT JOIN segment_attributions a ON a.segment_id=s.id
                WHERE s.run_id=? ORDER BY s.ordinal
                """,
                (run_id,),
            ).fetchall()
        results = []
        for row in rows:
            item = dict(row)
            item["status"] = item.pop("attribution_status") or "pending"
            for name in ("highlight_bbox_json", "label_bbox_json"):
                item[name.removesuffix("_json")] = json.loads(item.pop(name) or "null")
            results.append(item)
        return results

    def add_frame(
        self,
        run_id: str,
        segment_id: str,
        requested_ms: int,
        actual_ms: int,
        relative_path: str,
        sha256: str,
        width: int | None = None,
        height: int | None = None,
    ) -> dict[str, Any]:
        frame_id = str(uuid.uuid4())
        with self._connect() as connection:
            connection.execute(
                """
                INSERT INTO evidence_frames
                    (id, run_id, segment_id, requested_ms, actual_ms, relative_path,
                     width, height, sha256)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    frame_id,
                    run_id,
                    segment_id,
                    requested_ms,
                    actual_ms,
                    relative_path,
                    width,
                    height,
                    sha256,
                ),
            )
        return {
            "id": frame_id,
            "requested_ms": requested_ms,
            "actual_ms": actual_ms,
            "relative_path": relative_path,
            "sha256": sha256,
        }

    def list_frames(self, segment_id: str) -> list[dict[str, Any]]:
        with self._connect() as connection:
            rows = connection.execute(
                "SELECT * FROM evidence_frames WHERE segment_id=? ORDER BY requested_ms",
                (segment_id,),
            ).fetchall()
        return [dict(row) for row in rows]

    def set_attribution(
        self,
        segment_id: str,
        status: str,
        speaker_label: str | None,
        confidence: float | None,
        highlight_bbox: list[float] | None = None,
        label_bbox: list[float] | None = None,
        reason: str | None = None,
        invocation_id: str | None = None,
    ) -> None:
        with self._connect() as connection:
            connection.execute(
                """
                INSERT INTO segment_attributions
                    (segment_id, status, speaker_label, confidence, highlight_bbox_json,
                     label_bbox_json, reason, invocation_id, updated_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(segment_id) DO UPDATE SET status=excluded.status,
                    speaker_label=excluded.speaker_label, confidence=excluded.confidence,
                    highlight_bbox_json=excluded.highlight_bbox_json,
                    label_bbox_json=excluded.label_bbox_json, reason=excluded.reason,
                    invocation_id=excluded.invocation_id, updated_at=excluded.updated_at
                """,
                (
                    segment_id,
                    status,
                    speaker_label,
                    confidence,
                    json.dumps(highlight_bbox),
                    json.dumps(label_bbox),
                    reason,
                    invocation_id,
                    _now(),
                ),
            )

    def set_manual_label(
        self,
        segment_id: str,
        user_id: str,
        label: str,
        *,
        reason: str = "Изменено пользователем",
    ) -> None:
        self.set_manual_labels([segment_id], user_id, label, reason=reason)

    def set_manual_labels(
        self,
        segment_ids: list[str],
        user_id: str,
        label: str,
        *,
        reason: str = "Изменено пользователем",
    ) -> int:
        normalized = " ".join(label.split())
        if not normalized:
            raise ValueError("Подпись не может быть пустой")
        if not segment_ids:
            return 0
        now = _now()
        with self._connect() as connection:
            connection.executemany(
                """
                INSERT INTO segment_attributions(
                    segment_id, status, manual_label, manual_by, manual_at, reason, updated_at
                ) VALUES (?, 'manual', ?, ?, ?, ?, ?)
                ON CONFLICT(segment_id) DO UPDATE SET status='manual',
                    manual_label=excluded.manual_label, manual_by=excluded.manual_by,
                    manual_at=excluded.manual_at, reason=excluded.reason,
                    updated_at=excluded.updated_at
                """,
                [
                    (segment_id, normalized, user_id, now, reason, now)
                    for segment_id in segment_ids
                ],
            )
        return len(segment_ids)
