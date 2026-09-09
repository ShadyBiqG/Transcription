from transcription_service.auth import hash_password, hash_session_token, verify_password


def test_password_is_hashed_and_verified():
    encoded = hash_password("password123")

    assert "password123" not in encoded
    assert verify_password("password123", encoded)
    assert not verify_password("wrong-password", encoded)
    assert not verify_password("password123", "invalid")


def test_session_token_is_not_stored_as_plain_text():
    token = "secret-session-token"

    assert hash_session_token(token) != token
    assert len(hash_session_token(token)) == 64
