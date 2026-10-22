from __future__ import annotations

import pytest

from local_voice_transcriber.auth import (
    PASSWORD_MINIMUM_LENGTH,
    ApiError,
    hash_password,
    normalize_login,
    validate_password,
    verify_password,
)


def test_password_is_scrypt_hashed_and_verified() -> None:
    password_hash = hash_password("correct horse battery")

    assert password_hash.startswith("scrypt$")
    assert verify_password("correct horse battery", password_hash)
    assert not verify_password("неверный пароль", password_hash)


def test_login_is_normalized() -> None:
    assert normalize_login("  Admin.Local  ") == "admin.local"


def test_email_login_is_normalized() -> None:
    assert (
        normalize_login("  Admin.Team@Example-Company.test  ")
        == "admin.team@example-company.test"
    )


def test_short_password_is_rejected() -> None:
    with pytest.raises(ApiError) as error:
        validate_password("x" * (PASSWORD_MINIMUM_LENGTH - 1))

    assert error.value.code == "weak_password"


@pytest.mark.parametrize(
    "login",
    ["ab", "bad space", "кириллица", "admin@host", "admin..team@example.test", "a@b"],
)
def test_invalid_login_is_rejected(login: str) -> None:
    with pytest.raises(ApiError) as error:
        normalize_login(login)

    assert error.value.code == "invalid_login"
