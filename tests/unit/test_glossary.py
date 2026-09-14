from __future__ import annotations

import logging
from pathlib import Path

import pytest

from vox_server.glossary import Glossary

_TOP_LEVEL_YAML = """\
"джигвард": "Jigward"
"клауд код": "Claude Code"
"спринг бут": "Spring Boot"
"""

_TERMS_YAML = """\
terms:
  "джигвард": "Jigward"
  "клауд код": "Claude Code"
  "спринг бут": "Spring Boot"
"""


def _write(tmp_path: Path, text: str, name: str = "glossary.yaml") -> Path:
    path = tmp_path / name
    path.write_text(text, encoding="utf-8")
    return path


@pytest.mark.parametrize("text", [_TOP_LEVEL_YAML, _TERMS_YAML])
def test_load_accepts_both_document_shapes(tmp_path: Path, text: str) -> None:
    glossary = Glossary.load(_write(tmp_path, text))

    assert len(glossary) == 3
    assert glossary.entries[0].spoken == "джигвард"
    assert glossary.entries[0].term_for("ru") == "Jigward"
    assert glossary.entries[2].term_for("en") == "Spring Boot"
    assert bool(glossary) is True


def test_missing_file_yields_an_empty_glossary(tmp_path: Path) -> None:
    glossary = Glossary.load(tmp_path / "nothing-here.yaml")

    assert len(glossary) == 0
    assert bool(glossary) is False
    assert glossary.as_prompt_block("ru") == ""
    assert glossary.as_stt_prompt() == ""


def test_malformed_yaml_is_ignored_instead_of_raising(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    path = _write(tmp_path, "terms: [unclosed\n  - broken: *missing-anchor\n")

    with caplog.at_level(logging.WARNING):
        glossary = Glossary.load(path)

    assert len(glossary) == 0
    assert caplog.records


def test_a_yaml_document_that_is_not_a_mapping_is_ignored(tmp_path: Path) -> None:
    assert len(Glossary.load(_write(tmp_path, "- one\n- two\n"))) == 0


def test_an_empty_file_yields_an_empty_glossary(tmp_path: Path) -> None:
    assert len(Glossary.load(_write(tmp_path, ""))) == 0


def test_from_mapping_skips_unusable_entries() -> None:
    glossary = Glossary.from_mapping(
        {
            "  спринг  ": "  Spring  ",
            "": "Empty",
            "blank": "   ",
            "джигвард": "Jigward",
            "ДЖИГВАРД": "Duplicate",
        }
    )

    assert [entry.spoken for entry in glossary.entries] == ["спринг", "джигвард"]
    assert glossary.entries[0].term_for("ru") == "Spring"


def test_as_prompt_block_renders_one_arrow_line_per_entry() -> None:
    glossary = Glossary.from_mapping({"джигвард": "Jigward", "градл": "Gradle"})

    assert glossary.as_prompt_block("ru") == "джигвард -> Jigward\nградл -> Gradle"


def test_a_scalar_term_applies_to_every_language() -> None:
    glossary = Glossary.from_mapping({"джигвард": "Jigward"})

    assert glossary.as_prompt_block("ru") == "джигвард -> Jigward"
    assert glossary.as_prompt_block("en") == "джигвард -> Jigward"
    assert glossary.as_prompt_block("de") == "джигвард -> Jigward"


def test_a_per_language_term_renders_only_that_language_spelling() -> None:
    glossary = Glossary.from_mapping({"расшифровка": {"ru": "расшифровка", "en": "transcript"}})

    assert glossary.as_prompt_block("ru") == "расшифровка -> расшифровка"
    assert glossary.as_prompt_block("en") == "расшифровка -> transcript"
    assert glossary.languages() == ("ru", "en")


def test_a_language_the_entry_never_mentions_drops_out_of_its_block() -> None:
    """Borrowing another language's spelling would put a foreign word in the answer."""
    glossary = Glossary.from_mapping({"расшифровка": {"ru": "расшифровка"}})

    assert glossary.as_prompt_block("ru") == "расшифровка -> расшифровка"
    assert glossary.as_prompt_block("en") == ""


def test_hotwords_take_every_spelling_in_every_language() -> None:
    """What is spoken does not depend on the language the answer is written in."""
    glossary = Glossary.from_mapping(
        {"расшифровка": {"ru": "расшифровка", "en": "transcript"}, "градл": "Gradle"}
    )

    assert glossary.as_stt_prompt() == "расшифровка, transcript, Gradle"
    assert glossary.as_stt_prompt("en") == "transcript, Gradle"


def test_an_entry_naming_no_usable_term_is_skipped() -> None:
    glossary = Glossary.from_mapping({"пусто": {"ru": "   "}, "мусор": [1, 2], "ок": "fine"})

    assert [entry.spoken for entry in glossary.entries] == ["ок"]


def test_as_stt_prompt_joins_unique_canonical_terms() -> None:
    glossary = Glossary.from_mapping(
        {"джигвард": "Jigward", "джиг вард": "Jigward", "градл": "Gradle"}
    )

    assert glossary.as_stt_prompt() == "Jigward, Gradle"


def test_the_repository_glossary_loads_with_entries(repo_root: Path) -> None:
    glossary = Glossary.load(repo_root / "config" / "glossary.yaml")

    assert len(glossary) > 0
    assert "Claude Code" in glossary.as_stt_prompt()
    assert " -> " in glossary.as_prompt_block("ru")
    assert " -> " in glossary.as_prompt_block("en")
    # The repository glossary is what proves the bilingual form is actually exercised.
    assert glossary.as_prompt_block("ru") != glossary.as_prompt_block("en")
