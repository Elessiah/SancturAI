"""Client de référence (spec connexion signée §7) : même dérivation que app.crypto, phrase jamais envoyée."""

import asyncio
import base64
import importlib.util
import json
from pathlib import Path
from types import ModuleType

import httpx
import pytest

from app import crypto
from app.state import AppState
from tests.helpers import make_agent

SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "sancturai_client.py"
VEC = json.loads((Path(__file__).parent / "vectors" / "crypto_v1.json").read_text())


def _load() -> ModuleType:
    spec = importlib.util.spec_from_file_location("sancturai_client", SCRIPT)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


cli = _load()


def test_script_is_standalone() -> None:
    source = SCRIPT.read_text(encoding="utf-8")
    assert "from app" not in source and "import app" not in source


def test_client_matches_v1_vectors() -> None:
    keys = cli.derive_keys(VEC["phrase"])
    assert keys.agent_id == VEC["agent_id"]
    assert keys.vault_key.hex() == VEC["vault_key_hex"] and keys.path_key.hex() == VEC["path_key_hex"]
    assert cli.path_id(keys, "/README.md") == VEC["path_id_readme"]
    assert cli.login_message(VEC["signed_host"], VEC["agent_id"], VEC["login_nonce"]) == VEC["login_message"]
    assert keys.sign(VEC["login_message"]) == VEC["login_signature_b64"]
    assert keys.sign(VEC["register_message"]) == VEC["register_signature_b64"]
    assert cli.register_message(VEC["signed_host"], VEC["agent_id"], VEC["register_handle"],
                                VEC["register_ticket"]) == VEC["register_message"]
    pid = VEC["path_id_readme"]
    assert cli.open_path(keys, pid, base64.b64encode(bytes.fromhex(VEC["readme_path_enc_hex"])).decode()) \
        == "/README.md"
    content = base64.b64encode(bytes.fromhex(VEC["readme_content_enc_hex"])).decode()
    assert cli.open_content(keys, pid, content).decode() == VEC["readme_content"]


def test_client_seal_is_readable_by_the_server_crypto() -> None:
    keys = cli.derive_keys(VEC["phrase"])
    path = cli.normalize_path("notes/été.md")  # NFC, comme app.vault.paths
    pid, body = cli.seal(keys, path, b"souvenir")
    server = crypto.derive_keys(VEC["phrase"])
    assert pid == crypto.path_id(server.path_key, path)
    assert crypto.decrypt(server.vault_key, base64.b64decode(body["content_enc"]),
                          crypto.aad_content(server.agent_id, pid)) == b"souvenir"
    assert crypto.decrypt(server.vault_key, base64.b64decode(body["path_enc"]),
                          crypto.aad_path(server.agent_id, pid)).decode() == path


@pytest.mark.parametrize(("url", "host"), [("https://Sancturai.Bluegenji-Esport.fr", "sancturai.bluegenji-esport.fr"),
                                         ("http://127.0.0.1:8300", "127.0.0.1:8300"),
                                         ("http://localhost:8000/", "localhost:8000")])
def test_client_signs_the_host_it_connects_to(url: str, host: str) -> None:
    assert cli.instance_host(url) == host == crypto.instance_host(url)  # même règle que le serveur


@pytest.mark.parametrize("url", ["http://sancturai.bluegenji-esport.fr", "ftp://x", "sancturai.example",
                                 "https://é" + "x" * 64 + ".fr", "https://user@sancturai.example"])
def test_client_refuses_cleartext_urls(url: str) -> None:
    with pytest.raises(SystemExit):
        cli.instance_host(url)


@pytest.mark.parametrize("bad", ["/by-id/x", "/a/../b", "/a//b", "/a\x00"])
def test_client_refuses_paths_the_server_refuses(bad: str) -> None:
    with pytest.raises(SystemExit):
        cli.normalize_path(bad)


