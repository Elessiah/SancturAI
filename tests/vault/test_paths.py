import pytest

from app.errors import ApiError
from app.vault.paths import normalize_path


@pytest.mark.parametrize(("raw", "expected"), [
    ("notes/a.md", "/notes/a.md"),
    ("/README.md", "/README.md"),
    ("/café.md", "/café.md"),
])
def test_valid(raw: str, expected: str) -> None:
    assert normalize_path(raw) == expected


@pytest.mark.parametrize("raw", ["", "/", "/a/../b", "/a//b", "/a/", "/./a", "/a\x00b", "/" + "x" * 512])
def test_invalid(raw: str) -> None:
    with pytest.raises(ApiError) as exc:
        normalize_path(raw)
    assert exc.value.code == "VALIDATION_ERROR"
