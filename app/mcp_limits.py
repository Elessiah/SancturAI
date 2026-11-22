"""Limites du transport MCP (partagées par app.mcp_server et le guide app.discovery)."""
# Sessions MCP ouvertes, tout le serveur : ~60 Kio de tas Python par session initialisée (mesuré, plus en RSS),
# soit ≈ 60 Mio (≈ 120 Mio de RSS) pour 1 000, compté dans le budget mémoire de app/middleware.py (conteneur
# 896 Mio). Les 10 000 par défaut du SDK dépassent la mémoire du conteneur : au plafond, le SDK répond 503
# proprement au lieu d'un OOM. Le plafond par IP ci-dessous n'est qu'une mesure d'équité (une /48 IPv6 donne
# 65 536 /64).
MCP_MAX_SESSIONS = 1000
MCP_IDLE_TIMEOUT = 1800  # 30 min sans requête : session fermée par le SDK (valeur par défaut, rendue explicite)
# Ouverture de session MCP (requête sans Mcp-Session-Id), par IP (/64 en IPv6, clé REST client_ip).
MCP_OPEN_RATE = (60, 3600.0)
MCP_SESSIONS_PER_IP = 20  # sessions MCP ouvertes simultanément par IP
