import asyncio
import json
import re
from dataclasses import dataclass

import anyio
from starlette.datastructures import Headers
from starlette.requests import Request
from starlette.responses import JSONResponse
from starlette.types import ASGIApp, Message, Receive, Scope, Send

from app.admin_messages.service import verify_internal
from app.errors import ApiError, error_response
from app.state import AppState

NO_STORE_PREFIXES = ("/api/", "/mcp")


class NoStoreMiddleware:
    """Cache-Control: no-store sur toute réponse /api/* et /mcp (phrase, token, contenu du vault...).
    ASGI pur : ne met pas en tampon les flux (SSE du MCP)."""

    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        path = scope.get("path", "") if scope["type"] == "http" else ""
        if not (path == "/api" or path.startswith(NO_STORE_PREFIXES)):
            await self.app(scope, receive, send)
            return

        async def _send(message: Message) -> None:
            if message["type"] == "http.response.start":
                headers = [(k, v) for k, v in message.get("headers", []) if k.lower() != b"cache-control"]
                message = {**message, "headers": [*headers, (b"cache-control", b"no-store")]}
            await send(message)

        await self.app(scope, receive, _send)


def _is_internal(path: str) -> bool:
    return path == "/internal" or path.startswith("/internal/")


def _paths(scope: Scope) -> tuple[str, str]:
    """Chemin brut et chemin vu par le routeur (sans root_path, comme starlette.get_route_path) :
    un root_path configuré (--root-path) ne doit pas faire contourner la garde."""
    path: str = scope.get("path", "")
    root: str = scope.get("root_path", "")
    if root and path.startswith(root + "/"):
        return path, path[len(root):]
    return path, path


class InternalGuardMiddleware:
    """Garde de tout /internal (routes actuelles et futures), AVANT le routage : sans token valide ou avec
    X-Forwarded-For, la réponse est celle d'une route inexistante (ni 405, ni 422 de décodage du corps)."""

    def __init__(self, app: ASGIApp, state: AppState) -> None:
        self.app = app
        self.state = state

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] == "http" and any(_is_internal(p) for p in _paths(scope)):
            try:
                verify_internal(Request(scope), self.state.settings)
            except ApiError as err:
                await error_response(err)(scope, receive, send)
                return
        await self.app(scope, receive, send)


