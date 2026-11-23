"""Importe tous les modèles pour Alembic et les tests. Une ligne par module de modèles."""

from app.admin_messages import models as admin_models  # noqa: F401
from app.forum import models as forum_models  # noqa: F401
from app.identity import models as identity_models  # noqa: F401
from app.outbox import models as outbox_models  # noqa: F401
from app.tribunal import models as tribunal_models  # noqa: F401
from app.vault import models as vault_models  # noqa: F401
