from __future__ import annotations

import pytest

from vox_server.llm import clean_llm_output

FENCE = "```"


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("Просто текст.", "Просто текст."),
        ("  Просто текст.  ", "Просто текст."),
        (f"{FENCE}\nhello\n{FENCE}", "hello"),
        (f'{FENCE}json\n{{"a": 1}}\n{FENCE}', '{"a": 1}'),
        (f"{FENCE}python\nprint(1)", "print(1)"),
        (f"{FENCE}\nа\n\nб\n{FENCE}", "а\n\nб"),
        ("Here is your prompt:\nDo the thing", "Do the thing"),
        ("Here's the result:\nDo the thing", "Do the thing"),
        ("Certainly:\nX", "X"),
        ("Result:\nX", "X"),
        ("Output:\nX", "X"),
        ("Вот итоговый промпт:\nСделай рефакторинг", "Сделай рефакторинг"),
        ("Готовый промпт:\nПроверь X", "Проверь X"),
        ("Результат:\nПроверь X", "Проверь X"),
        (f"Result:\n{FENCE}\ncode\n{FENCE}", "code"),
        ("<think>рассуждения</think>\nОтвет", "Ответ"),
        ("<think>\nмного\nстрок\n</think>\n\nОтвет", "Ответ"),
        ("незакрытые рассуждения</think>\nФинал", "Финал"),
        ('"Готовый текст"', "Готовый текст"),
        ("«Готовый текст»", "Готовый текст"),
        ("'Готовый текст'", "Готовый текст"),
        ("", ""),
        ("   \n  ", ""),
    ],
)
def test_clean_llm_output_strips_only_wrapping_junk(raw: str, expected: str) -> None:
    assert clean_llm_output(raw) == expected


@pytest.mark.parametrize(
    "raw",
    [
        "Открытые вопросы:\n- нужен ли кэш",
        "Note: важное замечание",
        "Первая строка\nВторая: с двоеточием\nТретья",
        '"a" и "b"',
        "Проверь следующие файлы:\n- api.py\n- llm.py",
    ],
)
def test_clean_llm_output_keeps_legitimate_text_intact(raw: str) -> None:
    assert clean_llm_output(raw) == raw


def test_interior_lines_survive_a_fence_removal() -> None:
    raw = f"{FENCE}text\nпервая\nвторая: с двоеточием\n\nчетвёртая\n{FENCE}"

    cleaned = clean_llm_output(raw)

    assert cleaned.splitlines() == ["первая", "вторая: с двоеточием", "", "четвёртая"]


def test_an_interior_fence_is_left_alone() -> None:
    raw = f"Сделай так:\n\n{FENCE}\nkod\n{FENCE}\n\nи проверь тесты"

    assert clean_llm_output(raw) == raw


def test_a_reply_that_is_only_a_preamble_becomes_empty() -> None:
    assert clean_llm_output("Вот результат:") == ""


def test_only_one_preamble_line_is_removed() -> None:
    assert clean_llm_output("Certainly:\nOutput:\nX") == "Output:\nX"


def test_a_think_block_does_not_eat_the_answer() -> None:
    cleaned = clean_llm_output("<think>надо ли трогать тесты</think>Проверь тесты.")

    assert cleaned == "Проверь тесты."


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        # A first line ending in ":" is only boilerplate when every word is filler. These read
        # like announcements but carry real content, so they must survive intact.
        (
            "Response headers надо проверить так:\n- Content-Type",
            "Response headers надо проверить так:\n- Content-Type",
        ),
        (
            "Ответ сервиса надо проверить тут:\n- UserController",
            "Ответ сервиса надо проверить тут:\n- UserController",
        ),
        ("Проверить надо это:\n- Нужен ли index?", "Проверить надо это:\n- Нужен ли index?"),
        ("Вот итоговый промпт:\nДобавь индекс.", "Добавь индекс."),
        ("Here is your prompt:\nAdd the index.", "Add the index."),
        ("Here’s the final prompt:\nAdd it.", "Add it."),
    ],
)
def test_only_all_filler_lines_are_treated_as_a_preamble(raw: str, expected: str) -> None:
    assert clean_llm_output(raw) == expected


def test_a_reply_with_several_fenced_blocks_is_left_alone() -> None:
    """Only a fence wrapping the WHOLE reply may be unwrapped.

    Scanning backwards for the closing fence spliced the end of the first block to the start
    of the last one, producing output with unbalanced markers.
    """
    raw = "```bash\ndocker compose up\n```\n\nЗатем:\n\n```bash\ndocker compose logs\n```"

    assert clean_llm_output(raw) == raw


def test_think_block_offsets_survive_characters_that_change_length_when_lowercased() -> None:
    """U+0130 lowercases to two characters, so offsets taken from text.lower() shifted."""
    assert clean_llm_output("<think>İİİ reasoning</think>Ответ здесь") == "Ответ здесь"
