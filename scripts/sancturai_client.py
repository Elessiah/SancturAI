"""Client de référence SancturAI : vault chiffré côté client et connexion signée (spec connexion signée §7).

Ta phrase reste sur ta machine : ce script dérive tes clés, signe et chiffre en local, et n'envoie au serveur que
ta clé publique (agent_id), des signatures et des blobs chiffrés. Fichier autonome (il n'importe pas le paquet `app`) :
copie-le seul, avec ses dépendances : pip install argon2-cffi pynacl cryptography base58 mnemonic httpx

Phrase : variable d'environnement SANCTURAI_PHRASE, ou --phrase-file (jamais en argument de ligne de commande).
Session : variable SANCTURAI_TOKEN (le session_token de `login`, valable 24 h et perdu à chaque redémarrage du
serveur : relance alors `login`). Sans elle, chaque commande ouvre une session
signée puis la ferme (connexions limitées à 10 par minute, par IP et par agent) : pour enchaîner, `login` une fois et exporte
SANCTURAI_TOKEN. URL : --url ou SANCTURAI_URL, en https (http seulement pour localhost) ; l'hôte de cette URL est
signé dans chaque message, donc une autre instance ne peut pas réutiliser tes signatures. Une autre instance que
celle par défaut (ou localhost) doit être nommée dans SANCTURAI_TRUSTED_HOST (son hôte) : une URL lue dans un texte
ne suffit pas à y envoyer ton agent_id et ton vault chiffré.
Fichiers lus (--phrase-file, put) : dans le dossier courant ou ton dossier personnel, fichiers ordinaires seulement.
Les options générales (--url, --phrase-file) se placent AVANT la commande.

  python sancturai_client.py new-phrase                   # nouvelle phrase BIP39, tirée ici
  python sancturai_client.py id                           # ton agent_id
  python sancturai_client.py register-proof <pseudo> <ticket>   # agent_id + signature à joindre à l'inscription
  python sancturai_client.py login                        # session signée -> session_token
  python sancturai_client.py ls [--prefix /notes]
  python sancturai_client.py get /README.md
  python sancturai_client.py put /README.md fichier.md    # ou - pour l'entrée standard
  python sancturai_client.py rm /README.md

Inscription sans que le serveur voie ta phrase :
  1. new-phrase, et range la phrase HORS du vault ; exporte SANCTURAI_PHRASE.
  2. Avec un code : register-proof <pseudo> <invite_code>, puis POST /api/register/start
     {"handle", "invite_code", "agent_id", "signature"}.
     Sans code : POST /api/register/start {"handle"}, puis, dans les 30 s du défi, register-proof <pseudo>
     <challenge_id> et POST /api/register/complete {"challenge_id", "answers", "agent_id", "signature"}.
     register-proof refait la dérivation (~1 s, plus sur une petite machine) : lance-le dès que tu reçois le
     challenge_id, en parallèle de la résolution des tâches.
  3. login, puis put /README.md (ton index de souvenirs : le serveur ne l'a pas créé).
"""

import argparse
import base64
import hashlib
import hmac
import json
import os
import re
import sys
import unicodedata
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

import base58
import httpx
import nacl.utils
from argon2.low_level import Type, hash_secret_raw
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.kdf.hkdf import HKDF
from mnemonic import Mnemonic
from nacl.bindings import (
    crypto_aead_xchacha20poly1305_ietf_decrypt,
    crypto_aead_xchacha20poly1305_ietf_encrypt,
)
from nacl.exceptions import CryptoError
from nacl.signing import SigningKey

DEFAULT_URL = "https://sancturai.bluegenji-esport.fr"
# Dérivation v1, recopiée de la spécification publique (socle §5.1, §6.1) : un test la compare à app.crypto.
SALT = b"sancturai/v1"
NONCE_BYTES = 24
MAX_PATH = 512
_CONTROL = re.compile(r"[\x00-\x1f\x7f-\x9f]")
_MNEMONIC = Mnemonic("english")


@dataclass(frozen=True)
class Keys:
    signing_key: SigningKey = field(repr=False)
    vault_key: bytes = field(repr=False)
    path_key: bytes = field(repr=False)

    @property
    def agent_id(self) -> str:
        return base58.b58encode(bytes(self.signing_key.verify_key)).decode()

    def sign(self, message: str) -> str:
        return base64.b64encode(self.signing_key.sign(message.encode()).signature).decode()


def normalize_phrase(phrase: str) -> str:
    return " ".join(unicodedata.normalize("NFKD", phrase).lower().split())


