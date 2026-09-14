"""The runtime LLM override: the file that wins over .env, and never breaks startup."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from tests.conftest import make_settings
from vox_server import runtime_config
from vox_server.config import Settings
from vox_server.runtime_config import LlmOverride


def _settings(tmp_path: Path, **overrides: object) -> Settings:
    return make_settings(state_dir=tmp_path, **overrides)


def test_no_file_means_no_override(tmp_path: Path) -> None:
    override = runtime_config.load(_settings(tmp_path))

    assert not override
    assert override == LlmOverride()


def test_a_saved_override_round_trips(tmp_path: Path) -> None:
    settings = _settings(tmp_path)
    saved = LlmOverride(base_url="https://api.test/v1", model="gpt-x", api_key="sk-1", warmup=False)

    path = runtime_config.save(settings, saved)

    assert path is not None and path.is_file()
    assert runtime_config.load(settings) == saved


def test_an_override_wins_over_the_environment(tmp_path: Path) -> None:
    settings = _settings(
        tmp_path, llm_base_url="http://local/v1", llm_model="qwen", llm_warmup=True
    )
    override = LlmOverride(base_url="https://api.test/v1", model="gpt-x", warmup=False)

    effective = override.applied_to(settings)

    assert effective.base_url == "https://api.test/v1"
    assert effective.model == "gpt-x"
    assert effective.warmup is False
    assert effective.overridden is True
    # Untouched fields still come from the environment.
    assert effective.api_key == "local"


def test_an_empty_override_leaves_the_environment_alone(tmp_path: Path) -> None:
    settings = _settings(tmp_path, llm_base_url="http://local/v1", llm_model="qwen")

    effective = LlmOverride().applied_to(settings)

    assert effective.base_url == "http://local/v1"
    assert effective.model == "qwen"
    assert effective.overridden is False


def test_an_empty_api_key_is_kept_rather_than_treated_as_absent(tmp_path: Path) -> None:
    """A local server wants no bearer token, and that has to be expressible."""
    settings = _settings(tmp_path, llm_api_key="from-env")
    override = LlmOverride(api_key="")

    assert override.applied_to(settings).api_key == ""
    assert bool(override) is True


def test_merge_replaces_only_the_fields_the_update_states() -> None:
    stored = LlmOverride(base_url="https://one/v1", model="a", api_key="sk-1", warmup=False)

    merged = stored.merge(LlmOverride(model="b"))

    assert merged.base_url == "https://one/v1"
    assert merged.model == "b"
    assert merged.api_key == "sk-1"
    assert merged.warmup is False


@pytest.mark.parametrize(
    "body",
    ["{ not json", '"a string"', "[1, 2]", '{"base_url": 7, "warmup": "yes"}'],
)
def test_an_unusable_file_degrades_to_no_override(tmp_path: Path, body: str) -> None:
    """A corrupt state file must not stop the server from starting on its .env defaults."""
    (tmp_path / runtime_config.FILE_NAME).write_text(body, encoding="utf-8")

    assert not runtime_config.load(_settings(tmp_path))


def test_a_partial_file_keeps_the_fields_it_does_state(tmp_path: Path) -> None:
    (tmp_path / runtime_config.FILE_NAME).write_text('{"model": "gpt-x"}', encoding="utf-8")

    override = runtime_config.load(_settings(tmp_path))

    assert override.model == "gpt-x"
    assert override.base_url is None


def test_saving_omits_the_fields_that_were_never_set(tmp_path: Path) -> None:
    settings = _settings(tmp_path)

    runtime_config.save(settings, LlmOverride(model="gpt-x"))

    body = json.loads((tmp_path / runtime_config.FILE_NAME).read_text(encoding="utf-8"))
    assert body == {"model": "gpt-x"}


def test_clearing_restores_the_environment(tmp_path: Path) -> None:
    settings = _settings(tmp_path)
    runtime_config.save(settings, LlmOverride(model="gpt-x"))

    runtime_config.clear(settings)

    assert not runtime_config.load(settings)


def test_clearing_nothing_is_not_an_error(tmp_path: Path) -> None:
    runtime_config.clear(_settings(tmp_path))


def test_an_unwritable_state_directory_is_reported_rather_than_raised(tmp_path: Path) -> None:
    """A backend that cannot persist must still have applied the change in memory."""
    blocker = tmp_path / "blocked"
    blocker.write_text("not a directory", encoding="utf-8")

    assert runtime_config.save(_settings(blocker), LlmOverride(model="x")) is None
