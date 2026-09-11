from __future__ import annotations

import json
import sqlite3
import uuid
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path
from typing import Any


def _now() -> str:
    return datetime.now(UTC).isoformat()


def _external_error_diagnostic(
    error_code: str | None, error_message: str | None, status: str
) -> tuple[str | None, str | None]:
    if status not in {"failed", "outcome_unknown"}:
        return None, None
    code = (error_code or "").strip().lower()
    message = (error_message or "").strip()
    text = message.lower()
    if status == "outcome_unknown" or "timeout" in text or "истекло ожидание" in text:
        return "timeout", "Превышено время ожидания; итог вызова неизвестен"
    if code in {"401", "403"}:
        return "authorization", "Провайдер отклонил API-ключ или доступ к модели"
    if code == "402" or "insufficient" in text or "balance" in text:
        return "balance", "Недостаточно средств или исчерпан лимит провайдера"
    if code == "404":
        return "model_not_found", "Модель или адрес API не найдены у провайдера"
    if code == "429":
        return "rate_limit", "Превышен лимит частоты запросов провайдера"
    if code in {"500", "502", "503", "504"}:
        return "provider_unavailable", "Временная ошибка на стороне провайдера"
    if any(word in text for word in ("response_format", "json_schema", "structured")):
        return "structured_output", "Модель не поддерживает требуемый структурированный ответ"
    if any(word in text for word in ("image", "vision", "modality")):
        return "vision", "Модель не приняла изображение или не поддерживает vision-вход"
    if any(
        word in text
        for word in (
            "json",
            "пустой ответ",
            "число результатов",
            "frame_index",
            "speaker_label",
        )
    ):
        return "invalid_response", "Модель вернула ответ в неподходящем формате"
    return "provider_error", message or "Провайдер не сообщил причину ошибки"