KIB = 1024
MIB = 1024 * KIB
# Plafonds de corps par route, au plus juste du besoin réel. JSON échappé au pire : \u00XX = 6 octets par octet
# de contrôle C0 (3 par caractère C1), 6 pour < > & chez un client Go (échappement HTML), 12 par emoji.
# - écriture vault REST (PUT /api/vault/..., by-id) : fichier de 5 Mo en base64 ≈ 6,7 Mo ; en utf8 un texte courant
#   (≤ 3 octets échappés par octet) tient, un contenu riche en contrôles non : le guide conseille base64 ;
#   même plafond par route que nginx (deploy/nginx/sancturai.conf) ;
# - /mcp : 256 Kio (décidé par l'utilisateur) ; vault_write en MCP pour les petits fichiers, l'API REST au-delà ;
# - forum : corps ≤ 32 Ko en octets, soit ≤ 8 192 emoji × 12 ≈ 96 Kio échappés, + titre (un corps > 60 % de < > &
#   envoyé par un client Go peut dépasser : cas accepté) ;
# - ailleurs (login, inscription, messages admin ≤ 4 100 car. × 12 ≈ 48 Kio, déplacements...) : 64 Kio.
MAX_UPLOAD_BYTES = 16 * MIB
MCP_BODY_BYTES = 256 * KIB
FORUM_BODY_BYTES = 128 * KIB
DEFAULT_BODY_BYTES = 64 * KIB
# Filtre de densité (décidé par l'utilisateur, étendu à toutes les routes) : `json.loads` d'un corps dense en objets
# ([{},{},…]) coûte ≈ 24 × sa taille, et le SDK MCP en garde l'arbre ET une copie model_dump (≈ 48 ×) jusqu'au
# message suivant de la session, donc jusqu'à 30 min après la requête. Les messages légitimes portent leur volume
# dans des chaînes : on compte { [ , : hors chaînes, et au-delà de ce seuil le corps est refusé avant tout parsing.
# Messages légitimes les plus structurés : initialize avec toutes les capacités = 60, register_complete à 40
# réponses = 60 (tests/test_body_limit.py) → seuil à plus de 15 × de marge. Un corps admis coûte alors ≈ sa taille
# (chaînes) plus au plus 1 000 petits objets, sur REST comme sur MCP.
MAX_JSON_STRUCTURE = 1000
# Budget global des corps en vol (REST + MCP), compté sur la longueur déclarée AVANT tout parsing : il borne la
# concurrence (nginx admet 256 connexions × 16 Mio).
BODY_BUDGET_BYTES = 17 * MIB  # un corps REST maximal (16 Mio) + 1 Mio de petits corps simultanés
# Pire cas mémoire (tas, pas RSS), conteneur 896 Mio, 1 worker :
#   socle Python/FastAPI/SQLAlchemy/SDK MCP ≈ 150 Mio ; Argon2 2 × 64 Mio = 128 Mio ;
#   corps en vol : un emoji dans une chaîne la fait stocker à 4 octets/caractère par CPython : json.loads d'un corps
#   REST de 16 Mio culmine à 128 Mio (texte décodé + valeur, mesuré), + le corps brut 16, puis encodé et chiffré
#   (≤ 2 × 16) une fois le texte décodé libéré → ≈ 175 Mio ; + 1 Mio de petits corps × ≈ 8 → ≈ 185 Mio. Le scan
#   (json_structure) copie au plus 2 × le corps, libérées avant le parsing : pas cumulé ;
#   sessions MCP inactives (150, décidé par l'utilisateur) : état ~60 Kio + dernier message gardé par le SDK
#   (arbre, modèle et model_dump qui partage les chaînes ≈ 0,96 Mio élargis pour 244 Kio, + corps brut 256 Kio)
#   ≈ 1,3 Mio → 150 × 1,3 ≈ 195 Mio ;
#   total ≈ 660 Mio < 896 Mio, marge ≈ 235 Mio. Elle couvre les outils MCP qui survivent à leur échange HTTP (client
#   parti, ≤ 256 Kio d'arguments chacun, ≈ 1,3 Mio parsés) : authentifiés, bornés par AGENT_RATE (60/min, comptes
#   sur invitation) et la durée d'un outil (pool_timeout 30 s) ; non authentifiés (login, register_start/complete),
#   refusés par leur limiteur IP en mémoire avant toute requête DB, donc brefs (i9) ; et la fragmentation du tas.
BODY_BUDGET_WAIT = 10.0  # attente bornée d'une place dans le budget, puis SERVER_BUSY
# Détention bornée de l'échange (lecture du corps comprise) : sans borne, un client qui envoie son corps ou lit sa
# réponse au compte-gouttes (sans nginx) garderait sa réservation.
BODY_HOLD = 60.0
BUSY_RETRY_AFTER = 5
MCP_TOO_LARGE_HINT = (f"Une requête MCP est limitée à {MCP_BODY_BYTES // KIB} Kio : pour un fichier plus gros "
                      "(jusqu'à la limite du vault), utilise l'API REST : PUT /api/vault/<chemin>.")
DENSE_HINT = (f"Corps JSON trop structuré (plus de {MAX_JSON_STRUCTURE} objets, listes ou champs) : mets le contenu "
              "dans une chaîne (encoding base64 pour un fichier).")
