import os

import pytest

from transcription_service.secrets import mask_secret, protect_secret, unprotect_secret


def test_secret_mask_does_not_reveal_key() -> None:
    key = "routerai-secret-value"
    masked = mask_secret(key)
    assert key not in masked
    assert masked.endswith("alue")


@pytest.mark.skipif(os.name != "nt", reason="DPAPI доступен только на Windows")
def test_dpapi_round_trip() -> None:
    encrypted = protect_secret("routerai-secret-value")
    assert b"routerai-secret-value" not in encrypted
    assert unprotect_secret(encrypted) == "routerai-secret-value"
