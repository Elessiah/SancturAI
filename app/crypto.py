"""Dérivation SancturAI v1 — IMMUABLE. Toute modification rend les vaults existants illisibles."""

import hashlib
import hmac
import unicodedata
from dataclasses import dataclass, field

import base58
import nacl.utils
from argon2.low_level import Type, hash_secret_raw
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.kdf.hkdf import HKDF
from mnemonic import Mnemonic
from nacl.bindings import (
    crypto_aead_xchacha20poly1305_ietf_decrypt,
    crypto_aead_xchacha20poly1305_ietf_encrypt,
)
from nacl.exceptions import CryptoError as _NaclCryptoError
from nacl.signing import SigningKey

_MNEMONIC = Mnemonic("english")
SALT = b"sancturai/v1"
NONCE_BYTES = 24
TAG_BYTES = 16
MAX_PHRASE_CHARS = 1024  # garde-fou d'entrée, pas un paramètre de dérivation
INFO_SIGN = b"sancturai/v1/ed25519"
INFO_VAULT = b"sancturai/v1/vault"
INFO_PATH = b"sancturai/v1/path"
# Données associées AEAD du vault (spec §6.1) : lient chaque blob à son agent ET à son path_id.
AAD_PATH_FORMAT = "sancturai/v1/path|{agent_id}|{path_id}"
AAD_CONTENT_FORMAT = "sancturai/v1/content|{agent_id}|{path_id}"


class CryptoError(Exception):
    pass


@dataclass(frozen=True)
class AgentKeys:
    signing_key: SigningKey = field(repr=False)
    vault_key: bytes = field(repr=False)
    path_key: bytes = field(repr=False)

    @property
    def agent_id(self) -> str:
        return base58.b58encode(bytes(self.signing_key.verify_key)).decode()


def generate_phrase() -> str:
    return _MNEMONIC.generate(strength=128)


def normalize_phrase(phrase: str) -> str:
    # spec §5.1 : NFKD, minuscules, espaces (tous blancs Unicode) réduits à un seul, bords retirés
    return " ".join(unicodedata.normalize("NFKD", phrase).lower().split())


def is_valid_phrase(phrase: str) -> bool:
    if len(phrase) > MAX_PHRASE_CHARS:
        return False
    norm = normalize_phrase(phrase)
    return len(norm.split()) == 12 and _MNEMONIC.check(norm)


def _hkdf(seed: bytes, info: bytes) -> bytes:
    return HKDF(algorithm=hashes.SHA256(), length=32, salt=None, info=info).derive(seed)


def derive_keys(phrase: str) -> AgentKeys:
    if not is_valid_phrase(phrase):
        raise ValueError("invalid phrase")
    seed = hash_secret_raw(
        secret=normalize_phrase(phrase).encode(),
        salt=SALT,
        time_cost=3,
        memory_cost=65_536,
        parallelism=1,
        hash_len=32,
        type=Type.ID,
    )
    return AgentKeys(
        signing_key=SigningKey(_hkdf(seed, INFO_SIGN)),
        vault_key=_hkdf(seed, INFO_VAULT),
        path_key=_hkdf(seed, INFO_PATH),
    )


def path_id(path_key: bytes, path: str) -> str:
    return hmac.new(path_key, path.encode(), hashlib.sha256).hexdigest()


def aad_path(agent_id: str, pid: str) -> bytes:
    return AAD_PATH_FORMAT.format(agent_id=agent_id, path_id=pid).encode()


def aad_content(agent_id: str, pid: str) -> bytes:
    return AAD_CONTENT_FORMAT.format(agent_id=agent_id, path_id=pid).encode()


def encrypt(key: bytes, plaintext: bytes, aad: bytes) -> bytes:
    nonce = nacl.utils.random(NONCE_BYTES)
    return nonce + crypto_aead_xchacha20poly1305_ietf_encrypt(plaintext, aad, nonce, key)


def decrypt(key: bytes, blob: bytes, aad: bytes) -> bytes:
    if len(blob) < NONCE_BYTES + TAG_BYTES:
        raise CryptoError("blob too short")
    try:
        return crypto_aead_xchacha20poly1305_ietf_decrypt(blob[NONCE_BYTES:], aad, blob[:NONCE_BYTES], key)
    except _NaclCryptoError as exc:
        raise CryptoError("decryption failed") from exc