UTF8_HINT = "Envoie le corps JSON encodé en UTF-8 (UTF-16 et UTF-32 sont refusés)."
assert MAX_UPLOAD_BYTES <= BODY_BUDGET_BYTES
# Refus de cette couche sur POST /mcp : le client MCP officiel traite tout statut non 2xx comme une panne du
# transport (raise_for_status) et la session meurt sans que l'agent voie l'erreur. Si le corps porte un id
# JSON-RPC, la réponse est donc un 200 JSON-RPC (erreur d'outil pour tools/call), sinon le statut HTTP habituel.
# id, method et params.name sont cherchés dans les MCP_HEAD_BYTES premiers et derniers octets seulement : le SDK
# Python officiel sérialise {"method", "params": {"name", "arguments"}, "jsonrpc", "id"} (id en fin de corps),
# d'autres clients mettent l'id en tête. Un corps refusé avant lecture (Content-Length au-delà du plafond, budget
# plein) est donc lu jusqu'au bout sans être gardé (deux fenêtres de 4 Kio), s'il fait au plus MCP_DRAIN_BYTES et
# en au plus MCP_DRAIN_WAIT secondes ; au-delà, seul le début compte.
MCP_HEAD_BYTES = 4 * KIB
MCP_DRAIN_BYTES = MAX_UPLOAD_BYTES  # un vault_write de fichier maximal reçoit le conseil REST
MCP_DRAIN_WAIT = 15.0
JSONRPC_INVALID_REQUEST = -32600
_JSON_STRING = re.compile(rb'"[^"]*"')


def json_structure(body: bytes) -> int:
    """Nombre de { [ , : hors chaînes JSON (une chaîne non fermée compte comme structure). Linéaire (N5) : les
    échappements (barre oblique doublée, puis guillemet échappé) sont retirés d'abord ; en JSON valide ils n'existent
    que dans les chaînes et s'apparient de gauche à droite. Chaque chaîne restante est alors un simple "…".
    s chaînes d'un JSON valide sont séparées par au moins s - 1 , ou : : au-delà du seuil, le score est ce minorant,
    sans retirer les chaînes une à une (≈ 0,7 s par 16 Mio de guillemets sinon). Un thread n'aiderait pas : re et
    bytes gardent le GIL (mesuré, m6)."""
    bare = body.replace(b"\\\\", b"").replace(b'\\"', b"")
    strings_floor = bare.count(b'"') // 2 - 1
    if strings_floor > MAX_JSON_STRUCTURE:
        return strings_floor
    bare = _JSON_STRING.sub(b"", bare)
    return bare.count(b"{") + bare.count(b"[") + bare.count(b",") + bare.count(b":")


def is_utf8_json(body: bytes) -> bool:
    """Même détection que json.loads(bytes) (Starlette, SDK MCP) : UTF-16/32 refusés, le scan ne lit que l'UTF-8 (N4)."""
    return json.detect_encoding(body) in ("utf-8", "utf-8-sig")


@dataclass(frozen=True)
class JsonRpcHead:
    id: int | str
    method: str | None
    tool: str | None  # params.name, si method vaut tools/call ou non


_WS = re.compile(r"[ \t\n\r]*")
# Chaîne JSON complète ; alternatives exclusives (premier caractère) : échec en temps linéaire.
_STRING = re.compile(r'"((?:[^"\\\x00-\x1f]|\\(?:["\\/bfnrt]|u[0-9a-fA-F]{4}))*)"')
_SCALAR = re.compile(r"[-+.0-9A-Za-z]+")
_SCALAR_CHARS = frozenset("-+.0123456789abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ")
_INT_ID = re.compile(r"-?(?:0|[1-9][0-9]{0,17})")
# Valeurs renvoyées au client : ASCII imprimable sans guillemet ni barre oblique inverse, bornées.
_STR_ID = re.compile(r"[\x20\x21\x23-\x5b\x5d-\x7e]{1,128}")
_METHOD = re.compile(r"[A-Za-z0-9/_.-]{1,64}")
_TOOL_NAME = re.compile(r"[A-Za-z0-9_.-]{1,64}")
_SESSION_ID = re.compile(r"[\x21-\x7e]{1,128}")
_BOM = "\N{ZERO WIDTH NO-BREAK SPACE}"
_BAD = object()  # valeur présente mais anormale (ou clé en double) : jamais renvoyée


def _skip_ws(text: str, i: int) -> int:
    match = _WS.match(text, i)
    return match.end() if match else i


def _skip_value(text: str, i: int) -> int | None:
    """Fin de la valeur JSON commençant en i, None si tronquée ou invalide. Itératif (compteur de profondeur)."""
    if i >= len(text):
        return None
    if text[i] not in "{[":
        match = (_STRING if text[i] == '"' else _SCALAR).match(text, i)
        return match.end() if match else None
    depth = 0
    while i < len(text):
        char = text[i]
        if char == '"':
            match = _STRING.match(text, i)
            if match is None:
                return None
            i = match.end()
            continue
        if char in "{[":
            depth += 1
        elif char in "}]":
            depth -= 1
            if depth == 0:
                return i + 1
        i += 1
    return None


