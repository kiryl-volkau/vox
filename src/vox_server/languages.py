from __future__ import annotations

from dataclasses import dataclass

DEFAULT_LANGUAGE = "en"


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
    separator-insensitively. An unknown two-or-three-letter code is returned lower-cased
    rather than rejected, because Whisper knows far more languages than :data:`LANGUAGES`
    lists; anything else yields None.
    """
    if value is None:
        return None
    cleaned = value.strip()
    if not cleaned:
        return None
    base = cleaned.replace("_", "-").split("-", 1)[0].casefold()
    if base in _BY_CODE:
        return base
    named = _BY_NAME.get(cleaned.casefold())
    if named is not None:
        return named.code
    return base if 2 <= len(base) <= 3 and base.isalpha() else None


def language_name(code: str) -> str:
    """Return the English name for ``code``, or the code itself when it is not listed."""
    language = _BY_CODE.get(code)
    return language.english_name if language is not None else code
