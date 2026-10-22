"""Конкурентные изменения не оставляют сервис без активного администратора."""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from threading import Barrier

import pytest

from local_voice_transcriber.database import Database, LastActiveAdminError


@pytest.mark.parametrize("deactivate", [False, True])
def test_concurrent_removal_preserves_one_active_admin(tmp_path: Path, deactivate: bool) -> None:
    database = Database(tmp_path / "next.sqlite3")
    database.initialize()
    admins = [
        database.create_user(login=f"admin-{index}", password_hash="unused", role="admin")
        for index in range(2)
    ]
    barrier = Barrier(2)

    def remove(user_id: str) -> str:
        barrier.wait(timeout=5)
        try:
            database.update_user(
                user_id=user_id,
                role=None if deactivate else "user",
                is_active=False if deactivate else None,
                protect_last_admin=True,
            )
        except LastActiveAdminError:
            return "protected"
        return "updated"

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(remove, [admin["id"] for admin in admins]))

    assert sorted(results) == ["protected", "updated"]
    assert database.count_active_admins() == 1
