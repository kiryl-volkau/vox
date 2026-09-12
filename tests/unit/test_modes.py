from __future__ import annotations

from collections.abc import Callable
from pathlib import Path

import pytest

from voice_code_server.modes import Mode, ModeError, ModeRegistry, UnknownModeError

EXPECTED_MODES = ("clean", "context", "dictation", "task")


def _write_mode(directory: Path, name: str, text: str) -> Path:
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / f"{name}.md"
    path.write_text(text, encoding="utf-8")
    return path


def test_the_repository_modes_load(repo_root: Path) -> None:
    registry = ModeRegistry.load(repo_root / "modes")

    assert registry.names() == list(EXPECTED_MODES)
    assert len(registry) == 4


def test_context_is_the_only_mode_that_wraps_for_claude(repo_root: Path) -> None:
    registry = ModeRegistry.load(repo_root / "modes")

    for mode in registry.list():
        assert mode.requires_llm is True
        assert mode.wrap_for_claude is (mode.name == "context")


def test_the_context_wrapper_carries_the_normalized_placeholder(repo_root: Path) -> None:
    context = ModeRegistry.load(repo_root / "modes").get("context")

    assert "{normalized}" in context.wrapper_template
    assert len(context.wrapper_template) > 100

    wrapped = context.render_wrapper("Проверь membership.")

    assert "Проверь membership." in wrapped
    assert wrapped != "Проверь membership."
    assert "{normalized}" not in wrapped


def test_every_repository_mode_prompts_with_transcript_and_glossary(repo_root: Path) -> None:
    for mode in ModeRegistry.load(repo_root / "modes").list():
        assert "{transcript}" in mode.user_template
        assert "{glossary}" in mode.user_template
        assert mode.system_prompt.strip()


def test_to_info_exposes_the_client_facing_fields(repo_root: Path) -> None:
    info = ModeRegistry.load(repo_root / "modes").get("dictation").to_info()

    assert info.name == "dictation"
    assert info.label == "Dictation"
    assert info.requires_llm is True
    assert info.wrap_for_claude is False
    assert info.description


def test_get_is_case_insensitive_and_trims(repo_root: Path) -> None:
    registry = ModeRegistry.load(repo_root / "modes")

    assert registry.get("CONTEXT") is registry.get("  context  ")


def test_unknown_mode_reports_what_is_available(repo_root: Path) -> None:
    registry = ModeRegistry.load(repo_root / "modes")

    with pytest.raises(UnknownModeError) as excinfo:
        registry.get("does-not-exist")

    assert excinfo.value.name == "does-not-exist"
    assert excinfo.value.available == list(EXPECTED_MODES)
    assert "context" in str(excinfo.value)


def test_a_file_without_front_matter_falls_back_to_the_filename(tmp_path: Path) -> None:
    _write_mode(tmp_path, "raw", "## SYSTEM\nБудь краток.\n")

    mode = ModeRegistry.load(tmp_path).get("raw")

    assert mode.name == "raw"
    assert mode.label == "Raw"
    assert mode.description == ""
    assert mode.requires_llm is True
    assert mode.wrap_for_claude is False
    assert mode.fallback_to_transcript is False
    assert mode.temperature is None
    assert mode.system_prompt == "Будь краток."


def test_a_missing_user_section_defaults_to_the_transcript(tmp_path: Path) -> None:
    _write_mode(tmp_path, "raw", "## SYSTEM\nБудь краток.\n")

    assert ModeRegistry.load(tmp_path).get("raw").user_template == "{transcript}"


def test_front_matter_overrides_every_default(tmp_path: Path) -> None:
    _write_mode(
        tmp_path,
        "wrapped",
        "---\n"
        "name: fancy\n"
        "label: Fancy\n"
        "description: Что-то полезное\n"
        "requires_llm: false\n"
        "wrap_for_claude: true\n"
        "fallback_to_transcript: true\n"
        "temperature: 0.35\n"
        "---\n\n"
        "## USER\nДай {transcript}\n\n"
        "## WRAPPER\n[{normalized}]\n",
    )

    mode = ModeRegistry.load(tmp_path).get("fancy")

    assert mode.name == "fancy"
    assert mode.label == "Fancy"
    assert mode.description == "Что-то полезное"
    assert mode.requires_llm is False
    assert mode.wrap_for_claude is True
    assert mode.fallback_to_transcript is True
    assert mode.temperature == pytest.approx(0.35)
    assert mode.wrapper_template == "[{normalized}]"


