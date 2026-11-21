import re
import unicodedata

from app.errors import ApiError

MAX_PATH = 512
_CONTROL = re.compile(r"[\x00-\x1f\x7f-\x9f]")  # C0, DEL, C1


def normalize_path(raw: str) -> str:
    path = unicodedata.normalize("NFC", raw)
    if not path.startswith("/"):
        path = "/" + path
    if len(path) > MAX_PATH or _CONTROL.search(path):
        raise ApiError("VALIDATION_ERROR", "path too long or contains control characters")
    segments = path.split("/")[1:]
    if not segments or any(s in ("", ".", "..") for s in segments):
        raise ApiError("VALIDATION_ERROR", "path must be absolute, without empty, '.' or '..' segments")
    return path