def _accept(raw: str, is_string: bool, pattern: re.Pattern[str], allow_int: bool) -> object:
    """Chaîne (contenu sans échappement) conforme à `pattern`, ou entier borné si `allow_int` ; sinon _BAD."""
    if is_string:
        return raw if pattern.fullmatch(raw) else _BAD
    return int(raw) if allow_int and _INT_ID.fullmatch(raw) else _BAD


def _scalar(text: str, i: int, pattern: re.Pattern[str], allow_int: bool) -> tuple[object, int | None]:
    end = _skip_value(text, i)
    if end is None:
        return _BAD, None
    if text[i] == '"':
        return _accept(text[i + 1:end - 1], True, pattern, allow_int), end
    return _accept(text[i:end], False, pattern, allow_int), end


def _members(text: str, i: int, top: bool) -> dict[str, object]:
    """Champs id/method/params.name (top) ou name (params) de l'objet commençant en i ; s'arrête à la première
    irrégularité ou troncature en gardant ce qui précède. Chaque caractère est lu une fois au plus (linéaire)."""
    found: dict[str, object] = {}
    if text[i:i + 1] != "{":
        return found
    i = _skip_ws(text, i + 1)
    while True:
        key = _STRING.match(text, i)
        if key is None:
            return found
        name = key.group(1)
        i = _skip_ws(text, key.end())
        if text[i:i + 1] != ":":
            return found
        i = _skip_ws(text, i + 1)
        end: int | None
        if top and name == "id":
            value, end = _scalar(text, i, _STR_ID, allow_int=True)
        elif top and name == "method":
            value, end = _scalar(text, i, _METHOD, allow_int=False)
        elif (top and name == "params") or (not top and name == "name"):
            name = "name"
            if top:
                inner = _members(text, i, top=False)
                value, end = inner.get("name"), _skip_value(text, i)
            else:
                value, end = _scalar(text, i, _TOOL_NAME, allow_int=False)
        else:
            value, end = None, _skip_value(text, i)
        if value is not None:
            found[name] = _BAD if name in found else value
        if end is None:
            return found
        i = _skip_ws(text, end)
        if text[i:i + 1] != ",":
            return found
        i = _skip_ws(text, i + 1)


def _rskip_ws(text: str, i: int) -> int:
    while i > 0 and text[i - 1] in " \t\n\r":
        i -= 1
    return i


def _rtoken(text: str, i: int) -> tuple[int, str, bool] | None:
    """Chaîne sans échappement ou scalaire finissant en i (exclu), lu à rebours : (début, contenu, chaîne ?).
    Partant d'une position hors chaîne, un guillemet non échappé est un délimiteur : une chaîne dont le contenu
    porte une barre oblique inverse est refusée, jamais interprétée."""
    if i == 0:
        return None
    if text[i - 1] == '"':
        start = text.rfind('"', 0, i - 1)
        raw = text[start + 1:i - 1]
        if start < 0 or "\\" in raw or text[start - 1:start] == "\\":
            return None
        return start, raw, True
    j = i
    while j > 0 and text[j - 1] in _SCALAR_CHARS:
        j -= 1
    return (j, text[j:i], False) if j < i else None


def _tail_members(text: str) -> dict[str, object]:
    """id et method parmi les derniers champs de premier niveau, lus à rebours depuis l'accolade finale ; s'arrête
    au premier champ dont la valeur n'est pas un scalaire (objet, liste, chaîne échappée). Linéaire."""
    found: dict[str, object] = {}
    i = _rskip_ws(text, len(text))
    if text[i - 1:i] != "}":
        return found
    i = _rskip_ws(text, i - 1)
    while True:
        value = _rtoken(text, i)
        if value is None:
            return found
        j = _rskip_ws(text, value[0])
        if text[j - 1:j] != ":":
            return found
        key = _rtoken(text, _rskip_ws(text, j - 1))
        if key is None or not key[2]:
            return found
        k = _rskip_ws(text, key[0])
        separator = text[k - 1:k]
        if separator not in (",", "{"):  # champ retenu seulement s'il est bien délimité
            return found
        if key[1] in ("id", "method"):
            is_id = key[1] == "id"
            parsed = _accept(value[1], value[2], _STR_ID if is_id else _METHOD, allow_int=is_id)
            found[key[1]] = _BAD if key[1] in found else parsed
        if separator == "{":
            return found
        i = _rskip_ws(text, k - 1)


