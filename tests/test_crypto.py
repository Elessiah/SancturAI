import base64
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


def test_oversized_phrase_rejected_before_normalization() -> None:
    padded = VEC["phrase"] + " " * crypto.MAX_PHRASE_CHARS
    assert not crypto.is_valid_phrase(padded)
    with pytest.raises(ValueError):
        crypto.derive_keys(padded)


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


def test_signed_message_vectors() -> None:
    # spec connexion signée §3 : formats figés, signatures Ed25519 déterministes
    keys = crypto.derive_keys(VEC["phrase"])
    login = crypto.login_message(VEC["signed_host"], VEC["agent_id"], VEC["login_nonce"])
    register = crypto.register_message(VEC["signed_host"], VEC["agent_id"], VEC["register_handle"],
                                       VEC["register_ticket"])
    assert login.decode() == VEC["login_message"] and register.decode() == VEC["register_message"]
    for message, field in ((login, "login_signature_b64"), (register, "register_signature_b64")):
        signature = base64.b64decode(VEC[field])
        assert keys.signing_key.sign(message).signature == signature
        assert crypto.verify_signature(VEC["agent_id"], message, signature)


def test_signature_rejections() -> None:
    signature = base64.b64decode(VEC["login_signature_b64"])
    login = VEC["login_message"].encode()
    other = crypto.derive_keys(crypto.generate_phrase()).agent_id
    assert not crypto.verify_signature(other, login, signature)  # autre clé
    assert not crypto.verify_signature(VEC["agent_id"], login + b"x", signature)  # autre message
    assert not crypto.verify_signature(VEC["agent_id"], login, signature[:-1])  # longueur
    # une signature de connexion ne vaut pas inscription (préfixes distincts)
    assert not crypto.verify_signature(VEC["agent_id"], VEC["register_message"].encode(), signature)
    for bad in ("", "0OIl", "1" + VEC["agent_id"], "system:tribunal", "abc"):
        assert crypto.public_key_of(bad) is None, bad
        assert not crypto.verify_signature(bad, login, signature)
    assert crypto.public_key_of(VEC["agent_id"]) is not None
    # liée à l'instance : la même signature ne vaut pas pour un autre hôte
    elsewhere = crypto.login_message("autre.example", VEC["agent_id"], VEC["login_nonce"])
    assert not crypto.verify_signature(VEC["agent_id"], elsewhere, signature)


def test_instance_host() -> None:
    assert crypto.instance_host("https://Sancturai.Bluegenji-Esport.fr/") == "sancturai.bluegenji-esport.fr"
    assert crypto.instance_host("http://127.0.0.1:8300") == "127.0.0.1:8300"
