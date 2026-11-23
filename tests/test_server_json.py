import json
import tomllib
from pathlib import Path

from app import __version__
from app.config import Settings

ROOT = Path(__file__).resolve().parents[1]


def test_server_json_matches_config() -> None:
    data = json.loads((ROOT / "server.json").read_text(encoding="utf-8"))
    public_url = Settings.model_fields["public_url"].default.rstrip("/")
    assert data["$schema"].startswith("https://static.modelcontextprotocol.io/schemas/")
    assert data["name"] == "io.github.Elessiah/sancturai"
    assert data["version"] == __version__
    assert data["remotes"] == [{"type": "streamable-http", "url": f"{public_url}/mcp/"}]
    assert 0 < len(data["description"]) <= 100  # limite du registre


def test_version_has_a_single_source() -> None:
    pyproject = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))
    assert pyproject["project"]["version"] == __version__
