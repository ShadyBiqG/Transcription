from __future__ import annotations

from pathlib import Path


def test_default_installer_uses_pinned_gigaam_v3_e2e_ctc_pack() -> None:
    installer = (
        Path(__file__).parents[2] / "deploy" / "install-default-model-packs.ps1"
    ).read_text(encoding="utf-8")

    assert '$asrDirectoryName = "asr/gigaam-v3-e2e-ctc-322c3b29"' in installer
    assert (
        '"v3_e2e_ctc.onnx" = '
        '"377701bd33568f4733feec2db5b2dc12544fd09a5a5dfa69ccf55d161f84027a"'
    ) in installer
    assert (
        '"v3_e2e_ctc_vocab.txt" = '
        '"142de7570b3de5b3035ce111a89c228e80e6085273731d944093ddf24fa539cd"'
    ) in installer
    assert 'pack_id = "gigaam-v3-e2e-ctc-322c3b29"' in installer
    assert 'model_name = "gigaam-v3-e2e-ctc"' in installer
