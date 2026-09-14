from __future__ import annotations

from functools import lru_cache
from pathlib import Path
from typing import Literal
from urllib.parse import urlsplit, urlunsplit

from pydantic import Field, SecretStr, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

from .languages import DEFAULT_LANGUAGE, normalise_language


class Settings(BaseSettings):
    """Runtime configuration for the vox backend.

    Values come from the process environment and from a ``.env`` file in the working
    directory, matched case-insensitively against the environment names below. Fields may
    also be passed by field name, e.g. ``Settings(stt_model="tiny")``, which is what tests
    and embedded callers use.
    """

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
        case_sensitive=False,
        populate_by_name=True,
    )

    host: str = Field("0.0.0.0", validation_alias="VOICE_CODE_HOST")
    port: int = Field(8765, validation_alias="VOICE_CODE_PORT")

    stt_model: str = Field("turbo", validation_alias="STT_MODEL")
    stt_language: str = Field("ru", validation_alias="STT_LANGUAGE")
    stt_device: Literal["auto", "cuda", "cpu"] = Field("auto", validation_alias="STT_DEVICE")
    stt_compute_type: str = Field("float16", validation_alias="STT_COMPUTE_TYPE")
    stt_beam_size: int = Field(1, validation_alias="STT_BEAM_SIZE")
    stt_vad_filter: bool = Field(True, validation_alias="STT_VAD_FILTER")
    stt_glossary_hotwords: bool = Field(True, validation_alias="STT_GLOSSARY_HOTWORDS")

    llm_base_url: str = Field(
        "http://host.docker.internal:11434/v1", validation_alias="LLM_BASE_URL"
    )
    llm_model: str = Field("qwen2.5:7b-instruct", validation_alias="LLM_MODEL")
    llm_api_key: SecretStr = Field(SecretStr("local"), validation_alias="LLM_API_KEY")
    llm_timeout_seconds: float = Field(120.0, validation_alias="LLM_TIMEOUT_SECONDS")
    llm_temperature: float = Field(0.1, validation_alias="LLM_TEMPERATURE")
    # Dictation reproduces what was said rather than reformulating it, so it samples greedily.
    llm_dictation_temperature: float = Field(0.0, validation_alias="LLM_DICTATION_TEMPERATURE")
    llm_max_tokens: int = Field(1024, validation_alias="LLM_MAX_TOKENS")

    # Sends one throwaway completion at startup so a local model is resident before the first
    # real request. A hosted endpoint has nothing to load, so there the call buys nothing and
    # is billed like any other.
    llm_warmup: bool = Field(True, validation_alias="LLM_WARMUP")

    processing_concurrency: int = Field(1, validation_alias="PROCESSING_CONCURRENCY")

    log_level: str = Field("INFO", validation_alias="LOG_LEVEL")
    log_text: bool = Field(False, validation_alias="LOG_TEXT")

    # Where settings changed at runtime are kept so they survive a container restart. A
    # volume in the container; any writable directory when the server runs natively.
    state_dir: Path = Field(Path("/state"), validation_alias="STATE_DIR")

    trace_dir: Path | None = Field(None, validation_alias="VOX_TRACE_DIR")
    trace_keep: int = Field(200, validation_alias="VOX_TRACE_KEEP")

    prompt_path: Path = Field(Path("prompt.md"), validation_alias="PROMPT_PATH")
    dictation_prompt_path: Path = Field(
        Path("dictation.md"), validation_alias="DICTATION_PROMPT_PATH"
    )
    analysis_prompt_path: Path = Field(
        Path("analysis.md"), validation_alias="ANALYSIS_PROMPT_PATH"
    )
    glossary_path: Path = Field(Path("config/glossary.yaml"), validation_alias="GLOSSARY_PATH")

    max_audio_bytes: int = Field(25_000_000, validation_alias="MAX_AUDIO_BYTES")
    max_audio_seconds: float = Field(300.0, validation_alias="MAX_AUDIO_SECONDS")
    max_project_bytes: int = Field(8000, validation_alias="MAX_PROJECT_BYTES")
    max_context_bytes: int = Field(6000, validation_alias="MAX_CONTEXT_BYTES")

    # The language the answer is written in when the caller does not ask for one. STT_LANGUAGE
    # stays separate: what was spoken and what the answer is written in need not match.
    default_language: str = Field(DEFAULT_LANGUAGE, validation_alias="DEFAULT_LANGUAGE")

    @field_validator("default_language")
    @classmethod
    def _known_language(cls, value: str) -> str:
        return normalise_language(value) or DEFAULT_LANGUAGE

    @field_validator("trace_dir", mode="before")
    @classmethod
    def _blank_trace_dir_disables_tracing(cls, value: object) -> object:
        # "VOX_TRACE_DIR=" would otherwise parse as Path("."), silently switching on a
        # diagnostic that writes transcripts and prompts into the working directory.
        if isinstance(value, str) and not value.strip():
            return None
        return value


_installed: Settings | None = None


@lru_cache
def get_settings() -> Settings:
    """Return the process-wide Settings.

    Built from the environment unless :func:`set_settings` has installed an instance -
    which is how command-line overrides reach the application. Cached: tests that mutate
    the environment must call ``get_settings.cache_clear()``.
    """
    return _installed if _installed is not None else Settings()


def set_settings(settings: Settings) -> None:
    """Install ``settings`` as the process-wide configuration.

    Must be called before the app starts; clears the :func:`get_settings` cache so the
    new instance is picked up.
    """
    global _installed
    _installed = settings
    get_settings.cache_clear()


def redacted_base_url(url: str) -> str:
    """Return ``url`` with any ``user:password@`` userinfo removed.

    ``http://user:pass@host:11434/v1`` becomes ``http://host:11434/v1``. A URL without
    userinfo, or one that cannot be parsed, is returned unchanged.
    """
    try:
        parts = urlsplit(url)
    except ValueError:
        return url
    if "@" not in parts.netloc:
        return url
    host = parts.netloc.rpartition("@")[2]
    if not host:
        return url
    return urlunsplit((parts.scheme, host, parts.path, parts.query, parts.fragment))
