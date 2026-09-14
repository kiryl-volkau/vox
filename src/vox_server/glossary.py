from __future__ import annotations

import logging
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import yaml

from .languages import normalise_language

_log = logging.getLogger(__name__)

ANY_LANGUAGE = "*"


@dataclass(frozen=True, slots=True)
class GlossaryEntry:
    """One spoken form and the canonical term it maps to in each output language.

    ``terms`` preserves source order and is keyed by language code, plus the pseudo-code
    :data:`ANY_LANGUAGE` for an entry written in the scalar form, which applies to every
    language.
    """

    spoken: str
    terms: tuple[tuple[str, str], ...]

    def term_for(self, language: str) -> str | None:
        """Return the canonical term for ``language``.

        An exact language match wins; otherwise the :data:`ANY_LANGUAGE` term is used when
        the entry has one. Returns None when the entry says nothing about this language.
        """
        fallback: str | None = None
        for code, term in self.terms:
            if code == language:
                return term
            if code == ANY_LANGUAGE:
                fallback = term
        return fallback


@dataclass(frozen=True, slots=True)
class Glossary:
    """Spoken form -> canonical engineering term vocabulary, per output language.

    Entries are hints only. The canonical terms bias Whisper through its initial prompt and
    the mapping is rendered into the LLM prompt as vocabulary context; nothing here ever
    performs string replacement on a transcript, so an entry can never corrupt a sentence
    that happens to contain the same syllables.

    An entry may name a different term per language, which is what lets the same Russian
    speech produce ``membership`` in an English answer and ``membership`` in a Russian one
    without the model translating identifiers on its own.

    ``entries`` preserves source order and holds no duplicate spoken forms (compared
    case-insensitively). An empty Glossary is falsy.
    """

    entries: tuple[GlossaryEntry, ...] = ()

    @classmethod
    def load(cls, path: Path) -> Glossary:
        """Read a YAML glossary from ``path``.

        Accepts either a top-level mapping of spoken form to term, or that mapping under a
        top-level ``terms`` key. A missing, unreadable or malformed file logs a warning and
        yields an empty Glossary: a bad glossary must never stop the server from starting.
        Never raises.
        """
        if not path.is_file():
            _log.warning("Glossary file %s not found; continuing without a glossary", path)
            return cls()
        try:
            raw = yaml.safe_load(path.read_text(encoding="utf-8"))
        except (OSError, UnicodeDecodeError, yaml.YAMLError) as exc:
            _log.warning("Glossary file %s could not be parsed (%s); ignoring it", path, exc)
            return cls()
        if raw is None:
            return cls()
        if not isinstance(raw, Mapping):
            _log.warning("Glossary file %s is not a mapping; ignoring it", path)
            return cls()
        section = raw.get("terms")
        glossary = cls.from_mapping(section if isinstance(section, Mapping) else raw)
        _log.info("Loaded %d glossary entries from %s", len(glossary), path)
        return glossary

    @classmethod
    def from_mapping(cls, mapping: Mapping[str, Any]) -> Glossary:
        """Build a Glossary from an in-memory mapping.

        A value may be a string, which makes the term apply to every language, or a mapping
        of language code to term. Non-string keys, blank keys or terms, entries that name no
        usable term at all, and repeated spoken forms (case-insensitive) are skipped.
        Surrounding whitespace is trimmed.
        """
        entries: list[GlossaryEntry] = []
        seen: set[str] = set()
        for key, value in mapping.items():
            if not isinstance(key, str):
                continue
            spoken = key.strip()
            if not spoken:
                continue
            folded = spoken.casefold()
            if folded in seen:
                continue
            terms = _parse_terms(value)
            if not terms:
                continue
            seen.add(folded)
            entries.append(GlossaryEntry(spoken=spoken, terms=terms))
        return cls(entries=tuple(entries))

    def as_prompt_block(self, language: str) -> str:
        """Return one ``spoken -> canonical`` line per entry that covers ``language``.

        Entries that say nothing about this language are left out entirely rather than
        falling back to another language's spelling. Returns "" when nothing applies.
        """
        lines = []
        for entry in self.entries:
            term = entry.term_for(language)
            if term is not None:
                lines.append(f"{entry.spoken} -> {term}")
        return "\n".join(lines)

    def as_stt_prompt(self, language: str | None = None) -> str:
        """Return the unique canonical terms joined by ", ", for Whisper's initial prompt.

        ``language`` restricts the terms to that output language; the default None takes
        every term in every language, which is what biasing recognition wants - what the
        speaker says is independent of the language the answer will be written in. Order
        follows ``entries``; duplicates are dropped. Returns "" when empty.
        """
        terms: list[str] = []
        seen: set[str] = set()
        for entry in self.entries:
            found = (
                [entry.term_for(language)]
                if language is not None
                else [term for _code, term in entry.terms]
            )
            for term in found:
                if term is None or term in seen:
                    continue
                seen.add(term)
                terms.append(term)
        return ", ".join(terms)

    def languages(self) -> tuple[str, ...]:
        """Return every explicit language code the glossary names, in source order."""
        found: list[str] = []
        for entry in self.entries:
            for code, _term in entry.terms:
                if code != ANY_LANGUAGE and code not in found:
                    found.append(code)
        return tuple(found)

    def __len__(self) -> int:
        return len(self.entries)

    def __bool__(self) -> bool:
        return bool(self.entries)


def _parse_terms(value: Any) -> tuple[tuple[str, str], ...]:
    if isinstance(value, str):
        term = value.strip()
        return ((ANY_LANGUAGE, term),) if term else ()
    if not isinstance(value, Mapping):
        return ()
    terms: list[tuple[str, str]] = []
    seen: set[str] = set()
    for raw_code, raw_term in value.items():
        if not isinstance(raw_code, str) or not isinstance(raw_term, str):
            continue
        term = raw_term.strip()
        if not term:
            continue
        code = ANY_LANGUAGE if raw_code.strip() == ANY_LANGUAGE else normalise_language(raw_code)
        if code is None or code in seen:
            continue
        seen.add(code)
        terms.append((code, term))
    return tuple(terms)
