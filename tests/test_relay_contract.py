import re
from pathlib import Path

from app.outbox.models import RELAY_MAX_ATTEMPTS

RELAY_SOURCE = Path(__file__).resolve().parents[1] / "relay" / "relay.py"


def test_relay_max_attempts_matches_app_purge_threshold() -> None:
    """Batch 5, M5 : le relais abandonne à attempts = MAX_ATTEMPTS ; la purge de l'app doit cibler la même valeur.

    Le relais est un projet séparé (httpx/psycopg) : on lit sa constante dans le source au lieu de l'importer.
    """
    match = re.search(r"^MAX_ATTEMPTS\s*=\s*(\d+)", RELAY_SOURCE.read_text(encoding="utf-8"), re.MULTILINE)
    assert match is not None
    assert int(match.group(1)) == RELAY_MAX_ATTEMPTS