class AdminRepository:
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

    def get_settings(self) -> dict[str, Any]:
        with self._connect() as connection:
            rows = connection.execute("SELECT key, value_json FROM system_settings").fetchall()
        return {row["key"]: json.loads(row["value_json"]) for row in rows}

    def update_settings(self, values: dict[str, Any], admin_user_id: str) -> None:
        now = _now()
        before = self.get_settings()
        with self._connect() as connection:
            for key, value in values.items():
                connection.execute(
                    """
                    INSERT INTO system_settings(key, value_json, updated_at, updated_by)
                    VALUES (?, ?, ?, ?)
                    ON CONFLICT(key) DO UPDATE SET value_json=excluded.value_json,
                        updated_at=excluded.updated_at, updated_by=excluded.updated_by
                    """,
                    (key, json.dumps(value, ensure_ascii=False), now, admin_user_id),
                )
        self.audit(admin_user_id, "settings.update", "settings", None, before, values)

    def save_credential(
        self,
        provider: str,
        ciphertext: bytes,
        key_hint: str,
        admin_user_id: str,
        enabled: bool = True,
    ) -> None:
        before = self.get_credential(provider)
        with self._connect() as connection:
            connection.execute(
                """
                INSERT INTO provider_credentials
                    (provider, ciphertext, key_hint, enabled, updated_at, updated_by)
                VALUES (?, ?, ?, ?, ?, ?)
                ON CONFLICT(provider) DO UPDATE SET ciphertext=excluded.ciphertext,
                    key_hint=excluded.key_hint, enabled=excluded.enabled,
                    updated_at=excluded.updated_at, updated_by=excluded.updated_by,
                    last_checked_at=NULL, last_check_ok=NULL
                """,
                (provider, ciphertext, key_hint, int(enabled), _now(), admin_user_id),
            )
        self.audit(
            admin_user_id,
            "credential.replace",
            "provider",
            provider,
            {"key_hint": before["key_hint"]} if before else None,
            {"key_hint": key_hint, "enabled": enabled},
        )

    def get_credential(self, provider: str) -> dict[str, Any] | None:
        with self._connect() as connection:
            row = connection.execute(
                "SELECT * FROM provider_credentials WHERE provider=?", (provider,)
            ).fetchone()
        if row is None:
            return None
        result = dict(row)
        result["enabled"] = bool(result["enabled"])
        result["last_check_ok"] = (
            None if result["last_check_ok"] is None else bool(result["last_check_ok"])
        )
        return result

    def mark_credential_check(self, provider: str, ok: bool) -> None:
        with self._connect() as connection:
            connection.execute(
                "UPDATE provider_credentials SET last_checked_at=?, last_check_ok=? "
                "WHERE provider=?",
                (_now(), int(ok), provider),
            )

    def audit(
        self,
        admin_user_id: str | None,
        action: str,
        target_type: str,
        target_id: str | None,
        before: Any,
        after: Any,
    ) -> None:
        with self._connect() as connection:
            connection.execute(
                """
                INSERT INTO admin_audit_events
                    (id, admin_user_id, action, target_type, target_id,
                     before_json, after_json, created_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    str(uuid.uuid4()),
                    admin_user_id,
                    action,
                    target_type,
                    target_id,
                    json.dumps(before, ensure_ascii=False) if before is not None else None,
                    json.dumps(after, ensure_ascii=False) if after is not None else None,
                    _now(),
                ),
            )

    def replace_catalog(self, models: list[dict[str, Any]]) -> dict[str, Any]:
        snapshot_id = str(uuid.uuid4())
        fetched_at = _now()
        with self._connect() as connection:
            connection.execute(
                "INSERT INTO model_catalog_snapshots(id, fetched_at, status) VALUES (?, ?, 'ok')",
                (snapshot_id, fetched_at),
            )
            for model in models:
                architecture = model.get("architecture") or {}
                top_provider = model.get("top_provider") or {}
                inputs = architecture.get("input_modalities") or []
                outputs = architecture.get("output_modalities") or []
                parameters = (
                    model.get("supported_parameters")
                    or top_provider.get("supported_parameters")
                    or []
                )
                pricing = model.get("pricing") or top_provider.get("pricing") or {}
                pricing_units = model.get("pricing_units") or {}
                metadata_available = bool(inputs or outputs or parameters)
                compatible = not metadata_available or (
                    "image" in inputs
                    and "text" in outputs
                    and "response_format" in parameters
                )
                model_id = str(model.get("id") or "")
                if not model_id:
                    continue
                connection.execute(
                    """
                    INSERT INTO model_catalog_entries
                        (snapshot_id, model_id, name, author, is_alias,
                         input_modalities_json, output_modalities_json,
                         supported_parameters_json, pricing_json, compatible)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        snapshot_id,
                        model_id,
                        str(model.get("name") or model_id),
                        model_id.lstrip("~").split("/", 1)[0],
                        int(model_id.startswith("~")),
                        json.dumps(inputs),
                        json.dumps(outputs),
                        json.dumps(parameters),
                        json.dumps(
                            {
                                "pricing": pricing,
                                "pricing_units": pricing_units,
                            }
                        ),
                        int(compatible),
                    ),
                )
        return {"id": snapshot_id, "fetched_at": fetched_at, "count": len(models)}

    def mark_catalog_failure(self, message: str) -> None:
        with self._connect() as connection:
            connection.execute(
                "INSERT INTO model_catalog_snapshots(id, fetched_at, status, error_message) "
                "VALUES (?, ?, 'error', ?)",
                (str(uuid.uuid4()), _now(), message[:500]),
            )

    def list_models(self, compatible_only: bool = True) -> dict[str, Any]:
        with self._connect() as connection:
            snapshot = connection.execute(
                "SELECT * FROM model_catalog_snapshots WHERE status='ok' "
                "ORDER BY fetched_at DESC LIMIT 1"
            ).fetchone()
            last_attempt = connection.execute(
                "SELECT * FROM model_catalog_snapshots ORDER BY fetched_at DESC LIMIT 1"
            ).fetchone()
            if snapshot is None:
                return {"fetched_at": None, "stale": True, "models": [], "last_error": None}
            sql = "SELECT * FROM model_catalog_entries WHERE snapshot_id=?"
            parameters: list[Any] = [snapshot["id"]]
            if compatible_only:
                sql += " AND compatible=1"
            sql += " ORDER BY name"
            rows = connection.execute(sql, parameters).fetchall()
        models = []
        for row in rows:
            item = dict(row)
            for name in (
                "input_modalities_json",
                "output_modalities_json",
                "supported_parameters_json",
                "pricing_json",
            ):
                item[name.removesuffix("_json")] = json.loads(item.pop(name))
            item["is_alias"] = bool(item["is_alias"])
            item["compatible"] = bool(item["compatible"])
            models.append(item)
        last_error = (
            last_attempt["error_message"]
            if last_attempt and last_attempt["status"] == "error"
            else None
        )
        return {
            "fetched_at": snapshot["fetched_at"],
            "stale": bool(last_error),
            "models": models,
            "last_error": last_error,
        }

    def create_call(
        self,
        requested_model: str,
        image_count: int,
        *,
        provider: str = "routerai",
        run_id: str | None = None,
        job_id: str | None = None,
        user_id: str | None = None,
        attempt: int = 1,
    ) -> str:
        call_id = str(uuid.uuid4())
        with self._connect() as connection:
            connection.execute(
                """
                INSERT INTO external_model_calls
                    (id, run_id, job_id, user_id, provider, requested_model, status,
                     attempt, is_retry, image_count, created_at)
                VALUES (?, ?, ?, ?, ?, ?, 'started', ?, ?, ?, ?)
                """,
                (
                    call_id,
                    run_id,
                    job_id,
                    user_id,
                    provider,
                    requested_model,
                    attempt,
                    int(attempt > 1),
                    image_count,
                    _now(),
                ),
            )
        return call_id

    def finish_call(self, call_id: str, **values: Any) -> None:
        allowed = {
            "status",
            "actual_model",
            "actual_provider",
            "service_tier",
            "input_units",
            "output_units",
            "total_units",
            "provider_cost",
            "estimated_cost",
            "currency",
            "cost_source",
            "generation_id",
            "provider_request_id",
            "error_code",
            "error_message",
        }
        updates = {key: value for key, value in values.items() if key in allowed}
        updates["completed_at"] = _now()
        assignments = ", ".join(f"{key}=?" for key in updates)
        with self._connect() as connection:
            connection.execute(
                f"UPDATE external_model_calls SET {assignments} WHERE id=?",  # noqa: S608
                (*updates.values(), call_id),
            )

    def external_usage(
        self,
        date_from: str | None = None,
        date_to: str | None = None,
        user_id: str | None = None,
        model: str | None = None,
        status: str | None = None,
        limit: int = 100,
    ) -> dict[str, Any]:
        clauses = ["1=1"]
        parameters: list[Any] = []
        for column, value, operator in (
            ("created_at", date_from, ">="),
            ("created_at", date_to, "<="),
            ("user_id", user_id, "="),
            ("COALESCE(actual_model, requested_model)", model, "="),
            ("status", status, "="),
        ):
            if value:
                clauses.append(f"{column} {operator} ?")
                parameters.append(value)
        where = " AND ".join(clauses)
        with self._connect() as connection:
            totals = connection.execute(
                f"""
                SELECT COUNT(*) AS calls,
                    COALESCE(SUM(CASE WHEN status='completed' THEN 1 ELSE 0 END), 0)
                        AS successful_calls,
                    COALESCE(SUM(CASE WHEN status='failed' THEN 1 ELSE 0 END), 0)
                        AS failed_calls,
                    COALESCE(SUM(is_retry), 0) AS retries,
                    COALESCE(SUM(image_count), 0) AS images,
                    COALESCE(SUM(input_units), 0) AS input_units,
                    COALESCE(SUM(output_units), 0) AS output_units,
                    COALESCE(SUM(CAST(provider_cost AS NUMERIC)), 0) AS confirmed_cost,
                    COALESCE(SUM(CAST(estimated_cost AS NUMERIC)), 0) AS estimated_cost
                FROM external_model_calls WHERE {where}
                """,  # noqa: S608
                parameters,
            ).fetchone()
            rows = connection.execute(
                f"SELECT * FROM external_model_calls WHERE {where} "  # noqa: S608
                "ORDER BY created_at DESC LIMIT ?",
                (*parameters, limit),
            ).fetchall()
            grouped_rows = connection.execute(
                f"""
                SELECT c.user_id, COALESCE(u.email, 'Системный вызов') AS email,
                    COUNT(*) AS calls,
                    SUM(CASE WHEN c.status='completed' THEN 1 ELSE 0 END) successful_calls,
                    SUM(CASE WHEN c.status='failed' THEN 1 ELSE 0 END) failed_calls,
                    COALESCE(SUM(c.is_retry), 0) AS retries,
                    COALESCE(SUM(c.image_count), 0) AS images,
                    COALESCE(SUM(c.input_units), 0) AS input_units,
                    COALESCE(SUM(c.output_units), 0) AS output_units,
                    COALESCE(SUM(CAST(c.provider_cost AS NUMERIC)), 0) confirmed_cost,
                    COALESCE(SUM(CAST(c.estimated_cost AS NUMERIC)), 0) estimated_cost
                FROM (SELECT * FROM external_model_calls WHERE {where}) c
                LEFT JOIN users u ON u.id=c.user_id
                GROUP BY c.user_id, u.email ORDER BY u.email
                """,  # noqa: S608
                parameters,
            ).fetchall()
            failure_rows = connection.execute(
                f"""
                SELECT COALESCE(actual_model, requested_model) AS model,
                    status, error_code, error_message, COUNT(*) AS calls
                FROM external_model_calls
                WHERE {where} AND status IN ('failed', 'outcome_unknown')
                GROUP BY COALESCE(actual_model, requested_model), status,
                    error_code, error_message
                ORDER BY calls DESC, model
                LIMIT 50
                """,  # noqa: S608
                parameters,
            ).fetchall()
        items = [dict(row) for row in rows]
        for item in items:
            category, summary = _external_error_diagnostic(
                item.get("error_code"), item.get("error_message"), item["status"]
            )
            item["error_category"] = category
            item["error_summary"] = summary
        by_user = []
        for row in grouped_rows:
            item = dict(row)
            item["confirmed_cost"] = str(Decimal(str(item["confirmed_cost"])))
            item["estimated_cost"] = str(Decimal(str(item["estimated_cost"])))
            by_user.append(item)
        failure_reasons = []
        for row in failure_rows:
            item = dict(row)
            category, summary = _external_error_diagnostic(
                item.get("error_code"), item.get("error_message"), item["status"]
            )
            item["error_category"] = category
            item["error_summary"] = summary
            failure_reasons.append(item)
        return {
            **dict(totals),
            "confirmed_cost": str(Decimal(str(totals["confirmed_cost"]))),
            "estimated_cost": str(Decimal(str(totals["estimated_cost"]))),
            "currency": "RUB",
            "items": items,
            "by_user": by_user,
            "failure_reasons": failure_reasons,
            "updated_at": _now(),
        }

    def overview(self) -> dict[str, Any]:
        with self._connect() as connection:
            users = connection.execute("SELECT COUNT(*) FROM users").fetchone()[0]
            row = connection.execute(
                """
                SELECT COUNT(*) AS jobs,
                    COALESCE(SUM(CASE WHEN status='completed' THEN 1 ELSE 0 END), 0)
                        AS completed_jobs,
                    COALESCE(SUM(CASE WHEN status='failed' THEN 1 ELSE 0 END), 0)
                        AS failed_jobs,
                    COALESCE(SUM(CASE WHEN status IN ('queued','running') THEN 1 ELSE 0 END), 0)
                        AS active_jobs,
                    COALESCE(SUM(size_bytes), 0) AS source_bytes,
                    COALESCE(SUM(duration_ms), 0) AS recording_duration_ms
                FROM jobs
                """
            ).fetchone()
            user_rows = connection.execute(
                """
                SELECT u.id AS user_id, u.email, u.role,
                    COUNT(j.id) AS jobs,
                    SUM(CASE WHEN j.status='completed' THEN 1 ELSE 0 END) completed_jobs,
                    SUM(CASE WHEN j.status='failed' THEN 1 ELSE 0 END) failed_jobs,
                    SUM(CASE WHEN j.status IN ('queued','running') THEN 1 ELSE 0 END) active_jobs,
                    COALESCE(SUM(j.size_bytes), 0) AS source_bytes,
                    COALESCE(SUM(j.duration_ms), 0) AS recording_duration_ms
                FROM users u LEFT JOIN jobs j ON j.user_id=u.id
                GROUP BY u.id, u.email, u.role ORDER BY u.email
                """
            ).fetchall()
        return {
            "users": users,
            **dict(row),
            "by_user": [dict(item) for item in user_rows],
            "updated_at": _now(),
        }

    def configured_budget(self) -> Decimal | None:
        value = self.get_settings().get("global_budget")
        return Decimal(value) if value else None

    def confirmed_spend(self) -> Decimal:
        with self._connect() as connection:
            values = connection.execute(
                "SELECT provider_cost FROM external_model_calls WHERE provider_cost IS NOT NULL"
            ).fetchall()
        return sum((Decimal(row[0]) for row in values), Decimal("0"))
