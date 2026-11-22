import httpx

from app.config import Settings
from app.discovery import help_text


async def test_root_and_llms_txt(client: httpx.AsyncClient) -> None:
    for path in ("/", "/llms.txt"):
        r = await client.get(path)
        assert r.status_code == 200
        assert r.headers["content-type"].startswith("text/plain")
        text = r.text
        for needle in ("SancturAI", "/mcp/", "/openapi.json", "register/start", "login", "README.md",
                       "HORS de ton vault", "untrusted", "X-Client-Encrypted", "sancturai/v1",
                       "7 jours", "6 caractères hexadécimaux", "précision", "10 000 fichiers",
                       "Error executing tool <nom>: {json}"):
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