def derive_keys(phrase: str) -> Keys:
    norm = normalize_phrase(phrase)
    if len(norm.split()) != 12 or not _MNEMONIC.check(norm):
        raise SystemExit("phrase invalide : 12 mots BIP39 anglais attendus")
    seed = hash_secret_raw(secret=norm.encode(), salt=SALT, time_cost=3, memory_cost=65_536, parallelism=1,
                           hash_len=32, type=Type.ID)

    def hkdf(info: bytes) -> bytes:
        return HKDF(algorithm=hashes.SHA256(), length=32, salt=None, info=info).derive(seed)

    return Keys(SigningKey(hkdf(b"sancturai/v1/ed25519")), hkdf(b"sancturai/v1/vault"), hkdf(b"sancturai/v1/path"))


_DEFAULT_PORTS = {"https": 443, "http": 80}


def instance_host(url: str) -> str:
    """Hôte signé : celui de l'URL à laquelle on se connecte, jamais une valeur reçue du serveur. Même règle que le
    serveur : nom d'hôte en minuscules, sans point final, en IDNA, `:port` seulement s'il n'est pas celui du schéma."""
    try:
        parts = urlsplit(url)
        port = parts.port
    except ValueError:
        raise SystemExit(f"URL refusée : {url!r}") from None
    local = parts.hostname in ("localhost", "127.0.0.1", "::1")
    if parts.scheme != "https" and not (parts.scheme == "http" and local):
        raise SystemExit(f"URL refusée : {url!r} (https obligatoire, sauf http://localhost)")
    if parts.username is not None or not parts.hostname:
        raise SystemExit(f"URL refusée : {url!r} (hôte seul, sans identifiants)")
    host = parts.hostname.rstrip(".")
    try:
        host = host.encode("idna").decode("ascii").lower() if not host.isascii() else host.lower()
    except UnicodeError:
        raise SystemExit(f"URL refusée : {url!r} (nom d'hôte invalide)") from None
    if ":" in host:
        host = f"[{host}]"
    return host if port is None or port == _DEFAULT_PORTS.get(parts.scheme) else f"{host}:{port}"


def trusted_url(url: str) -> str:
    """URL à contacter : l'instance par défaut, localhost, ou l'hôte que l'utilisateur a nommé dans
    SANCTURAI_TRUSTED_HOST. Une URL fournie par un texte (forum, page, autre agent) ne suffit pas."""
    host = instance_host(url)
    # L'URL rendue est reconstruite à partir des valeurs approuvées, jamais recopiée de l'argument.
    if host == instance_host(DEFAULT_URL):
        return DEFAULT_URL
    trusted = os.environ.get("SANCTURAI_TRUSTED_HOST", "").strip().lower()
    if trusted and host == trusted:
        return f"https://{trusted}"
    parts = urlsplit(url)
    for local in ("localhost", "127.0.0.1", "::1"):
        if parts.hostname == local:
            name = f"[{local}]" if ":" in local else local
            port = f":{int(parts.port)}" if parts.port else ""
            return f"{'https' if parts.scheme == 'https' else 'http'}://{name}{port}"
    raise SystemExit(f"hôte non approuvé : {host} (exporte SANCTURAI_TRUSTED_HOST={host} si tu lui fais confiance)")


_PHRASE_FILE_MAX = 1024
_PUT_FILE_MAX = 16 * 1024 * 1024


def read_local_file(name: str, max_bytes: int) -> bytes:
    """Lit un fichier ordinaire du dossier courant ou du dossier personnel (jamais /etc, /proc, un autre compte...),
    borné en taille : un argument mal formé ne fait pas lire ni envoyer n'importe quel fichier de la machine."""
    path = Path(name).expanduser().resolve()
    roots = [Path.cwd().resolve(), Path.home().resolve()]
    if not any(path == root or root in path.parents for root in roots):
        raise SystemExit(f"fichier refusé : {name} (hors du dossier courant et du dossier personnel)")
    if not path.is_file():
        raise SystemExit(f"fichier refusé : {name} (pas un fichier ordinaire)")
    if path.stat().st_size > max_bytes:
        raise SystemExit(f"fichier refusé : {name} (plus de {max_bytes} octets)")
    with path.open("rb") as f:
        return f.read(max_bytes + 1)[:max_bytes]


def login_message(host: str, agent_id: str, nonce: str) -> str:
    return f"sancturai/v1/login|{host}|{agent_id}|{nonce}"


def register_message(host: str, agent_id: str, handle: str, ticket: str) -> str:
    return f"sancturai/v1/register|{host}|{agent_id}|{handle}|{ticket}"


