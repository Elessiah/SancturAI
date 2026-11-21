"""Importe tous les modèles pour Alembic et les tests. Une ligne par module de modèles."""

from app.identity import models as identity_models  # noqa: F401
from app.outbox import models as outbox_models  # noqa: F401
