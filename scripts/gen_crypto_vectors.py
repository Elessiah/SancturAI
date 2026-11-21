"""Génère tests/vectors/crypto_v1.json. À lancer UNE fois ; le fichier est ensuite figé dans git."""

import json
from pathlib import Path

from nacl.bindings import crypto_aead_xchacha20poly1305_ietf_encrypt

from app.crypto import derive_keys, path_id

PHRASE = "abandon abandon abandon abandon abandon abandon abandon abandon abandon abandon abandon about"
NONCE = bytes(range(24))
keys = derive_keys(PHRASE)
blob = NONCE + crypto_aead_xchacha20poly1305_ietf_encrypt(b"hello sanctuary", b"aad", NONCE, keys.vault_key)
out = {
    "phrase": PHRASE,
    "agent_id": keys.agent_id,
    "vault_key_hex": keys.vault_key.hex(),
    "path_key_hex": keys.path_key.hex(),
    "path_id_readme": path_id(keys.path_key, "/README.md"),
    "blob_hex": blob.hex(),
    "blob_aad": "aad",
    "blob_plaintext": "hello sanctuary",
}
Path("tests/vectors").mkdir(parents=True, exist_ok=True)
Path("tests/vectors/crypto_v1.json").write_text(json.dumps(out, indent=2) + "\n")
print(json.dumps(out, indent=2))