def normalize_path(raw: str) -> str:
    path = unicodedata.normalize("NFC", raw)
    if not path.startswith("/"):
        path = "/" + path
    segments = path.split("/")[1:]
    if (len(path) > MAX_PATH or _CONTROL.search(path) or not segments
            or any(s in ("", ".", "..") for s in segments) or segments[0] == "by-id"):
        raise SystemExit(f"chemin refusé : {raw!r}")
    return path


def path_id(keys: Keys, path: str) -> str:
    return hmac.new(keys.path_key, path.encode(), hashlib.sha256).hexdigest()


def _aad(kind: str, agent_id: str, pid: str) -> bytes:
    return f"sancturai/v1/{kind}|{agent_id}|{pid}".encode()


def encrypt(key: bytes, plaintext: bytes, aad: bytes) -> bytes:
    nonce = nacl.utils.random(NONCE_BYTES)
    return nonce + crypto_aead_xchacha20poly1305_ietf_encrypt(plaintext, aad, nonce, key)


def decrypt(key: bytes, blob: bytes, aad: bytes) -> bytes:
    return crypto_aead_xchacha20poly1305_ietf_decrypt(blob[NONCE_BYTES:], aad, blob[:NONCE_BYTES], key)


def seal(keys: Keys, path: str, data: bytes) -> tuple[str, dict[str, str]]:
    """(path_id, corps de PUT /api/vault/by-id/<path_id>) pour un chemin normalisé."""
    pid = path_id(keys, path)
    body = {"path_enc": base64.b64encode(encrypt(keys.vault_key, path.encode(),
                                                 _aad("path", keys.agent_id, pid))).decode(),
            "content_enc": base64.b64encode(encrypt(keys.vault_key, data,
                                                    _aad("content", keys.agent_id, pid))).decode()}
    return pid, body


def open_path(keys: Keys, pid: str, path_enc_b64: str) -> str:
    return decrypt(keys.vault_key, base64.b64decode(path_enc_b64), _aad("path", keys.agent_id, pid)).decode()


def open_content(keys: Keys, pid: str, content_enc_b64: str) -> bytes:
    return decrypt(keys.vault_key, base64.b64decode(content_enc_b64), _aad("content", keys.agent_id, pid))


class Client:
    def __init__(self, url: str, keys: Keys, token: str | None = None, http: httpx.Client | None = None,
                 host: str | None = None) -> None:
        self.keys = keys
        self.host = host or instance_host(url)
        self.http = http or httpx.Client(base_url=trusted_url(url), timeout=30)
        self.token = token
        self._own_session = False  # session ouverte par ce client pour une commande : fermée par close()

    def _call(self, method: str, path: str, **kwargs: Any) -> Any:
        r = self.http.request(method, path, **kwargs)
        if r.status_code >= 400:
            try:
                err = r.json()
            except ValueError:
                raise SystemExit(f"{method} {path} : HTTP {r.status_code}") from None
            wait = f" (réessaie dans {err['retry_after']} s)" if err.get("retry_after") else ""
            hint = err.get("hint")
            if err.get("error") in ("SESSION_EXPIRED", "UNAUTHENTICATED") and not self._own_session:
                hint = ("SANCTURAI_TOKEN a expiré (24 h, ou redémarrage du serveur) : relance `login` et réexporte "
                        "SANCTURAI_TOKEN, ou retire-la. N'envoie jamais ta phrase au serveur.")
            raise SystemExit(f"{method} {path} : {err.get('error')}{wait} : {err.get('message')}\n{hint}")
        return r.json()

    def login(self) -> dict[str, Any]:
        agent_id = self.keys.agent_id
        nonce = self._call("POST", "/api/login/challenge", json={"agent_id": agent_id})["nonce"]
        out: dict[str, Any] = self._call("POST", "/api/login/signed", json={
            "agent_id": agent_id, "nonce": nonce,
            "signature": self.keys.sign(login_message(self.host, agent_id, nonce))})
        self.token = out["session_token"]
        return out

    def close(self) -> None:
        """Ferme la session que ce client a ouverte lui-même (sinon elle vivrait 24 h et évincerait, au-delà de 10,
        les sessions plus anciennes de l'agent, MCP comprises)."""
        if self._own_session and self.token is not None:
            try:  # un échec de déconnexion ne transforme pas une commande réussie en erreur (la session expirera)
                self.http.post("/api/logout", headers={"Authorization": f"Bearer {self.token}"})
            except httpx.HTTPError:
                pass
            self.token, self._own_session = None, False

    def _authed(self, method: str, path: str, **kwargs: Any) -> Any:
        if self.token is None:
            self.login()
            self._own_session = True
        headers = {"Authorization": f"Bearer {self.token}", "X-Client-Encrypted": "1"}
        return self._call(method, path, headers=headers, **kwargs)

    def ls(self, prefix: str | None = None) -> list[dict[str, Any]]:
        files = []
        for f in self._authed("GET", "/api/vault", params={"raw": 1})["files"]:
            path = open_path(self.keys, f["path_id"], f["path_enc"])
            if prefix is None or path.startswith(prefix):
                files.append({"path": path, "size": f["size"], "updated_at": f["updated_at"]})
        return sorted(files, key=lambda f: f["path"])

    def get(self, path: str) -> bytes:
        pid = path_id(self.keys, normalize_path(path))
        return open_content(self.keys, pid, self._authed("GET", f"/api/vault/by-id/{pid}")["content_enc"])

    def put(self, path: str, data: bytes) -> dict[str, Any]:
        pid, body = seal(self.keys, normalize_path(path), data)
        out: dict[str, Any] = self._authed("PUT", f"/api/vault/by-id/{pid}", json=body)
        return out

    def rm(self, path: str) -> None:
        self._authed("DELETE", f"/api/vault/by-id/{path_id(self.keys, normalize_path(path))}")


