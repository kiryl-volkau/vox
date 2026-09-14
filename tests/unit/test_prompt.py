from __future__ import annotations

from pathlib import Path

import pytest

from vox_server.prompt import (
    ProjectFile,
    Prompt,
    PromptError,
    load_prompt,
    split_project_file,
)

PROJECT_TAG = "<project_context>"
INSTRUCTIONS_TAG = "<project_instructions>"
CONVERSATION_TAG = "<conversation>"
PROJECT_TEXT = "Термины: джигвард -> Jigward.\nМиграции уже применены."


def _write_prompt(directory: Path, text: str) -> Path:
    path = directory / "prompt.md"
    path.write_text(text, encoding="utf-8")
    return path


def test_a_missing_user_section_defaults_to_the_transcript(tmp_path: Path) -> None:
    path = _write_prompt(tmp_path, "## SYSTEM\nБудь краток.\n")

    prompt = load_prompt(path)

    assert prompt.system_prompt == "Будь краток."
    assert prompt.user_template == "{transcript}"


def test_a_user_section_overrides_the_default_template(tmp_path: Path) -> None:
    path = _write_prompt(tmp_path, "## SYSTEM\nБудь краток.\n\n## USER\nДай {transcript}\n")

    assert load_prompt(path).user_template == "Дай {transcript}"


def test_a_file_without_a_system_section_is_rejected(tmp_path: Path) -> None:
    path = _write_prompt(tmp_path, "## USER\n{transcript}\n")

    with pytest.raises(PromptError, match="SYSTEM"):
        load_prompt(path)


def test_a_missing_file_is_rejected(tmp_path: Path) -> None:
    with pytest.raises(PromptError, match="cannot read"):
        load_prompt(tmp_path / "absent.md")


def test_a_byte_order_mark_is_tolerated(tmp_path: Path) -> None:
    path = tmp_path / "prompt.md"
    path.write_bytes("## SYSTEM\nБудь краток.\n".encode("utf-8-sig"))

    assert load_prompt(path).system_prompt == "Будь краток."


def test_render_user_substitutes_only_the_known_placeholders() -> None:
    prompt = Prompt(
        system_prompt="s",
        user_template='Словарь:\n{glossary}\n\nJSON: {"a": 1} и {unknown}\n\n{transcript}',
    )

    rendered = prompt.render_user('вызови {"ok": true}', "джигвард -> Jigward")

    assert "джигвард -> Jigward" in rendered
    assert 'вызови {"ok": true}' in rendered
    assert '{"a": 1}' in rendered
    assert "{unknown}" in rendered
    assert "{transcript}" not in rendered
    assert "{glossary}" not in rendered


def test_render_system_appends_the_project_block() -> None:
    prompt = Prompt(system_prompt="Ты редактор.\n\n{project}", user_template="{transcript}")

    rendered = prompt.render_system(ProjectFile(context=PROJECT_TEXT))

    assert "{project}" not in rendered
    assert rendered.startswith("Ты редактор.")
    assert PROJECT_TAG in rendered
    assert rendered.index("Ты редактор.") < rendered.index(PROJECT_TAG)
    assert rendered.index(PROJECT_TAG) < rendered.index(PROJECT_TEXT)
    assert rendered.rstrip().endswith("</project_context>")


def test_render_system_without_a_project_leaves_no_dangling_delimiter() -> None:
    prompt = Prompt(system_prompt="Ты редактор.\n\n{project}", user_template="{transcript}")

    rendered = prompt.render_system(ProjectFile())

    assert "{project}" not in rendered
    assert PROJECT_TAG not in rendered
    assert rendered.strip() == "Ты редактор."


def test_render_system_treats_a_blank_project_as_no_project() -> None:
    """A .vox.md holding nothing but whitespace must read exactly like no .vox.md at all."""
    prompt = Prompt(system_prompt="Ты редактор.\n\n{project}", user_template="{transcript}")

    rendered = prompt.render_system(split_project_file("   \n\n  "))

    assert PROJECT_TAG not in rendered
    assert rendered.strip() == "Ты редактор."


def test_render_system_puts_instructions_and_conversation_in_their_own_blocks() -> None:
    prompt = Prompt(
        system_prompt="Ты редактор.\n{instructions}\n{project}\n{context}",
        user_template="{transcript}",
    )

    rendered = prompt.render_system(
        ProjectFile(instructions="Отвечай списком.", context=PROJECT_TEXT),
        "user: почини это\nassistant: починил",
    )

    assert INSTRUCTIONS_TAG in rendered
    assert "Отвечай списком." in rendered
    assert CONVERSATION_TAG in rendered
    assert "починил" in rendered
    assert rendered.index(INSTRUCTIONS_TAG) < rendered.index(PROJECT_TAG)
    assert rendered.index(PROJECT_TAG) < rendered.index(CONVERSATION_TAG)


def test_an_absent_conversation_leaves_no_conversation_block() -> None:
    prompt = Prompt(system_prompt="Ты редактор.\n{context}", user_template="{transcript}")

    rendered = prompt.render_system(ProjectFile(context=PROJECT_TEXT))

    assert CONVERSATION_TAG not in rendered
    assert rendered.strip() == "Ты редактор."