def _codecs(head: bytes) -> tuple[str, str, int]:
    """Codec du début (même détection que json.loads(bytes)), codec explicite de la fin (sans BOM), taille de
    l'unité de code."""
    encoding = json.detect_encoding(head)
    tail = {"utf-8-sig": "utf-8",
            "utf-16": "utf-16-le" if head.startswith(b"\xff\xfe") else "utf-16-be",
            "utf-32": "utf-32-le" if head.startswith(b"\xff\xfe\x00\x00") else "utf-32-be"}.get(encoding, encoding)
    unit = 4 if "32" in encoding else 2 if "16" in encoding else 1
    return encoding, tail, unit


def jsonrpc_head(head: bytes, tail: bytes = b"") -> JsonRpcHead | None:
    """id, method et params.name d'une requête JSON-RPC, cherchés dans les MCP_HEAD_BYTES premiers octets du corps
    (`head`) et ses MCP_HEAD_BYTES derniers (`tail`, alignés sur la fin du corps), jamais au-delà.
    None sans id utilisable (notification, lot, corps illisible, id anormal, en double ou contradictoire)."""
    head, tail = head[:MCP_HEAD_BYTES], tail[-MCP_HEAD_BYTES:]
    head_codec, tail_codec, unit = _codecs(head)
    text = head[:len(head) - len(head) % unit].decode(head_codec, errors="replace").removeprefix(_BOM)
    found = _members(text, _skip_ws(text, 0), top=True)
    for name, value in _tail_members(tail[len(tail) % unit:].decode(tail_codec, errors="replace")).items():
        found[name] = value if found.get(name, value) == value else _BAD  # vu en tête et en fin : même valeur
    rid, method, tool = found.get("id"), found.get("method"), found.get("name")
    if not isinstance(rid, int | str):
        return None
    return JsonRpcHead(rid, method if isinstance(method, str) else None, tool if isinstance(tool, str) else None)


def _is_mcp(path: str) -> bool:
    """`/mcp` et tout ce qui est dessous (pas `/mcpx`)."""
    first_segment = path.split("/", 2)[1:2]
    return first_segment == ["mcp"]


def body_cap(method: str, path: str) -> int:
    if _is_mcp(path):
        return MCP_BODY_BYTES
    if method == "PUT" and path.startswith("/api/vault/"):
        return MAX_UPLOAD_BYTES
    if path.startswith("/api/forum/"):
        return FORUM_BODY_BYTES
    return DEFAULT_BODY_BYTES


class ByteBudget:
    def __init__(self, total: int) -> None:
        self.total = total
        self.used = 0
        self._freed = asyncio.Event()

    async def acquire(self, n: int, wait_seconds: float) -> bool:
        loop = asyncio.get_running_loop()
        deadline = loop.time() + wait_seconds
        while self.used + n > self.total:
            self._freed.clear()
            try:
                await asyncio.wait_for(self._freed.wait(), deadline - loop.time())
            except TimeoutError:
                return False
        self.used += n  # sans await depuis le contrôle : pas de course
        return True

    def release(self, n: int) -> None:
        self.used -= n
        self._freed.set()


