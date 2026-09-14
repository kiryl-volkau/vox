"""The evaluation scorer, checked offline.

The eval suite needs a model and a backend, so it runs rarely; the logic that decides whether
it passed has to be trustworthy without either. A scorer that silently matches fragments
would turn the whole evaluation into a rubber stamp.
"""

from __future__ import annotations

import pytest

from tests.eval.scoring import contains_garbled, contains_term, score, suggest_correction


@pytest.mark.parametrize(
    ("text", "term"),
    [
        ("Check the membership export.", "membership"),
        ("Look at the system prompts.", "prompt"),
        ("There are two indexes missing.", "index"),
        ("Upgrade Spring Boot to the latest version.", "Spring Boot"),
        ("Upgrade spring-boot to the latest version.", "Spring Boot"),
        ("upgrade SPRING BOOT now", "Spring Boot"),
        ("Add a foreign key to organization.", "foreign key"),
        ("Вынеси запрос в repository.", "repository"),
        ("Опиши это в тестах.", "тест"),
        ("Добавь тесты.", "тест"),
    ],
)
def test_a_term_is_found_as_a_word(text: str, term: str) -> None:
    assert contains_term(text, term)


def test_an_inflected_term_has_to_be_written_as_its_stem() -> None:
    """Matching is open-ended at the end only, so a Russian term is written as a stem.

    "тесты" is not a prefix of "тестах": a case that forbids the nominative would quietly
    miss every other case ending, and pass while the model invented exactly what it forbade.
    """
    assert not contains_term("Опиши это в тестах.", "тесты")
    assert contains_term("Опиши это в тестах.", "тест")


@pytest.mark.parametrize(
    ("text", "term"),
    [
        ("Nothing about this at all.", "membership"),
        ("The latest build is green.", "test"),
        ("Reindexing is unrelated.", "index"),
        ("Spring is not Spring Boot's parent here.", "Spring Batch"),
    ],
)
def test_a_fragment_is_not_a_term(text: str, term: str) -> None:
    assert not contains_term(text, term)


@pytest.mark.parametrize("text", ["Please prepare the branch.", "A preprocessor ran."])
def test_an_acronym_must_be_a_whole_word(text: str) -> None:
    """An open-ended "PR" would be satisfied by "prepare", passing a case it never meant."""
    assert not contains_term(text, "PR")


@pytest.mark.parametrize("text", ["I sent a PR.", "the pr is ready", "Merged PR, one commit."])
def test_an_acronym_is_found_case_insensitively(text: str) -> None:
    assert contains_term(text, "PR")


def test_garbled_text_is_matched_as_a_substring() -> None:
    """The token is not a word anyone meant, so any trace of it counts as survival."""
    assert contains_garbled("посмотри системные промытоты тут", "промытоты")
    assert contains_garbled("Системные ПРОМЫТОТЫ", "промытоты")
    assert contains_garbled("что-то про промытотами говорили", "промытот")


def test_an_empty_garbled_token_is_never_found() -> None:
    """Cases that test something other than term recovery leave the field blank."""
    assert not contains_garbled("any text at all", "")
    assert not contains_garbled("any text at all", "   ")


def test_a_clean_rewrite_passes() -> None:
    result = score(
        "membership",
        "Add an endpoint that exports membership to CSV.",
        garbled="мембершипа",
        expect=("endpoint", "membership", "CSV"),
        forbid=("test", "pagination"),
    )

    assert result.passed
    assert result.describe() == "ok"


def test_a_surviving_garbled_token_fails_and_is_named() -> None:
    result = score(
        "prompts",
        "Посмотри системные промытоты.",
        garbled="промытоты",
        expect=("prompt",),
    )

    assert not result.passed
    assert result.garbled_survived == "промытоты"
    assert "промытоты" in result.describe()


def test_a_missing_term_fails_and_is_named() -> None:
    result = score("membership", "Add an export endpoint.", expect=("endpoint", "membership"))

    assert not result.passed
    assert result.missing == ("membership",)
    assert "missing membership" in result.describe()


def test_an_invented_term_fails_and_is_named() -> None:
    """The model helpfully adding work it was never asked for is the failure that matters."""
    result = score(
        "nothing-invented",
        "I looked at the linter. Add tests for it.",
        expect=("linter",),
        forbid=("test", "migration"),
    )

    assert not result.passed
    assert result.invented == ("test",)
    assert "invented test" in result.describe()


def test_a_correction_swaps_the_mangled_word_for_the_missing_term() -> None:
    corrected = suggest_correction(
        "Посмотри системные промытоты в этом сервисе.",
        garbled="промытоты",
        expect=("prompt",),
    )

    assert corrected == "Посмотри системные prompt в этом сервисе."


def test_a_correction_ignores_case_and_catches_every_occurrence() -> None:
    corrected = suggest_correction(
        "Промытоты дублируются, посмотри промытоты.", garbled="промытоты", expect=("prompt",)
    )

    assert corrected == "prompt дублируются, посмотри prompt."


def test_a_correction_prefers_the_term_the_output_is_missing() -> None:
    """With several expected terms, the one to suggest is the one that is not there."""
    corrected = suggest_correction(
        "Сделай endpoint для экспорта мембершипа.",
        garbled="мембершипа",
        expect=("endpoint", "membership"),
    )

    assert corrected == "Сделай endpoint для экспорта membership."


@pytest.mark.parametrize(
    ("output", "garbled", "expect"),
    [
        ("Check the prompts.", "промытоты", ("prompt",)),
        ("Anything at all.", "", ("prompt",)),
        ("Посмотри промытоты.", "промытоты", ()),
    ],
)
def test_there_is_no_correction_to_suggest(
    output: str, garbled: str, expect: tuple[str, ...]
) -> None:
    assert suggest_correction(output, garbled=garbled, expect=expect) is None


def test_every_reason_is_reported_at_once() -> None:
    result = score(
        "everything",
        "Посмотри промытоты и добавь тесты.",
        garbled="промытоты",
        expect=("prompt",),
        forbid=("тесты",),
    )

    described = result.describe()
    assert "survived" in described
    assert "missing prompt" in described
    assert "invented тесты" in described