def test_render_system_picks_the_language_block() -> None:
    prompt = Prompt(
        system_prompt="Ты редактор.\n{language}",
        user_template="{transcript}",
        language_blocks=(("en", "Answer in English."), ("ru", "Отвечай по-русски.")),
    )

    assert "Отвечай по-русски." in prompt.render_system(ProjectFile(), "", "ru")
    assert "Answer in English." in prompt.render_system(ProjectFile(), "", "en")


def test_an_unknown_language_falls_back_to_the_default_block() -> None:
    prompt = Prompt(
        system_prompt="{language}",
        user_template="{transcript}",
        language_blocks=(("en", "Answer in English."), ("ru", "Отвечай по-русски.")),
    )

    assert prompt.render_system(ProjectFile(), "", "de").strip() == "Answer in English."


def test_a_prompt_with_no_language_sections_renders_the_placeholder_away() -> None:
    prompt = Prompt(system_prompt="Ты редактор.\n{language}", user_template="{transcript}")

    rendered = prompt.render_system(ProjectFile(), "", "en")

    assert "{language}" not in rendered
    assert rendered.strip() == "Ты редактор."


def test_render_system_leaves_a_prompt_without_the_placeholder_unchanged() -> None:
    prompt = Prompt(
        system_prompt="Ты редактор. Плейсхолдера здесь нет.", user_template="{transcript}"
    )

    unchanged = "Ты редактор. Плейсхолдера здесь нет."
    assert prompt.render_system(ProjectFile(context=PROJECT_TEXT)) == unchanged
    assert prompt.render_system(ProjectFile()) == unchanged


def test_injected_text_is_never_rescanned_for_other_placeholders() -> None:
    """Substitution is one pass, so a .vox.md may document the placeholders themselves.

    Chained replaces would expand the "{project}" written inside the instructions section and
    inject the project block a second time, inside its own instructions.
    """
    prompt = Prompt(
        system_prompt="A{instructions}B{project}C{context}D", user_template="{transcript}"
    )

    rendered = prompt.render_system(
        ProjectFile(instructions="placeholders are {project} and {context}", context="ctx"),
        "conv",
    )

    assert "placeholders are {project} and {context}" in rendered
    assert rendered.count(PROJECT_TAG) == 1
    assert rendered.count(CONVERSATION_TAG) == 1


def test_split_project_file_without_a_system_section_is_all_context() -> None:
    """Every .vox.md written before the header existed has to keep working unchanged."""
    split = split_project_file(PROJECT_TEXT)

    assert split.instructions == ""
    assert split.context == PROJECT_TEXT


def test_split_project_file_extracts_the_system_section() -> None:
    split = split_project_file(
        "# Проект\nиспользует Kotlin\n\n## SYSTEM\nОтвечай списком.\n\n## Термины\nJigward\n"
    )

    assert split.instructions == "Отвечай списком."
    assert "использует Kotlin" in split.context
    assert "Jigward" in split.context
    assert "Отвечай списком." not in split.context


def test_a_system_section_running_to_the_end_of_the_file_is_taken_whole() -> None:
    split = split_project_file("контекст\n\n## SYSTEM\nстрока один\nстрока два\n")

    assert split.instructions == "строка один\nстрока два"
    assert split.context == "контекст"


def test_a_blank_project_file_is_falsy() -> None:
    assert not split_project_file("")
    assert not split_project_file("   \n\n")
    assert not split_project_file(None)
    assert split_project_file(PROJECT_TEXT)


def test_render_system_keeps_literal_braces_on_both_sides() -> None:
    """Prompt text and a project file both legitimately contain braces.

    Substitution has to be str.replace: str.format would raise KeyError on {unknown} and
    mangle the JSON examples either text carries.
    """
    prompt = Prompt(
        system_prompt='Верни JSON: {"ok": true}. Не трогай {unknown}.\n\n{project}',
        user_template="{transcript}",
    )

    rendered = prompt.render_system(
        ProjectFile(context='Пример вызова: {"id": 1}, шаблон {transcript}')
    )

    assert '{"ok": true}' in rendered
    assert "{unknown}" in rendered
    assert '{"id": 1}' in rendered
    assert "{transcript}" in rendered
    assert "{project}" not in rendered


def test_the_repository_prompt_loads(repo_root: Path) -> None:
    prompt = load_prompt(repo_root / "prompt.md")

    assert "{transcript}" in prompt.user_template
    assert "{glossary}" in prompt.user_template
    assert "{project}" in prompt.system_prompt
    assert "{instructions}" in prompt.system_prompt
    assert "{context}" in prompt.system_prompt
    assert "{language}" in prompt.system_prompt
    assert [code for code, _ in prompt.language_blocks] == ["ru", "en"]


def test_the_repository_dictation_prompt_loads(repo_root: Path) -> None:
    prompt = load_prompt(repo_root / "dictation.md")

    assert "{transcript}" in prompt.user_template
    assert "{glossary}" in prompt.user_template
    assert "{project}" in prompt.system_prompt
    assert "{instructions}" in prompt.system_prompt
    assert "{context}" in prompt.system_prompt
    assert "{language}" in prompt.system_prompt
    assert [code for code, _ in prompt.language_blocks] == ["ru", "en"]
