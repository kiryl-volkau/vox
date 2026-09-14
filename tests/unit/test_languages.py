from __future__ import annotations

import pytest

from vox_server.languages import DEFAULT_LANGUAGE, LANGUAGES, language_name, normalise_language


def test_english_is_the_default() -> None:
    assert DEFAULT_LANGUAGE == "en"
    assert any(language.code == DEFAULT_LANGUAGE for language in LANGUAGES)


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        ("en", "en"),
        ("EN", "en"),
        ("  ru  ", "ru"),
        ("en-GB", "en"),
        ("ru_RU", "ru"),
        ("English", "en"),
        ("russian", "ru"),
    ],
)
def test_codes_and_names_normalise_to_a_code(value: str, expected: str) -> None:
    assert normalise_language(value) == expected


def test_an_unlisted_but_plausible_code_is_kept() -> None:
    """Whisper knows far more languages than the picker lists, so codes are not gatekept."""
    assert normalise_language("sv") == "sv"
    assert normalise_language("ces") == "ces"


@pytest.mark.parametrize("value", [None, "", "   ", "not a language", "e", "12"])
def test_nonsense_yields_nothing(value: str | None) -> None:
    assert normalise_language(value) is None


def test_language_name_falls_back_to_the_code() -> None:
    assert language_name("ru") == "Russian"
    assert language_name("sv") == "sv"


def test_every_listed_language_normalises_to_itself() -> None:
    for language in LANGUAGES:
        assert normalise_language(language.code) == language.code
        assert normalise_language(language.english_name) == language.code