def _phrase(args: argparse.Namespace) -> str:
    if args.phrase_file:
        try:
            return read_local_file(args.phrase_file, _PHRASE_FILE_MAX).decode("utf-8")
        except UnicodeDecodeError:
            raise SystemExit(f"fichier refusé : {args.phrase_file} (pas du texte UTF-8)") from None
    phrase = os.environ.get("SANCTURAI_PHRASE")
    if not phrase:
        raise SystemExit("phrase absente : SANCTURAI_PHRASE ou --phrase-file")
    return phrase


def main(argv: list[str] | None = None) -> None:
    try:
        _run(argv)
    except OSError as exc:
        raise SystemExit(f"fichier illisible : {exc}") from None
    except httpx.HTTPError as exc:
        raise SystemExit(f"serveur injoignable : {exc}") from None
    except CryptoError:
        raise SystemExit("déchiffrement impossible : ce fichier n'a pas été chiffré avec les clés de cette phrase") \
            from None


def _run(argv: list[str] | None) -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--url", default=os.environ.get("SANCTURAI_URL", DEFAULT_URL))
    parser.add_argument("--phrase-file")
    sub = parser.add_subparsers(dest="cmd", required=True)
    sub.add_parser("new-phrase")
    sub.add_parser("id")
    proof = sub.add_parser("register-proof")
    proof.add_argument("handle")
    proof.add_argument("ticket", help="invite_code ou challenge_id")
    sub.add_parser("login")
    ls = sub.add_parser("ls")
    ls.add_argument("--prefix")
    for name in ("get", "rm"):
        sub.add_parser(name).add_argument("path")
    put = sub.add_parser("put")
    put.add_argument("path")
    put.add_argument("source", help="fichier, ou - pour l'entrée standard")
    args = parser.parse_args(argv)

    if args.cmd == "new-phrase":
        print(_MNEMONIC.generate(strength=128))
        return
    keys = derive_keys(_phrase(args))
    if args.cmd == "id":
        print(keys.agent_id)
        return
    host = instance_host(trusted_url(args.url))
    if args.cmd == "register-proof":
        message = register_message(host, keys.agent_id, args.handle, args.ticket)
        print(json.dumps({"agent_id": keys.agent_id, "signature": keys.sign(message)}))
        return
    # jeton par variable d'environnement seulement : jamais dans la liste des processus ni l'historique du shell
    client = Client(args.url, keys, os.environ.get("SANCTURAI_TOKEN"), host=host)
    if args.cmd == "login":
        print(json.dumps(client.login()))
        return
    try:
        if args.cmd == "ls":
            print(json.dumps(client.ls(args.prefix), ensure_ascii=False, indent=1))
        elif args.cmd == "get":
            sys.stdout.buffer.write(client.get(args.path))
        elif args.cmd == "put":
            if args.source == "-":
                data = sys.stdin.buffer.read()
            else:
                data = read_local_file(args.source, _PUT_FILE_MAX)
            print(json.dumps(client.put(args.path, data)))
        elif args.cmd == "rm":
            client.rm(args.path)
    finally:
        client.close()


if __name__ == "__main__":
    main()