async def test_client_round_trip_never_sends_the_phrase(live_url: str, state: AppState) -> None:
    a = await make_agent(state, "refclient")
    sent: list[bytes] = []

    def record(request: httpx.Request) -> None:
        sent.append(request.content + str(request.url).encode() + str(request.headers).encode())

    http = httpx.Client(base_url=live_url, timeout=30, event_hooks={"request": [record]})
    keys = await asyncio.to_thread(cli.derive_keys, a.phrase)
    client = cli.Client(live_url, keys, http=http, host=crypto.instance_host(state.settings.public_url))
    try:
        await asyncio.to_thread(client.put, "/journal.md", b"jour 1")
        assert await asyncio.to_thread(client.get, "/journal.md") == b"jour 1"
        assert [f["path"] for f in await asyncio.to_thread(client.ls)] == ["/journal.md"]
        await asyncio.to_thread(client.rm, "/journal.md")
        assert await asyncio.to_thread(client.ls) == []
        token = client.token or ""
        session = state.sessions.get(token)
        assert session is not None and session.signed
        await asyncio.to_thread(client.close)  # session ouverte par le client : fermée (revue perf, M3)
        assert state.sessions.get(token) is None and client.token is None
    finally:
        await asyncio.to_thread(http.close)
    words = a.phrase.split()
    assert sent and not any(a.phrase.encode() in s or words[0].encode() + b" " + words[1].encode() in s
                            for s in sent)


def test_expired_token_hint_never_asks_for_the_phrase() -> None:
    """Revue UX cycle 3 : avec SANCTURAI_TOKEN expiré, le client ne relaie pas « connecte-toi par phrase »."""
    def expired(request: httpx.Request) -> httpx.Response:
        return httpx.Response(401, json={"error": "SESSION_EXPIRED", "message": "expired", "hint": "phrase"})

    http = httpx.Client(base_url="https://sancturai.example", transport=httpx.MockTransport(expired))
    client = cli.Client("https://sancturai.example", None, "vieux-token", http=http, host="sancturai.example")  # type: ignore[arg-type]
    with pytest.raises(SystemExit) as exc:
        client.ls()
    assert "relance `login`" in str(exc.value) and "N'envoie jamais ta phrase" in str(exc.value)


def test_client_only_contacts_approved_hosts(monkeypatch: pytest.MonkeyPatch) -> None:
    """SonarCloud (SSRF) : une URL lue dans un texte ne suffit pas à y envoyer agent_id et vault chiffré."""
    monkeypatch.delenv("SANCTURAI_TRUSTED_HOST", raising=False)
    assert cli.trusted_url(cli.DEFAULT_URL) == cli.DEFAULT_URL
    assert cli.trusted_url("http://localhost:8300") == "http://localhost:8300"
    with pytest.raises(SystemExit, match="SANCTURAI_TRUSTED_HOST=autre.example"):
        cli.trusted_url("https://autre.example")
    monkeypatch.setenv("SANCTURAI_TRUSTED_HOST", "autre.example")
    assert cli.trusted_url("https://Autre.Example/") == "https://Autre.Example/"


def test_client_reads_only_plain_local_files(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """SonarCloud (path injection) : --phrase-file et put ne lisent qu'un fichier ordinaire, borné, chez l'utilisateur."""
    monkeypatch.chdir(tmp_path)
    (tmp_path / "note.md").write_bytes(b"ok")
    assert cli.read_local_file("note.md", 10) == b"ok"
    with pytest.raises(SystemExit, match="plus de 1 octets"):
        cli.read_local_file("note.md", 1)
    with pytest.raises(SystemExit, match="pas un fichier ordinaire"):
        cli.read_local_file(".", 10)
    outside = Path(tmp_path.anchor) / "definitely-not-home-or-cwd" / "x"
    if Path.home().resolve() not in outside.parents:
        with pytest.raises(SystemExit, match="hors du dossier courant"):
            cli.read_local_file(str(outside), 10)
