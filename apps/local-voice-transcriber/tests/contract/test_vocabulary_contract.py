"""Контракт общего словаря: доступен всем, но не раскрывает автора изменений."""

from __future__ import annotations

from conftest import login, register
from fastapi.testclient import TestClient


def _headers(client: TestClient) -> dict[str, str]:
    return {"X-CSRF-Token": client.cookies.get("local_voice_transcriber_csrf") or ""}


def test_vocabulary_is_shared_and_editable_without_private_provenance(client: TestClient) -> None:
    register(client, "first-user")
    login(client, "first-user")
    created = client.post(
        "/api/v1/vocabulary",
        headers=_headers(client),
        json={
            "canonical": "ФГИС Меркурий",
            "variants": ["Меркурий"],
            "context_hint": "Ветеринарные документы",
        },
    )

    assert created.status_code == 201, created.text
    term = created.json()["term"]
    assert "created_by" not in term
    assert "actor_id" not in term
    assert "source_job_id" not in term

    other = TestClient(client.app)
    with other:
        register(other, "second-user")
        login(other, "second-user")
        listed = other.get("/api/v1/vocabulary?query=мерк")
        renamed = other.patch(
            f"/api/v1/vocabulary/{term['id']}",
            headers=_headers(other),
            json={"canonical": "ФГИС «Меркурий»"},
        )
        archived = other.post(
            f"/api/v1/vocabulary/{term['id']}/archive",
            headers=_headers(other),
        )

    assert listed.status_code == 200, listed.text
    assert [item["id"] for item in listed.json()["items"]] == [term["id"]]
    assert renamed.status_code == 200, renamed.text
    assert renamed.json()["term"]["canonical"] == "ФГИС «Меркурий»"
    assert archived.status_code == 403
    assert archived.json()["code"] == "admin_required"
