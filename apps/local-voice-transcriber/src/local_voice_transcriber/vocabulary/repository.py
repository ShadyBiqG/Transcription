"""SQLite-репозиторий общего словаря специфичных слов.

Таблицы этого модуля не содержат эмбеддингов, векторов или пользовательских
расшифровок. Изменения словаря никогда сами по себе не переписывают готовые
транскрипции: дальнейшее использование термина является явным решением
пайплайна или пользователя.
"""

from __future__ import annotations

import sqlite3
import unicodedata
from collections.abc import Iterator, Sequence
from contextlib import contextmanager
from datetime import UTC, datetime
from pathlib import Path
from uuid import uuid4

from .models import (
    SharedVocabularyTerm,
    VocabularyTermNotFoundError,
    VocabularyTermState,
    VocabularyTermStateError,
)

_TERM_STATES = tuple(state.value for state in VocabularyTermState)
_MAX_TERM_LENGTH = 250
_MAX_CONTEXT_HINT_LENGTH = 1_000


class VocabularyRepository:
    """Durable-слой глобального словаря кандидата приложения."""

    def __init__(self, database_path: Path) -> None:
        self._database_path = database_path
        self._database_path.parent.mkdir(parents=True, exist_ok=True)

    def migrate(self) -> None:
        """Создаёт изолированные таблицы общего словаря."""

        with self._connection() as connection:
            connection.executescript(
                """
                CREATE TABLE IF NOT EXISTS next_vocabulary_terms (
                    id TEXT PRIMARY KEY,
                    canonical TEXT NOT NULL,
                    normalized_canonical TEXT NOT NULL UNIQUE,
                    context_hint TEXT,
                    state TEXT NOT NULL CHECK(state IN ('active', 'archived')),
                    created_by TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    archived_at TEXT
                );
                CREATE INDEX IF NOT EXISTS next_vocabulary_terms_state_idx
                    ON next_vocabulary_terms(state, normalized_canonical, id);

                CREATE TABLE IF NOT EXISTS next_vocabulary_variants (
                    id TEXT PRIMARY KEY,
                    term_id TEXT NOT NULL
                        REFERENCES next_vocabulary_terms(id) ON DELETE CASCADE,
                    value TEXT NOT NULL,
                    normalized_value TEXT NOT NULL UNIQUE,
                    created_at TEXT NOT NULL,
                    UNIQUE(term_id, normalized_value)
                );
                CREATE INDEX IF NOT EXISTS next_vocabulary_variants_term_idx
                    ON next_vocabulary_variants(term_id, normalized_value, id);

                CREATE TABLE IF NOT EXISTS next_vocabulary_lexemes (
                    normalized_value TEXT PRIMARY KEY,
                    term_id TEXT NOT NULL
                        REFERENCES next_vocabulary_terms(id) ON DELETE CASCADE,
                    kind TEXT NOT NULL CHECK(kind IN ('canonical', 'variant')),
                    UNIQUE(term_id, normalized_value)
                );
                CREATE INDEX IF NOT EXISTS next_vocabulary_lexemes_term_idx
                    ON next_vocabulary_lexemes(term_id, kind);

                CREATE TABLE IF NOT EXISTS next_vocabulary_events (
                    id TEXT PRIMARY KEY,
                    term_id TEXT NOT NULL
                        REFERENCES next_vocabulary_terms(id) ON DELETE CASCADE,
                    actor_id TEXT NOT NULL,
                    action TEXT NOT NULL CHECK(action IN (
                        'term_created', 'term_renamed', 'context_updated',
                        'variant_added', 'term_archived', 'term_restored'
                    )),
                    created_at TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS next_vocabulary_events_term_idx
                    ON next_vocabulary_events(term_id, created_at, id);
                """
            )

    def create_term(
        self,
        *,
        term_id: str,
        canonical: str,
        actor_id: str,
        variants: Sequence[str] = (),
        context_hint: str | None = None,
    ) -> SharedVocabularyTerm:
        """Добавляет общий термин и его глобально уникальные варианты."""

        clean_term_id = _identifier(term_id, "Идентификатор термина")
        clean_actor_id = _identifier(actor_id, "Идентификатор автора")
        clean_canonical = _term_value(canonical, "Каноническая форма")
        clean_variants = _unique_variants(variants, canonical=clean_canonical)
        clean_context_hint = _context_hint(context_hint)
        now = _utc_now()
        with self._transaction() as connection:
            connection.execute(
                """
                INSERT INTO next_vocabulary_terms (
                    id, canonical, normalized_canonical, context_hint, state,
                    created_by, created_at, updated_at, archived_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, NULL)
                """,
                (
                    clean_term_id,
                    clean_canonical,
                    _normalized(clean_canonical),
                    clean_context_hint,
                    VocabularyTermState.ACTIVE.value,
                    clean_actor_id,
                    now,
                    now,
                ),
            )
            self._insert_lexeme(
                connection,
                term_id=clean_term_id,
                normalized_value=_normalized(clean_canonical),
                kind="canonical",
            )
            self._insert_variants(
                connection,
                term_id=clean_term_id,
                variants=clean_variants,
                now=now,
            )
            self._event(
                connection,
                term_id=clean_term_id,
                actor_id=clean_actor_id,
                action="term_created",
            )
        return self.get_shared_term(clean_term_id)

    def get_shared_term(self, term_id: str) -> SharedVocabularyTerm:
        """Возвращает безопасную проекцию независимо от состояния термина."""

        with self._connection() as connection:
            row = connection.execute(
                self._shared_term_query() + " WHERE terms.id = ?",
                (_identifier(term_id, "Идентификатор термина"),),
            ).fetchone()
        if row is None:
            raise VocabularyTermNotFoundError("Термин словаря не найден")
        return _to_shared_term(row)

    def list_shared_terms(
        self,
        *,
        query: str = "",
        state: VocabularyTermState | None = VocabularyTermState.ACTIVE,
    ) -> list[SharedVocabularyTerm]:
        """Ищет общие термины по канонической форме либо варианту."""

        conditions: list[str] = []
        values: list[object] = []
        if state is not None:
            conditions.append("terms.state = ?")
            values.append(state.value)
        if query.strip():
            normalized_query = _normalized(query)
            pattern = f"%{_escape_like(normalized_query)}%"
            conditions.append(
                """
                (
                    terms.normalized_canonical LIKE ? ESCAPE '\\'
                    OR EXISTS (
                        SELECT 1
                        FROM next_vocabulary_variants AS matching_variants
                        WHERE matching_variants.term_id = terms.id
                          AND matching_variants.normalized_value LIKE ? ESCAPE '\\'
                    )
                )
                """
            )
            values.extend((pattern, pattern))

        statement = self._shared_term_query()
        if conditions:
            statement += " WHERE " + " AND ".join(conditions)
        statement += " ORDER BY terms.normalized_canonical, terms.id"
        with self._connection() as connection:
            rows = connection.execute(statement, values).fetchall()
        return [_to_shared_term(row) for row in rows]

    def search_active_terms(self, query: str = "") -> list[SharedVocabularyTerm]:
        """Возвращает только активную часть общего словаря."""

        return self.list_shared_terms(query=query, state=VocabularyTermState.ACTIVE)

    def rename_term(
        self,
        *,
        term_id: str,
        canonical: str,
        actor_id: str,
    ) -> SharedVocabularyTerm:
        """Меняет каноническую форму активного термина без изменения транскрипций."""

        clean_term_id = _identifier(term_id, "Идентификатор термина")
        clean_actor_id = _identifier(actor_id, "Идентификатор автора")
        clean_canonical = _term_value(canonical, "Каноническая форма")
        normalized_canonical = _normalized(clean_canonical)
        now = _utc_now()

        with self._transaction() as connection:
            current = self._require_active_term(connection, clean_term_id)
            previous_normalized = str(current["normalized_canonical"])
            if normalized_canonical != previous_normalized:
                self._ensure_lexeme_available(
                    connection,
                    normalized_value=normalized_canonical,
                    term_id=clean_term_id,
                )
                connection.execute(
                    """
                    DELETE FROM next_vocabulary_lexemes
                    WHERE term_id = ? AND kind = 'canonical'
                    """,
                    (clean_term_id,),
                )
                self._insert_lexeme(
                    connection,
                    term_id=clean_term_id,
                    normalized_value=normalized_canonical,
                    kind="canonical",
                )
            connection.execute(
                """
                UPDATE next_vocabulary_terms
                SET canonical = ?, normalized_canonical = ?, updated_at = ?
                WHERE id = ?
                """,
                (clean_canonical, normalized_canonical, now, clean_term_id),
            )
            self._event(
                connection,
                term_id=clean_term_id,
                actor_id=clean_actor_id,
                action="term_renamed",
            )
        return self.get_shared_term(clean_term_id)

    def update_context_hint(
        self,
        *,
        term_id: str,
        context_hint: str | None,
        actor_id: str,
    ) -> SharedVocabularyTerm:
        """Изменяет общую подсказку контекста у активного термина."""

        clean_term_id = _identifier(term_id, "Идентификатор термина")
        clean_actor_id = _identifier(actor_id, "Идентификатор автора")
        clean_context_hint = _context_hint(context_hint)
        now = _utc_now()
        with self._transaction() as connection:
            self._require_active_term(connection, clean_term_id)
            connection.execute(
                """
                UPDATE next_vocabulary_terms
                SET context_hint = ?, updated_at = ?
                WHERE id = ?
                """,
                (clean_context_hint, now, clean_term_id),
            )
            self._event(
                connection,
                term_id=clean_term_id,
                actor_id=clean_actor_id,
                action="context_updated",
            )
        return self.get_shared_term(clean_term_id)

    def add_variant(
        self,
        *,
        term_id: str,
        variant: str,
        actor_id: str,
    ) -> SharedVocabularyTerm:
        """Добавляет вариант с глобальным запретом совпадения с любым лексемом."""

        clean_term_id = _identifier(term_id, "Идентификатор термина")
        clean_actor_id = _identifier(actor_id, "Идентификатор автора")
        clean_variant = _term_value(variant, "Вариант")
        now = _utc_now()
        with self._transaction() as connection:
            term = self._require_active_term(connection, clean_term_id)
            if _normalized(clean_variant) == str(term["normalized_canonical"]):
                raise ValueError("Вариант не может совпадать с канонической формой.")
            self._insert_variants(
                connection,
                term_id=clean_term_id,
                variants=(clean_variant,),
                now=now,
            )
            connection.execute(
                "UPDATE next_vocabulary_terms SET updated_at = ? WHERE id = ?",
                (now, clean_term_id),
            )
            self._event(
                connection,
                term_id=clean_term_id,
                actor_id=clean_actor_id,
                action="variant_added",
            )
        return self.get_shared_term(clean_term_id)

    def archive_term(self, *, term_id: str, actor_id: str) -> SharedVocabularyTerm:
        """Скрывает термин из активной выдачи, не меняя существующие результаты."""

        return self._change_state(
            term_id=term_id,
            actor_id=actor_id,
            expected=VocabularyTermState.ACTIVE,
            target=VocabularyTermState.ARCHIVED,
            action="term_archived",
        )

    def restore_term(self, *, term_id: str, actor_id: str) -> SharedVocabularyTerm:
        """Возвращает архивный термин в будущую активную выдачу."""

        return self._change_state(
            term_id=term_id,
            actor_id=actor_id,
            expected=VocabularyTermState.ARCHIVED,
            target=VocabularyTermState.ACTIVE,
            action="term_restored",
        )

    def _change_state(
        self,
        *,
        term_id: str,
        actor_id: str,
        expected: VocabularyTermState,
        target: VocabularyTermState,
        action: str,
    ) -> SharedVocabularyTerm:
        clean_term_id = _identifier(term_id, "Идентификатор термина")
        clean_actor_id = _identifier(actor_id, "Идентификатор автора")
        now = _utc_now()
        with self._transaction() as connection:
            row = self._require_term(connection, clean_term_id)
            current_state = VocabularyTermState(row["state"])
            if current_state is not expected:
                raise VocabularyTermStateError(
                    f"Невозможен переход термина из {current_state.value} в {target.value}."
                )
            connection.execute(
                """
                UPDATE next_vocabulary_terms
                SET state = ?, archived_at = ?, updated_at = ?
                WHERE id = ?
                """,
                (
                    target.value,
                    now if target is VocabularyTermState.ARCHIVED else None,
                    now,
                    clean_term_id,
                ),
            )
            self._event(
                connection,
                term_id=clean_term_id,
                actor_id=clean_actor_id,
                action=action,
            )
        return self.get_shared_term(clean_term_id)

    def _insert_variants(
        self,
        connection: sqlite3.Connection,
        *,
        term_id: str,
        variants: Sequence[str],
        now: str,
    ) -> None:
        for variant in variants:
            normalized_variant = _normalized(variant)
            self._insert_lexeme(
                connection,
                term_id=term_id,
                normalized_value=normalized_variant,
                kind="variant",
            )
            connection.execute(
                """
                INSERT INTO next_vocabulary_variants (
                    id, term_id, value, normalized_value, created_at
                ) VALUES (?, ?, ?, ?, ?)
                """,
                (str(uuid4()), term_id, variant, normalized_variant, now),
            )

    @staticmethod
    def _insert_lexeme(
        connection: sqlite3.Connection,
        *,
        term_id: str,
        normalized_value: str,
        kind: str,
    ) -> None:
        connection.execute(
            """
            INSERT INTO next_vocabulary_lexemes (normalized_value, term_id, kind)
            VALUES (?, ?, ?)
            """,
            (normalized_value, term_id, kind),
        )

    @staticmethod
    def _ensure_lexeme_available(
        connection: sqlite3.Connection,
        *,
        normalized_value: str,
        term_id: str,
    ) -> None:
        existing = connection.execute(
            """
            SELECT term_id
            FROM next_vocabulary_lexemes
            WHERE normalized_value = ?
            """,
            (normalized_value,),
        ).fetchone()
        if existing is not None and existing["term_id"] != term_id:
            raise sqlite3.IntegrityError("Лексема уже принадлежит другому общему термину.")
        if existing is not None:
            raise sqlite3.IntegrityError("Лексема уже существует у этого термина.")

    @staticmethod
    def _event(
        connection: sqlite3.Connection,
        *,
        term_id: str,
        actor_id: str,
        action: str,
    ) -> None:
        connection.execute(
            """
            INSERT INTO next_vocabulary_events (id, term_id, actor_id, action, created_at)
            VALUES (?, ?, ?, ?, ?)
            """,
            (str(uuid4()), term_id, actor_id, action, _utc_now()),
        )

    @staticmethod
    def _require_term(connection: sqlite3.Connection, term_id: str) -> sqlite3.Row:
        row = connection.execute(
            """
            SELECT id, normalized_canonical, state
            FROM next_vocabulary_terms
            WHERE id = ?
            """,
            (term_id,),
        ).fetchone()
        if row is None:
            raise VocabularyTermNotFoundError("Термин словаря не найден")
        return row

    def _require_active_term(self, connection: sqlite3.Connection, term_id: str) -> sqlite3.Row:
        row = self._require_term(connection, term_id)
        if VocabularyTermState(row["state"]) is not VocabularyTermState.ACTIVE:
            raise VocabularyTermStateError("Изменять можно только активный термин словаря.")
        return row

    @staticmethod
    def _shared_term_query() -> str:
        return """
            SELECT
                terms.id,
                terms.canonical,
                terms.context_hint,
                terms.state,
                terms.created_at,
                terms.updated_at,
                terms.archived_at,
                COALESCE((
                    SELECT json_group_array(value)
                    FROM (
                        SELECT value
                        FROM next_vocabulary_variants
                        WHERE term_id = terms.id
                        ORDER BY normalized_value, id
                    )
                ), '[]') AS variants_json
            FROM next_vocabulary_terms AS terms
        """

    @contextmanager
    def _connection(self) -> Iterator[sqlite3.Connection]:
        connection = sqlite3.connect(self._database_path)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys = ON")
        connection.execute("PRAGMA busy_timeout = 5000")
        try:
            yield connection
            connection.commit()
        except BaseException:
            connection.rollback()
            raise
        finally:
            connection.close()

    @contextmanager
    def _transaction(self) -> Iterator[sqlite3.Connection]:
        with self._connection() as connection:
            connection.execute("BEGIN IMMEDIATE")
            yield connection


