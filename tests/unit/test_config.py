from __future__ import annotations

from pathlib import Path

import pytest
from pydantic import SecretStr, ValidationError

from voice_code_server.config import Settings, get_settings, redacted_base_url


def test_defaults_match_the_documented_contract() -> None:
    settings = Settings(_env_file=None)

    assert settings.host == "0.0.0.0"
    assert settings.port == 8765
    assert settings.stt_model == "turbo"
    assert settings.stt_language == "ru"
    assert settings.stt_device == "auto"
    assert settings.stt_compute_type == "float16"
    assert settings.stt_beam_size == 1
    assert settings.stt_vad_filter is True
    assert settings.stt_glossary_hotwords is True
    assert settings.llm_base_url == "http://host.docker.internal:11434/v1"
    assert settings.llm_model == "qwen2.5:7b-instruct"
    assert settings.llm_api_key.get_secret_value() == "local"
    assert settings.llm_timeout_seconds == 120.0
    assert settings.llm_temperature == 0.1
    assert settings.llm_max_tokens == 1024
    assert settings.processing_concurrency == 1
    assert settings.log_level == "INFO"
    assert settings.log_text is False
    assert settings.modes_dir == Path("modes")
    assert settings.glossary_path == Path("config/glossary.yaml")
    assert settings.max_audio_bytes == 25_000_000
    assert settings.max_audio_seconds == 300.0


def test_every_field_can_come_from_the_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("VOICE_CODE_HOST", "127.0.0.1")
    monkeypatch.setenv("VOICE_CODE_PORT", "9100")
    monkeypatch.setenv("STT_MODEL", "tiny")
    monkeypatch.setenv("STT_LANGUAGE", "en")
    monkeypatch.setenv("STT_DEVICE", "cpu")
    monkeypatch.setenv("STT_COMPUTE_TYPE", "int8")
    monkeypatch.setenv("STT_BEAM_SIZE", "5")
    monkeypatch.setenv("STT_VAD_FILTER", "false")
    monkeypatch.setenv("STT_GLOSSARY_HOTWORDS", "0")
    monkeypatch.setenv("LLM_BASE_URL", "http://ollama:11434/v1")
    monkeypatch.setenv("LLM_MODEL", "qwen3:14b")
    monkeypatch.setenv("LLM_API_KEY", "sk-from-env")
    monkeypatch.setenv("LLM_TIMEOUT_SECONDS", "12.5")
    monkeypatch.setenv("LLM_TEMPERATURE", "0.7")
    monkeypatch.setenv("LLM_MAX_TOKENS", "2048")
    monkeypatch.setenv("PROCESSING_CONCURRENCY", "4")
    monkeypatch.setenv("LOG_LEVEL", "DEBUG")
    monkeypatch.setenv("LOG_TEXT", "true")
    monkeypatch.setenv("MODES_DIR", "/srv/modes")
    monkeypatch.setenv("GLOSSARY_PATH", "/srv/glossary.yaml")
    monkeypatch.setenv("MAX_AUDIO_BYTES", "1024")
    monkeypatch.setenv("MAX_AUDIO_SECONDS", "42.5")

    settings = Settings(_env_file=None)

    assert settings.host == "127.0.0.1"
    assert settings.port == 9100
    assert settings.stt_model == "tiny"
    assert settings.stt_language == "en"
    assert settings.stt_device == "cpu"
    assert settings.stt_compute_type == "int8"
    assert settings.stt_beam_size == 5
    assert settings.stt_vad_filter is False
    assert settings.stt_glossary_hotwords is False
    assert settings.llm_base_url == "http://ollama:11434/v1"
    assert settings.llm_model == "qwen3:14b"
    assert settings.llm_api_key.get_secret_value() == "sk-from-env"
    assert settings.llm_timeout_seconds == 12.5
    assert settings.llm_temperature == 0.7
    assert settings.llm_max_tokens == 2048
    assert settings.processing_concurrency == 4
    assert settings.log_level == "DEBUG"
    assert settings.log_text is True
    assert settings.modes_dir == Path("/srv/modes")
    assert settings.glossary_path == Path("/srv/glossary.yaml")
    assert settings.max_audio_bytes == 1024
    assert settings.max_audio_seconds == 42.5


def test_environment_names_are_case_insensitive(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("stt_model", "small")
    monkeypatch.setenv("Llm_Model", "mistral")

    settings = Settings(_env_file=None)

    assert settings.stt_model == "small"
    assert settings.llm_model == "mistral"


def test_fields_can_be_passed_by_name() -> None:
    settings = Settings(
        _env_file=None,
        stt_model="medium",
        processing_concurrency=3,
        llm_api_key=SecretStr("sk-inline"),
        modes_dir=Path("/tmp/modes"),
    )

    assert settings.stt_model == "medium"
    assert settings.processing_concurrency == 3
    assert settings.llm_api_key.get_secret_value() == "sk-inline"
    assert settings.modes_dir == Path("/tmp/modes")


def test_named_kwargs_win_over_the_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("STT_MODEL", "from-env")

    assert Settings(_env_file=None, stt_model="from-kwargs").stt_model == "from-kwargs"


def test_unknown_environment_names_are_ignored(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("TOTALLY_UNRELATED", "x")

    assert Settings(_env_file=None).stt_model == "turbo"


def test_stt_device_is_restricted_to_the_documented_values() -> None:
    with pytest.raises(ValidationError):
        Settings(_env_file=None, stt_device="gpu")


def test_the_api_key_is_not_leaked_by_repr(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("LLM_API_KEY", "sk-do-not-print")

    settings = Settings(_env_file=None)

    assert "sk-do-not-print" not in repr(settings)
    assert "sk-do-not-print" not in str(settings.llm_api_key)
    assert "sk-do-not-print" not in settings.model_dump_json()


@pytest.mark.parametrize(
    ("url", "expected"),
    [
        ("http://host.docker.internal:11434/v1", "http://host.docker.internal:11434/v1"),
        ("http://user:pass@ollama:11434/v1", "http://ollama:11434/v1"),
        ("https://sk-token@api.example.com/v1", "https://api.example.com/v1"),
        ("http://user:p%40ss@ollama:11434/v1", "http://ollama:11434/v1"),
        ("http://user:pass@ollama:11434/v1?verbose=1", "http://ollama:11434/v1?verbose=1"),
        ("", ""),
        ("not a url at all", "not a url at all"),
        ("http://ollama:11434/v1/", "http://ollama:11434/v1/"),
    ],
)
def test_redacted_base_url_strips_credentials(url: str, expected: str) -> None:
    assert redacted_base_url(url) == expected


def test_redacted_base_url_keeps_a_url_whose_userinfo_is_empty() -> None:
    assert "@" not in redacted_base_url("http://user:pass@ollama:11434/v1")


def test_get_settings_is_cached_until_cleared(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("STT_MODEL", "tiny")
    first = get_settings()
    second = get_settings()

    assert first is second
    assert first.stt_model == "tiny"

    monkeypatch.setenv("STT_MODEL", "small")
    assert get_settings() is first
    assert get_settings().stt_model == "tiny"

    get_settings.cache_clear()
    refreshed = get_settings()

    assert refreshed is not first
    assert refreshed.stt_model == "small"