class BodyLimitMiddleware:
    """Couche ASGI la plus externe, avant routage et parsing JSON : plafond de corps par route (Content-Length refusé
    d'emblée, corps lu ici en comptant), filtre de densité JSON, budget global d'octets en vol (REST et MCP)."""

    def __init__(self, app: ASGIApp, budget_bytes: int = BODY_BUDGET_BYTES, budget: ByteBudget | None = None) -> None:
        self.app = app
        self.budget = budget or ByteBudget(budget_bytes)

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        path = _paths(scope)[1]
        cap = body_cap(scope["method"], path)
        headers = Headers(scope=scope)
        length = headers.get("content-length")
        chunked = "transfer-encoding" in headers  # prime sur Content-Length (h11 lit alors en chunked)
        if not chunked and length is not None and not length.isdigit():
            await _reject(ApiError("VALIDATION_ERROR", "invalid Content-Length"), scope, receive, send)
            return
        reserved = cap if chunked else int(length or 0)
        rpc = scope["method"] == "POST" and _is_mcp(path)  # refus en JSON-RPC si le corps porte un id
        if reserved > cap:
            ends = await _drain(receive, _Ends(), MCP_DRAIN_BYTES if reserved <= MCP_DRAIN_BYTES else 0) if rpc else None
            await _reject(too_large_error(cap, path), scope, receive, send, ends)
            return
        if reserved == 0:  # sans corps (GET, flux SSE) : ni compté ni borné dans le temps
            await self.app(scope, receive, send)
            return
        if not await self.budget.acquire(reserved, BODY_BUDGET_WAIT):
            busy = ApiError("SERVER_BUSY", "too many request bodies in flight", retry_after=BUSY_RETRY_AFTER)
            await _reject(busy, scope, receive, send, await _drain(receive, _Ends(), MCP_DRAIN_BYTES) if rpc else None)
            return
        started = False
        ends = _Ends()
        rpc_ends = ends if rpc else None

        async def watched_send(message: Message) -> None:
            nonlocal started
            started = started or message["type"] == "http.response.start"
            await send(message)

        try:
            # Une seule borne pour la lecture et l'application ; une seule réponse au plus.
            # Annuler le transport MCP ne coupe que la réponse : l'outil s'exécute dans la tâche de session du SDK.
            with anyio.move_on_after(BODY_HOLD) as hold:
                try:
                    body = await _read_capped(receive, cap, ends)
                except _BodyTooLarge:
                    if rpc:
                        await _drain(receive, ends, MCP_DRAIN_BYTES)
                    await _reject(too_large_error(cap, path), scope, receive, watched_send, rpc_ends)
                    return
                except _ClientGone:
                    return
                if chunked:  # réservation ramenée à la taille réelle
                    self.budget.release(reserved - len(body))
                    reserved = len(body)
                if not is_utf8_json(body):
                    utf8 = ApiError("VALIDATION_ERROR", "request body must be UTF-8 JSON", hint=UTF8_HINT)
                    await _reject(utf8, scope, receive, watched_send, rpc_ends)
                    return
                if json_structure(body) > MAX_JSON_STRUCTURE:
                    dense = ApiError("VALIDATION_ERROR", "request body is too structured", hint=DENSE_HINT)
                    await _reject(dense, scope, receive, watched_send, rpc_ends)
                    return
                await self.app(scope, _replay(body, receive), watched_send)
            if hold.cancelled_caught and not started:  # sinon : 500 texte brut d'uvicorn (aucune réponse émise)
                slow = ApiError("SERVER_BUSY", "request took too long", retry_after=BUSY_RETRY_AFTER)
                await _reject(slow, scope, receive, send, rpc_ends)
        finally:
            self.budget.release(reserved)


async def _reject(err: ApiError, scope: Scope, receive: Receive, send: Send, ends: "_Ends | None" = None) -> None:
    """Réponses propres à cette couche, hors NoStoreMiddleware : no-store posé ici. `ends` (POST /mcp) : début et
    fin du corps ; s'ils portent un id JSON-RPC, réponse 200 JSON-RPC, sinon le statut HTTP de l'erreur."""
    request = None
    if ends is not None:  # fin du corps inconnue s'il n'a pas été lu jusqu'au bout
        request = jsonrpc_head(bytes(ends.head), bytes(ends.tail) if ends.complete else b"")
    response = error_response(err) if request is None else _jsonrpc_reply(err, request, scope)
    response.headers["Cache-Control"] = "no-store"
    await response(scope, receive, send)


