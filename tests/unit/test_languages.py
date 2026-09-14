from __future__ import annotations

import pytest

from vox_server.languages import (
    AUTO_LANGUAGE,
    DEFAULT_LANGUAGE,
    FALLBACK_LANGUAGE,
    LANGUAGES,
    detect_language,
    language_name,
    normalise_language,
)


def test_auto_is_the_default_and_english_is_what_it_falls_back_to() -> None:
    """Answering in the language that was spoken is what ships; "en" is only the fallback."""
    assert DEFAULT_LANGUAGE == AUTO_LANGUAGE
    assert FALLBACK_LANGUAGE == "en"
    assert any(language.code == FALLBACK_LANGUAGE for language in LANGUAGES)
    assert all(language.code != AUTO_LANGUAGE for language in LANGUAGES)


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


def test_auto_survives_normalisation_in_any_casing() -> None:
    assert normalise_language("auto") == AUTO_LANGUAGE
    assert normalise_language("AUTO") == AUTO_LANGUAGE
    assert normalise_language(" Auto ") == AUTO_LANGUAGE


def test_auto_has_a_name_to_show_in_a_picker() -> None:
    assert language_name(AUTO_LANGUAGE) == "Auto"


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("посмотри этот сервис", "ru"),
        ("check this service", "en"),
        ("добавь migration на index", "ru"),
        ("add a migration for the index", "en"),
        ("", "en"),
        ("   ", "en"),
        ("123 45.6 -- ???", "en"),
    ],
)
def test_written_text_is_read_by_its_script(text: str, expected: str) -> None:
    """It separates scripts, not languages: Cyrillic is Russian, everything else falls back."""
    assert detect_language(text) == expected


def test_one_cyrillic_word_is_enough_to_read_the_whole_line_as_russian() -> None:
    """Russian speech is mostly English identifiers by letter count; a majority rule fails it."""
    assert detect_language("добавь migration на index") == "ru"
    assert detect_language("проверь feature flag") == "ru"
    assert detect_language("add a migration for the index") == "en"
