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
                       "7 jours", "6 caractères hexadécimaux", "précision", "10 000 fichiers"):
            assert needle in text, needle
        assert "<html" not in text.lower()


def test_help_text_uses_public_url() -> None:
    text = help_text(Settings(public_url="https://example.test/"))  # type: ignore[call-arg]
    assert "https://example.test/mcp/" in text
    assert "https://example.test//" not in text
