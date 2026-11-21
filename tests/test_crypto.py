import json
from pathlib import Path

import pytest

from app import crypto

VEC = json.loads((Path(__file__).parent / "vectors" / "crypto_v1.json").read_text())


def test_vectors_are_stable() -> None:
    keys = crypto.derive_keys(VEC["phrase"])
    assert keys.agent_id == VEC["agent_id"]
    assert keys.vault_key.hex() == VEC["vault_key_hex"]
    assert keys.path_key.hex() == VEC["path_key_hex"]
    assert crypto.path_id(keys.path_key, "/README.md") == VEC["path_id_readme"]
    plain = crypto.decrypt(keys.vault_key, bytes.fromhex(VEC["blob_hex"]), VEC["blob_aad"].encode())
    assert plain.decode() == VEC["blob_plaintext"]


def test_normalization_tolerates_case_and_spaces() -> None:
    messy = "  " + VEC["phrase"].upper().replace(" ", "   ") + "\n"
    assert crypto.derive_keys(messy).agent_id == VEC["agent_id"]


def test_generated_phrase_is_valid_and_unique() -> None:
    a, b = crypto.generate_phrase(), crypto.generate_phrase()
    assert crypto.is_valid_phrase(a) and len(a.split()) == 12
    assert a != b


def test_invalid_phrase_rejected() -> None:
    assert not crypto.is_valid_phrase("abandon " * 12)
    with pytest.raises(ValueError):
        crypto.derive_keys("not a phrase")


def test_aead_roundtrip_and_tamper() -> None:
    key = bytes(32)
    blob = crypto.encrypt(key, b"secret", b"ctx")
    assert crypto.decrypt(key, blob, b"ctx") == b"secret"
    with pytest.raises(crypto.CryptoError):
        crypto.decrypt(key, blob, b"other-ctx")
    tampered = blob[:-1] + bytes([blob[-1] ^ 1])
    with pytest.raises(crypto.CryptoError):
        crypto.decrypt(key, tampered, b"ctx")
    with pytest.raises(crypto.CryptoError):
        crypto.decrypt(key, b"short", b"ctx")


def test_two_encryptions_differ() -> None:
    key = bytes(32)
    assert crypto.encrypt(key, b"x", b"") != crypto.encrypt(key, b"x", b"")
