from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from local_voice_transcriber.vocabulary import (
    VocabularyRepository,
    VocabularyTermState,
    VocabularyTermStateError,
)


def test_shared_projection_excludes_actor_and_private_provenance(tmp_path: Path) -> None:
    repository = prepared_repository(tmp_path)

    term = repository.create_term(
        term_id="mercury",
        canonical="  ФГИС   Меркурий ",
        variants=("Меркурий", "fgis mercury"),
        context_hint="Ветеринарные сопроводительные документы",
        actor_id="user-a",
    )

    assert term.canonical == "ФГИС Меркурий"
    assert term.variants == ("fgis mercury", "Меркурий")
    assert set(term.__dataclass_fields__) == {
        "id",
        "canonical",
        "variants",
        "context_hint",
        "state",
        "created_at",
        "updated_at",
        "archived_at",
    }
    with sqlite3.connect(tmp_path / "next.sqlite3") as connection:
        event = connection.execute(
            "SELECT actor_id, action FROM next_vocabulary_events"
        ).fetchone()
        term_columns = {
            row[1] for row in connection.execute("PRAGMA table_info(next_vocabulary_terms)")
        }

    assert event == ("user-a", "term_created")
    assert "embedding" not in term_columns
    assert "vector" not in term_columns


def test_canonical_and_variants_are_unique_across_the_entire_directory(tmp_path: Path) -> None:
    repository = prepared_repository(tmp_path)
    repository.create_term(
        term_id="mercury",
        canonical="ФГИС Меркурий",
        variants=("Меркурий",),
        actor_id="user-a",
    )
    repository.create_term(term_id="erp", canonical="1С ERP", actor_id="user-b")

    with pytest.raises(sqlite3.IntegrityError):
        repository.create_term(term_id="other", canonical="меркурий", actor_id="user-b")
    with pytest.raises(sqlite3.IntegrityError):
        repository.add_variant(term_id="erp", variant="ФГИС МЕРКУРИЙ", actor_id="user-b")
    with pytest.raises(ValueError, match="уникальны"):
        repository.create_term(
            term_id="duplicate",
            canonical="Контур",
            variants=("контур.экстерн", "Контур.Экстерн"),
            actor_id="user-c",
        )


def test_search_only_returns_active_terms_and_escapes_like_characters(tmp_path: Path) -> None:
    repository = prepared_repository(tmp_path)
    repository.create_term(
        term_id="mercury",
        canonical="ФГИС Меркурий",
        variants=("Меркурий",),
        actor_id="user-a",
    )
    repository.create_term(
        term_id="percent",
        canonical="100% готово",
        variants=("сто процентов",),
        actor_id="user-b",
    )

    archived = repository.archive_term(term_id="mercury", actor_id="admin")

    assert archived.state is VocabularyTermState.ARCHIVED
    assert [term.id for term in repository.search_active_terms("мерк")] == []
    assert [term.id for term in repository.search_active_terms("%")] == ["percent"]
    assert [term.id for term in repository.list_shared_terms(state=None)] == ["percent", "mercury"]

    restored = repository.restore_term(term_id="mercury", actor_id="admin")

    assert restored.state is VocabularyTermState.ACTIVE
    assert [term.id for term in repository.search_active_terms("мерк")] == ["mercury"]


def test_edit_operations_are_audited_and_do_not_change_any_transcription(tmp_path: Path) -> None:
    repository = prepared_repository(tmp_path)
    repository.create_term(term_id="erp", canonical="1С ERP", actor_id="user-a")

    renamed = repository.rename_term(term_id="erp", canonical="1С:ERP", actor_id="user-b")
    updated = repository.update_context_hint(
        term_id="erp",
        context_hint="Планирование и учёт предприятия",
        actor_id="user-c",
    )
    final = repository.add_variant(term_id="erp", variant="ERP", actor_id="user-d")

    assert renamed.canonical == "1С:ERP"
    assert updated.context_hint == "Планирование и учёт предприятия"
    assert final.variants == ("ERP",)
    with sqlite3.connect(tmp_path / "next.sqlite3") as connection:
        actions = connection.execute(
            """
            SELECT actor_id, action
            FROM next_vocabulary_events
            WHERE term_id = ?
            ORDER BY rowid
            """,
            ("erp",),
        ).fetchall()
        transcript_tables = connection.execute(
            """
            SELECT name
            FROM sqlite_master
            WHERE type = 'table' AND name LIKE 'next_transcript%'
            """
        ).fetchall()

    assert actions == [
        ("user-a", "term_created"),
        ("user-b", "term_renamed"),
        ("user-c", "context_updated"),
        ("user-d", "variant_added"),
    ]
    assert transcript_tables == []


def test_archived_terms_are_immutable_until_restored(tmp_path: Path) -> None:
    repository = prepared_repository(tmp_path)
    repository.create_term(term_id="erp", canonical="1С ERP", actor_id="user-a")
    repository.archive_term(term_id="erp", actor_id="user-a")

    with pytest.raises(VocabularyTermStateError, match="активный"):
        repository.add_variant(term_id="erp", variant="ERP", actor_id="user-b")
    with pytest.raises(VocabularyTermStateError, match="переход"):
        repository.archive_term(term_id="erp", actor_id="user-b")


def prepared_repository(tmp_path: Path) -> VocabularyRepository:
    repository = VocabularyRepository(tmp_path / "next.sqlite3")
    repository.migrate()
    return repository
