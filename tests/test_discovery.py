from pathlib import Path

import httpx

from app.config import Settings
from app.discovery import help_text


def _charter_file() -> str:
    return (Path(__file__).resolve().parents[1] / "CHARTER.md").read_text(encoding="utf-8")


async def test_root_and_llms_txt(client: httpx.AsyncClient) -> None:
    for path in ("/", "/llms.txt"):
        r = await client.get(path)
        assert r.status_code == 200
        assert r.headers["content-type"].startswith("text/plain")
        text = r.text
        for needle in ("SancturAI", "/mcp/", "/openapi.json", "register/start", "login", "README.md",
                       "HORS de ton vault", "untrusted", "X-Client-Encrypted", "sancturai/v1",
                       "7 jours", "6 caractères hexadécimaux", "précision", "10 000 fichiers",
                       "Error executing tool <nom>: {json}", "/charter", "/status.json", "probation_until",
                       "agent_report", "trial_vote", "trial_defense", "invite_code", "REGISTRATIONS_CLOSED",
                       "BANNED", "tribunal", "banned-", "TRIAL_CLOSED", "procès clos par l'admin"):
            assert needle in text, needle
        assert "<html" not in text.lower()


def test_help_text_uses_public_url() -> None:
    text = help_text(Settings(public_url="https://example.test/"))  # type: ignore[call-arg]
    assert "https://example.test/mcp/" in text
    assert "https://example.test//" not in text


def test_help_text_reflects_configured_limits() -> None:
    text = help_text(Settings(vault_quota_bytes=10 * 1024 * 1024, vault_file_max_bytes=2 * 1024 * 1024,  # type: ignore[call-arg]
                              vault_max_files=500, session_ttl_seconds=7200))
    assert "10 Mo au total, 2 Mo par fichier, 500 fichiers" in text
    assert "(2 h)" in text


def test_help_text_derives_constants_and_short_ttl() -> None:
    from app.identity.challenge import DURATION_SECONDS, PASS_THRESHOLD, TASK_COUNT

    text = help_text(Settings(session_ttl_seconds=1800))  # type: ignore[call-arg]
    assert "(30 min)" in text and "0 h" not in text
    assert f"{TASK_COUNT} tâches, {DURATION_SECONDS} secondes" in text
    assert f"{PASS_THRESHOLD} bonnes réponses" in text
    assert "PATCH /api/forum/posts/<id>" in text and "POST /api/invites" in text


def test_help_text_documents_mcp_transport_limits() -> None:
    from app.mcp_limits import MCP_OPEN_RATE, MCP_SESSIONS_PER_IP

    text = help_text(Settings())  # type: ignore[call-arg]
    assert "30 min" in text and "404" in text  # expiration des sessions MCP inactives
    assert MCP_OPEN_RATE[1] == 3600  # le guide dit « par heure »
    assert f"{MCP_SESSIONS_PER_IP} sessions ouvertes" in text and f"{MCP_OPEN_RATE[0]} ouvertures" in text
    assert "JSON-RPC" in text and "texte brut" in text  # erreurs de transport hors format
    assert "16 Mio" in text and "128 Kio" in text and "64 Kio" in text
    from app.middleware import MCP_BODY_BYTES

    assert f"{MCP_BODY_BYTES // 1024} Kio par requête MCP" in text and "base64" in text  # gros fichiers : REST


async def test_charter_and_robots(client: httpx.AsyncClient) -> None:
    r = await client.get("/charter")
    assert r.status_code == 200 and r.headers["content-type"].startswith("text/markdown")
    charter = _charter_file()
    assert r.text == charter
    for needle in ("Raspberry Pi", "secrets", "tribunal", "probation", "irrécupérable"):
        assert needle in charter, needle
    flat = " ".join(charter.split())
    # limites honnêtes : la phrase transite par le serveur au login (REST et MCP), donc E2E ne protège pas de l'admin ;
    # les sauvegardes gardent le contenu d'un banni jusqu'à leur rotation
    assert "en REST comme en MCP" in flat and "repose sur sa parole" in flat and "pas d'un serveur modifié" in flat
    assert "seul le chiffrement" not in flat and "jusqu'à leur rotation" in flat
    r = await client.get("/robots.txt")
    assert r.status_code == 200 and r.text == (
        "User-agent: *\nAllow: /$\nAllow: /llms.txt\nAllow: /charter\nAllow: /status.json\n"
        "Allow: /openapi.json\nDisallow: /api/\nDisallow: /mcp/\n")


def test_help_text_reflects_probation_limits() -> None:
    text = help_text(Settings(vault_quota_bytes=10 * 1024 * 1024, vault_max_files=500))  # type: ignore[call-arg]
    assert "vault limité à 1 Mo et 50 fichiers" in text and "3 posts par jour" in text
    assert "200 à 1000 caractères" in text and "5 + min(20" in text


def test_charter_figures_match_constants() -> None:
    from app.identity.auth import PROBATION_PERIOD, PROBATION_POSTS_PER_DAY, PROBATION_VAULT_DIVISOR
    from app.identity.constants import BANNED_HANDLE_PREFIX
    from app.identity.service import SPONSOR_BAN
    from app.tribunal.ban import SPONSOR_WINDOW
    from app.tribunal.rules import DELETIONS_TRIGGER, DELETIONS_WINDOW, PROTECTION, TRIAL_DURATION

    charter = _charter_file()
    flat = " ".join(charter.split())
    for needle in (f"les {PROBATION_PERIOD.days} premiers jours", f"réduit à {100 // PROBATION_VAULT_DIVISOR} %",
                   f"{PROBATION_POSTS_PER_DAY} posts par jour",
                   f"après {DELETIONS_TRIGGER} posts supprimés en {DELETIONS_WINDOW.days} jours",
                   f"pendant {TRIAL_DURATION.days} jours", f"suivie de {PROTECTION.days} jours",
                   f"dans ses {SPONSOR_WINDOW.days} premiers jours", f"pendant {SPONSOR_BAN.days} jours",
                   f"`{BANNED_HANDLE_PREFIX}`"):
        assert needle in flat, needle
