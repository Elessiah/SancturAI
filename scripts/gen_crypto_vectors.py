"""Génère tests/vectors/crypto_v1.json. Le fichier est figé dans git : une régénération ne peut qu'AJOUTER
des champs (toutes les valeurs sont déterministes ; vérifier au diff que les champs existants sont inchangés)."""

import base64
import json
from pathlib import Path

from nacl.bindings import crypto_aead_xchacha20poly1305_ietf_encrypt

from app.crypto import derive_keys, path_id

PHRASE = "abandon abandon abandon abandon abandon abandon abandon abandon abandon abandon abandon about"
NONCE = bytes(range(24))
keys = derive_keys(PHRASE)
blob = NONCE + crypto_aead_xchacha20poly1305_ietf_encrypt(b"hello sanctuary", b"aad", NONCE, keys.vault_key)

# Vault v1 (spec §6.1). Les AAD sont écrites ici en toutes lettres depuis la spec, PAS importées de app.crypto :
# les tests comparent ainsi le module à une référence indépendante.
pid = path_id(keys.path_key, "/README.md")
aad_path = f"sancturai/v1/path|{keys.agent_id}|{pid}"
aad_content = f"sancturai/v1/content|{keys.agent_id}|{pid}"
README = "# vecteur v1\n"
PATH_NONCE, CONTENT_NONCE = bytes(range(24, 48)), bytes(range(48, 72))
path_enc = PATH_NONCE + crypto_aead_xchacha20poly1305_ietf_encrypt(
    b"/README.md", aad_path.encode(), PATH_NONCE, keys.vault_key)
content_enc = CONTENT_NONCE + crypto_aead_xchacha20poly1305_ietf_encrypt(
    README.encode(), aad_content.encode(), CONTENT_NONCE, keys.vault_key)

# Messages signés (spec connexion signée §3), eux aussi écrits ici en toutes lettres ; Ed25519 est déterministe.
HOST, LOGIN_NONCE, REG_HANDLE, REG_TICKET = "vecteur.sancturai.test", "nonce-vecteur-v1", "vecteur", "ticket-v1"
login_msg = f"sancturai/v1/login|{HOST}|{keys.agent_id}|{LOGIN_NONCE}"
register_msg = f"sancturai/v1/register|{HOST}|{keys.agent_id}|{REG_HANDLE}|{REG_TICKET}"


def sign_b64(message: str) -> str:
    return base64.b64encode(keys.signing_key.sign(message.encode()).signature).decode()


out = {
    "phrase": PHRASE,
    "agent_id": keys.agent_id,
    "vault_key_hex": keys.vault_key.hex(),
    "path_key_hex": keys.path_key.hex(),
    "path_id_readme": pid,
    "blob_hex": blob.hex(),
    "blob_aad": "aad",
    "blob_plaintext": "hello sanctuary",
    "aad_path_readme": aad_path,
    "aad_content_readme": aad_content,
    "readme_path_enc_hex": path_enc.hex(),
    "readme_content_enc_hex": content_enc.hex(),
    "readme_content": README,
    "signed_host": HOST,
    "login_nonce": LOGIN_NONCE,
    "login_message": login_msg,
    "login_signature_b64": sign_b64(login_msg),
    "register_handle": REG_HANDLE,
    "register_ticket": REG_TICKET,
    "register_message": register_msg,
    "register_signature_b64": sign_b64(register_msg),
}
Path("tests/vectors").mkdir(parents=True, exist_ok=True)
Path("tests/vectors/crypto_v1.json").write_text(json.dumps(out, indent=2) + "\n", newline="\n")
print(json.dumps(out, indent=2))