def _jsonrpc_reply(err: ApiError, request: JsonRpcHead, scope: Scope) -> JSONResponse:
    """Erreur d'outil au format de FastMCP (« Error executing tool <nom>: {json} », app/discovery.py) pour
    tools/call, erreur JSON-RPC sinon. Seuls l'id et le nom d'outil validés par jsonrpc_head sont renvoyés."""
    error = err.to_dict()
    payload: dict[str, object] = {"jsonrpc": "2.0", "id": request.id}
    if request.method == "tools/call" and request.tool is not None:
        text = f"Error executing tool {request.tool}: " + json.dumps(error, ensure_ascii=False)
        payload["result"] = {"content": [{"type": "text", "text": text}], "isError": True}
    else:
        payload["error"] = {"code": JSONRPC_INVALID_REQUEST, "message": f"{err.code}: {err.message}", "data": error}
    headers = {"Retry-After": str(err.retry_after)} if err.retry_after is not None else {}
    sessions = Headers(scope=scope).getlist("mcp-session-id")
    if len(sessions) == 1 and _SESSION_ID.fullmatch(sessions[0]):
        headers["Mcp-Session-Id"] = sessions[0]
    return JSONResponse(payload, headers=headers)


class _Ends:
    """MCP_HEAD_BYTES premiers et derniers octets d'un corps lu morceau par morceau, en mémoire constante."""

    def __init__(self) -> None:
        self.head = bytearray()
        self.tail = bytearray()
        self.size = 0
        self.complete = False

    def feed(self, chunk: bytes, more: bool) -> None:
        if len(self.head) < MCP_HEAD_BYTES:
            self.head += chunk[:MCP_HEAD_BYTES - len(self.head)]
        self.tail += chunk[-MCP_HEAD_BYTES:]
        del self.tail[:-MCP_HEAD_BYTES]
        self.size += len(chunk)
        self.complete = not more


async def _drain(receive: Receive, ends: _Ends, limit: int) -> _Ends:
    """Suite d'un corps refusé, lue sans être gardée : jusqu'au bout s'il fait au plus `limit` octets, sinon jusqu'à
    en avoir le début ; MCP_DRAIN_WAIT s au plus (déconnexion ou délai : fin inconnue)."""
    with anyio.move_on_after(MCP_DRAIN_WAIT):
        while not ends.complete and (ends.size <= limit or len(ends.head) < MCP_HEAD_BYTES):
            message = await receive()
            if message["type"] != "http.request":
                break
            ends.feed(message.get("body", b""), message.get("more_body", False))
    return ends


def too_large_error(cap: int, path: str) -> ApiError:
    """Corps trop gros pour la route ; nginx renvoie le même corps JSON (deploy/nginx, tests/test_deploy_files.py)."""
    hint = MCP_TOO_LARGE_HINT if _is_mcp(path) else None
    return ApiError("VALIDATION_ERROR", f"request body exceeds {cap} bytes for this route", hint=hint)


class _BodyTooLarge(Exception):
    pass


class _ClientGone(Exception):
    pass


async def _read_capped(receive: Receive, cap: int, ends: _Ends) -> bytes:
    """Corps complet ; _BodyTooLarge dès que `cap` est dépassé (le reste n'est pas lu ici). Début et fin du corps
    sont suivis dans `ends`, morceau qui dépasse compris (id JSON-RPC d'un refus)."""
    chunks: list[bytes] = []
    size = 0
    while True:
        message = await receive()
        if message["type"] != "http.request":
            raise _ClientGone  # corps incomplet : jamais confié à l'application
        chunk = message.get("body", b"")
        ends.feed(chunk, message.get("more_body", False))
        size += len(chunk)
        if size > cap:
            raise _BodyTooLarge
        chunks.append(chunk)
        if not message.get("more_body", False):
            return chunks[0] if len(chunks) == 1 else b"".join(chunks)


def _replay(body: bytes, receive: Receive) -> Receive:
    delivered = False

    async def replay() -> Message:
        nonlocal delivered
        if delivered:
            return await receive()  # au-delà : attente de la déconnexion (flux SSE)
        delivered = True
        return {"type": "http.request", "body": body, "more_body": False}

    return replay