def test_wrap_for_claude_without_a_wrapper_section_is_rejected(tmp_path: Path) -> None:
    _write_mode(
        tmp_path,
        "broken",
        "---\nwrap_for_claude: true\n---\n\n## SYSTEM\nБудь краток.\n",
    )

    with pytest.raises(ModeError, match="wrap_for_claude"):
        ModeRegistry.load(tmp_path)


def test_an_llm_mode_without_a_system_section_is_rejected(tmp_path: Path) -> None:
    _write_mode(tmp_path, "broken", "---\nrequires_llm: true\n---\n\n## USER\n{transcript}\n")

    with pytest.raises(ModeError, match="SYSTEM"):
        ModeRegistry.load(tmp_path)


def test_unclosed_front_matter_is_rejected(tmp_path: Path) -> None:
    _write_mode(tmp_path, "broken", "---\nname: broken\n\n## SYSTEM\nБудь краток.\n")

    with pytest.raises(ModeError, match="never closed"):
        ModeRegistry.load(tmp_path)


def test_a_non_boolean_flag_is_rejected(tmp_path: Path) -> None:
    _write_mode(tmp_path, "broken", "---\nrequires_llm: maybe\n---\n\n## SYSTEM\nx\n")

    with pytest.raises(ModeError, match="requires_llm"):
        ModeRegistry.load(tmp_path)


def test_a_non_numeric_temperature_is_rejected(tmp_path: Path) -> None:
    _write_mode(tmp_path, "broken", "---\ntemperature: warm\n---\n\n## SYSTEM\nx\n")

    with pytest.raises(ModeError, match="temperature"):
        ModeRegistry.load(tmp_path)


def test_duplicate_mode_names_are_rejected(tmp_path: Path) -> None:
    _write_mode(tmp_path, "one", "---\nname: same\n---\n\n## SYSTEM\nx\n")
    _write_mode(tmp_path, "two", "---\nname: Same\n---\n\n## SYSTEM\nx\n")

    with pytest.raises(ModeError, match="duplicate"):
        ModeRegistry.load(tmp_path)


def test_an_empty_directory_is_rejected(tmp_path: Path) -> None:
    (tmp_path / "empty").mkdir()

    with pytest.raises(ModeError, match="no mode files"):
        ModeRegistry.load(tmp_path / "empty")


def test_a_missing_directory_is_rejected(tmp_path: Path) -> None:
    with pytest.raises(ModeError, match="not found"):
        ModeRegistry.load(tmp_path / "absent")


def test_render_user_substitutes_only_the_known_placeholders() -> None:
    mode = Mode(
        name="x",
        label="X",
        description="",
        requires_llm=True,
        wrap_for_claude=False,
        temperature=None,
        fallback_to_transcript=False,
        system_prompt="s",
        user_template='Словарь:\n{glossary}\n\nJSON: {"a": 1} и {unknown}\n\n{transcript}',
        wrapper_template="",
    )

    rendered = mode.render_user('вызови {"ok": true}', "джигвард -> Jigward")

    assert "джигвард -> Jigward" in rendered
    assert 'вызови {"ok": true}' in rendered
    assert '{"a": 1}' in rendered
    assert "{unknown}" in rendered
    assert "{transcript}" not in rendered
    assert "{glossary}" not in rendered


def test_render_wrapper_is_a_no_op_without_wrapping(mode_factory: Callable[..., Mode]) -> None:
    plain = mode_factory(wrap_for_claude=False, wrapper_template="[{normalized}]")
    assert plain.render_wrapper("текст") == "текст"

    empty_wrapper = mode_factory(wrap_for_claude=True, wrapper_template="")
    assert empty_wrapper.render_wrapper("текст") == "текст"

    wrapping = mode_factory(wrap_for_claude=True, wrapper_template="<<{normalized}>>")
    assert wrapping.render_wrapper("текст") == "<<текст>>"
