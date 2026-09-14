from __future__ import annotations

import re
from dataclasses import dataclass

#: Asked for by a client that wants the answer in whatever language was spoken. Not a language
#: anybody writes in, so it never reaches a prompt: the pipeline resolves it to a real code
#: first, from Whisper's own detection for a recording and from the script of the text for a
#: replay.
AUTO_LANGUAGE = "auto"

#: What a request gets when it names no language. Answering in the language that was spoken is
#: the behaviour that needs no configuring, so it is the one that ships.
DEFAULT_LANGUAGE = AUTO_LANGUAGE

#: The language written when there is nothing better to go on: an unreadable transcript, or a
#: detected language the prompt files carry no ``## LANGUAGE`` section for.
FALLBACK_LANGUAGE = "en"


@dataclass(frozen=True, slots=True)
class Language:
    """One output language the pipeline can be asked for.

    ``code`` is the ISO 639-1 code shared by Whisper, the glossary and the prompt files.
    ``english_name`` and ``native_name`` exist for clients that render a picker.
    """

    code: str
    english_name: str
    native_name: str


# Curated rather than exhaustive: these are the languages the prompts and the glossary are
# written for. An unlisted code is still accepted and passed to Whisper untouched.
LANGUAGES: tuple[Language, ...] = (
    Language("en", "English", "English"),
    Language("ru", "Russian", "Русский"),
    Language("de", "German", "Deutsch"),
    Language("fr", "French", "Français"),
    Language("es", "Spanish", "Español"),
    Language("it", "Italian", "Italiano"),
    Language("pt", "Portuguese", "Português"),
    Language("nl", "Dutch", "Nederlands"),
    Language("pl", "Polish", "Polski"),
    Language("uk", "Ukrainian", "Українська"),
    Language("tr", "Turkish", "Türkçe"),
    Language("zh", "Chinese", "中文"),
    Language("ja", "Japanese", "日本語"),
    Language("ko", "Korean", "한국어"),
)

_BY_CODE = {language.code: language for language in LANGUAGES}
_BY_NAME = {language.english_name.casefold(): language for language in LANGUAGES}


def normalise_language(value: str | None) -> str | None:
    """Return the canonical language code for ``value``, or None when it names nothing.

    Accepts a code ("EN", "en-GB", "ru_RU") or an English name ("English"), case- and
    separator-insensitively, and :data:`AUTO_LANGUAGE`. An unknown two-or-three-letter code is
    returned lower-cased rather than rejected, because Whisper knows far more languages than
    :data:`LANGUAGES` lists; anything else yields None.
    """
    if value is None:
        return None
    cleaned = value.strip()
    if not cleaned:
        return None
    if cleaned.casefold() == AUTO_LANGUAGE:
        return AUTO_LANGUAGE
    base = cleaned.replace("_", "-").split("-", 1)[0].casefold()
    if base in _BY_CODE:
        return base
    named = _BY_NAME.get(cleaned.casefold())
    if named is not None:
        return named.code
    return base if 2 <= len(base) <= 3 and base.isalpha() else None


def language_name(code: str) -> str:
    """Return the English name for ``code``, or the code itself when it is not listed."""
    if code == AUTO_LANGUAGE:
        return "Auto"
    language = _BY_CODE.get(code)
    return language.english_name if language is not None else code


_CYRILLIC = re.compile(r"[\u0400-\u04FF]")


def detect_language(text: str) -> str:
    """Guess the language of written ``text``, for a request that has no audio to go on.

    Whisper is the real detector and it only exists for a recording; this is what
    ``/v1/transform`` falls back on when it is asked for :data:`AUTO_LANGUAGE`. It separates
    scripts, not languages, so it can only tell apart the two the prompt files carry sections
    for: any Cyrillic at all reads as Russian, and everything else as
    :data:`FALLBACK_LANGUAGE`.

    A single Cyrillic word is enough on purpose, rather than a majority of them. The speech
    this pipeline gets is Russian with English identifiers dropped into it - "добавь migration
    на index" is two thirds Latin by letter count and entirely Russian as a sentence - while
    English speech practically never carries Cyrillic. Counting letters would answer that
    example in the wrong language; asymmetry answers it in the right one.

    Deliberately crude beyond that: a wrong guess costs one replay in the wrong language,
    while a heuristic elaborate enough to separate Latin-script languages from each other
    would be wrong more often and much harder to explain.
    """
    return "ru" if _CYRILLIC.search(text) else FALLBACK_LANGUAGE
