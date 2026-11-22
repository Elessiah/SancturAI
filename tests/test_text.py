import pytest

from app.errors import ApiError
from app.text import check_line, check_text, strip_control


def test_strip_control_removes_nul_and_c0_keeps_newlines() -> None:
    assert strip_control("a\x00b\x07c\td\ne\x7f") == "abc\td\ne"
    assert strip_control("x\ud800y") == "x\N{REPLACEMENT CHARACTER}y"


def test_check_text_rejects_nul_controls_and_surrogates() -> None:
    assert check_text("ligne 1\r\nligne 2\ttab", "body") == "ligne 1\r\nligne 2\ttab"
    for bad in ("a\x00b", "a\x1bb", "a\ud800b"):
        with pytest.raises(ApiError) as exc:
            check_text(bad, "body")
        assert exc.value.code == "VALIDATION_ERROR"
        assert bad not in exc.value.message  # jamais la valeur reçue dans le message


def test_check_line_rejects_any_control_or_line_separator() -> None:
    assert check_line("Titre — ok é", "title") == "Titre — ok é"
    for bad in ("a\nb", "a\rb", "a\tb", "a\x00b", "a\x85b", "a\u2028b", "a\u2029b", "a\ud800b"):
        with pytest.raises(ApiError) as exc:
            check_line(bad, "title")
        assert exc.value.code == "VALIDATION_ERROR"


@pytest.mark.parametrize("char", ["‪", "‫", "‬", "‭", "‮", "⁦", "⁧", "⁨",
                                  "⁩", "‎", "‏", "؜"])
def test_check_line_rejects_bidi_and_invisible_format_chars(char: str) -> None:
    """M2 (revue batch 2) : un titre ne peut pas inverser ou masquer visuellement l'affichage d'une liste."""
    with pytest.raises(ApiError) as exc:
        check_line(f"titre{char}nocif", "title")
    assert exc.value.code == "VALIDATION_ERROR"
    assert check_text(f"corps{char}ok", "body")  # les corps (markdown multilingue) les gardent