def _to_shared_term(row: sqlite3.Row) -> SharedVocabularyTerm:
    variants = tuple(str(value) for value in _parse_variants(row["variants_json"]))
    return SharedVocabularyTerm(
        id=str(row["id"]),
        canonical=str(row["canonical"]),
        variants=variants,
        context_hint=str(row["context_hint"]) if row["context_hint"] is not None else None,
        state=VocabularyTermState(row["state"]),
        created_at=str(row["created_at"]),
        updated_at=str(row["updated_at"]),
        archived_at=str(row["archived_at"]) if row["archived_at"] is not None else None,
    )


def _parse_variants(raw_value: object) -> list[object]:
    if not isinstance(raw_value, str):
        raise RuntimeError("Варианты общего словаря имеют неверный формат.")
    import json

    parsed = json.loads(raw_value)
    if not isinstance(parsed, list) or not all(isinstance(item, str) for item in parsed):
        raise RuntimeError("Варианты общего словаря имеют неверный формат.")
    return parsed


def _identifier(value: str, label: str) -> str:
    cleaned = value.strip()
    if not cleaned or len(cleaned) > 128:
        raise ValueError(f"{label} обязателен и не должен превышать 128 символов.")
    return cleaned


def _term_value(value: str, label: str) -> str:
    cleaned = " ".join(value.split())
    if not cleaned or len(cleaned) > _MAX_TERM_LENGTH:
        raise ValueError(f"{label} обязателен и не должен превышать {_MAX_TERM_LENGTH} символов.")
    return cleaned


def _context_hint(value: str | None) -> str | None:
    if value is None:
        return None
    cleaned = " ".join(value.split())
    if not cleaned:
        return None
    if len(cleaned) > _MAX_CONTEXT_HINT_LENGTH:
        raise ValueError(
            f"Контекстная подсказка не должна превышать {_MAX_CONTEXT_HINT_LENGTH} символов."
        )
    return cleaned


def _unique_variants(variants: Sequence[str], *, canonical: str) -> tuple[str, ...]:
    canonical_normalized = _normalized(canonical)
    result: list[str] = []
    normalized_values: set[str] = set()
    for value in variants:
        cleaned = _term_value(value, "Вариант")
        normalized = _normalized(cleaned)
        if normalized == canonical_normalized:
            raise ValueError("Вариант не может совпадать с канонической формой.")
        if normalized in normalized_values:
            raise ValueError("Варианты должны быть уникальны.")
        result.append(cleaned)
        normalized_values.add(normalized)
    return tuple(result)


def _normalized(value: str) -> str:
    return unicodedata.normalize("NFKC", " ".join(value.split())).casefold()


def _escape_like(value: str) -> str:
    return value.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")


def _utc_now() -> str:
    return datetime.now(UTC).isoformat()
