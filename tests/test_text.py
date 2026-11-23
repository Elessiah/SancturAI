import pytest

from app.errors import ApiError
from app.text import check_line, check_text, one_line, strip_control, visible_length


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


@pytest.mark.parametrize("char", ["\u202a", "\u202b", "\u202c", "\u202d", "\u202e", "\u2066", "\u2067", "\u2068",
                                  "\u2069", "\u200e", "\u200f", "\u061c"])
def test_check_line_rejects_bidi_and_invisible_format_chars(char: str) -> None:
    """M2 (revue batch 2) : un titre ne peut pas inverser ou masquer visuellement l'affichage d'une liste."""
    with pytest.raises(ApiError) as exc:
        check_line(f"titre{char}nocif", "title")
    assert exc.value.code == "VALIDATION_ERROR"
    assert check_text(f"corps{char}ok", "body")  # les corps (markdown multilingue) les gardent


def test_one_line_keeps_meaningful_joiners_and_drops_stray_ones() -> None:
    """Secu tour 2 : ZWNJ (persan) et ZWJ (émojis composés) gardés entre deux caractères visibles ; bidi retirés."""
    family = "\U0001f468\u200d\U0001f469\u200d\U0001f467"
    assert one_line(f"je veux : می\u200cخواهم {family}") == f"je veux : می\u200cخواهم {family}"
    assert one_line("a\u200d\u200d\u200db \u200dc\u200d\n\u202ed") == "a\u200db c d"
    assert visible_length("a\u200db") == 2
