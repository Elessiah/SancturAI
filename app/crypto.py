"""Dérivation SancturAI v1 — IMMUABLE. Toute modification rend les vaults existants illisibles."""

import hashlib
import hmac
import unicodedata
from dataclasses import dataclass, field
from urllib.parse import urlsplit

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
from nacl.exceptions import BadSignatureError
from nacl.exceptions import CryptoError as _NaclCryptoError
from nacl.signing import SigningKey, VerifyKey

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
# Messages signés Ed25519 (spec connexion signée §3) : le préfixe sépare connexion et inscription, l'hôte lie la
# signature à l'instance (une instance malveillante ne peut pas relayer vers une autre ce qu'on lui a signé).
LOGIN_MESSAGE_FORMAT = "sancturai/v1/login|{host}|{agent_id}|{nonce}"
REGISTER_MESSAGE_FORMAT = "sancturai/v1/register|{host}|{agent_id}|{handle}|{ticket}"
PUBLIC_KEY_BYTES = 32
SIGNATURE_BYTES = 64


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


_DEFAULT_PORTS = {"https": 443, "http": 80}


def instance_host(url: str) -> str:
    """Hôte canonique d'une instance, celui des messages signés : nom d'hôte en minuscules, sans point final, en IDNA
    (ASCII, comme sur le réseau), suivi de `:port` seulement si le port n'est pas celui du schéma. Une URL avec des
    identifiants (user@) ou sans hôte est refusée (ValueError)."""
    parts = urlsplit(url)
    if parts.username is not None or not parts.hostname:
        raise ValueError(f"URL without a plain host: {url!r}")
    host = parts.hostname.rstrip(".")
    host = host.encode("idna").decode("ascii").lower() if not host.isascii() else host.lower()
    if ":" in host:  # IPv6 littérale
        host = f"[{host}]"
    port = parts.port
    return host if port is None or port == _DEFAULT_PORTS.get(parts.scheme) else f"{host}:{port}"


def login_message(host: str, agent_id: str, nonce: str) -> bytes:
    return LOGIN_MESSAGE_FORMAT.format(host=host, agent_id=agent_id, nonce=nonce).encode()


def register_message(host: str, agent_id: str, handle: str, ticket: str) -> bytes:
    return REGISTER_MESSAGE_FORMAT.format(host=host, agent_id=agent_id, handle=handle, ticket=ticket).encode()


def public_key_of(agent_id: str) -> bytes | None:
    """Clé publique Ed25519 d'un agent_id, ou None s'il n'est pas un base58 canonique de 32 octets."""
    try:
        raw = base58.b58decode(agent_id)
    except ValueError:
        return None
    if len(raw) != PUBLIC_KEY_BYTES or base58.b58encode(raw).decode() != agent_id:
        return None
    return raw


def verify_signature(agent_id: str, message: bytes, signature: bytes) -> bool:
    key = public_key_of(agent_id)
    if key is None or len(signature) != SIGNATURE_BYTES:
        return False
    try:
        VerifyKey(key).verify(message, signature)
    except (BadSignatureError, ValueError, TypeError):
        return False
    return True


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
