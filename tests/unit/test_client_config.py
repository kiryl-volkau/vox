from __future__ import annotations

from pathlib import Path

import pytest
import yaml

from voice_code_client.config import ConfigError, default_config, load_config

EXPECTED_BINDINGS = {
    "context": "ctrl+alt+space",
    "dictation": "ctrl+alt+d",
    "clean": "ctrl+alt+c",
    "task": "ctrl+alt+t",
}


def _write(tmp_path: Path, data: object) -> Path:
    path = tmp_path / "client.yaml"
    path.write_text(yaml.safe_dump(data, allow_unicode=True), encoding="utf-8")
    return path


def _write_raw(tmp_path: Path, text: str) -> Path:
    path = tmp_path / "client.yaml"
    path.write_text(text, encoding="utf-8")
    return path


def test_the_defaults_match_the_documented_contract() -> None:
    config = default_config()

    assert config.server.base_url == "http://127.0.0.1:8765"
    assert config.server.connect_timeout_seconds == 3.0
    assert config.server.timeout_seconds == 60.0
    assert config.audio.input_device is None
    assert config.audio.max_seconds == 120.0
    assert config.audio.sample_rate is None
    assert config.audio.channels == 1
    assert config.hotkeys.bindings == EXPECTED_BINDINGS
    assert config.hotkeys.cancel == "esc"
    assert config.paste.enabled is True
    assert config.paste.shortcut == "ctrl+v"
    assert config.paste.preserve_clipboard is True
    assert config.paste.only_if_target_window_unchanged is True
    assert config.paste.restore_delay_ms == 600
    assert config.paste.auto_submit is False
    assert config.overlay.enabled is True
    assert config.overlay.position == "bottom-center"
    assert config.overlay.hide_delay_ms == 1200
    assert config.logging.level == "INFO"
    assert config.logging.log_text is False


def test_a_missing_file_yields_the_defaults(tmp_path: Path) -> None:
    assert load_config(tmp_path / "absent.yaml") == default_config()


def test_an_empty_file_yields_the_defaults(tmp_path: Path) -> None:
    assert load_config(_write_raw(tmp_path, "")) == default_config()


def test_a_partial_file_only_overrides_what_it_names(tmp_path: Path) -> None:
    path = _write(
        tmp_path,
        {"server": {"base_url": "http://10.0.0.5:9000/"}, "paste": {"auto_submit": True}},
    )

    config = load_config(path)

    assert config.server.base_url == "http://10.0.0.5:9000"
    assert config.server.timeout_seconds == 60.0
    assert config.paste.auto_submit is True
    assert config.paste.shortcut == "ctrl+v"
    assert config.audio == default_config().audio
    assert config.hotkeys.bindings == EXPECTED_BINDINGS


def test_unknown_top_level_keys_are_ignored(tmp_path: Path) -> None:
    path = _write(tmp_path, {"nonsense": {"a": 1}, "overlay": {"position": "top-center"}})

    config = load_config(path)

    assert config.overlay.position == "top-center"
    assert config.server == default_config().server


def test_custom_bindings_replace_the_defaults(tmp_path: Path) -> None:
    path = _write(
        tmp_path,
        {"hotkeys": {"bindings": {"context": "ctrl+shift+space"}, "cancel": "escape"}},
    )

    config = load_config(path)

    assert config.hotkeys.bindings == {"context": "ctrl+shift+space"}
    assert config.hotkeys.cancel == "escape"


def test_the_device_name_and_sample_rate_round_trip(tmp_path: Path) -> None:
    path = _write(
        tmp_path,
        {"audio": {"input_device": "Microphone (USB)", "sample_rate": 48000, "channels": 2}},
    )

    config = load_config(path)

    assert config.audio.input_device == "Microphone (USB)"
    assert config.audio.sample_rate == 48000
    assert config.audio.channels == 2


@pytest.mark.parametrize(
    ("document", "key"),
    [
        ({"server": {"base_url": "127.0.0.1:8765"}}, "server.base_url"),
        ({"server": {"base_url": 8765}}, "server.base_url"),
        ({"server": {"connect_timeout_seconds": 0}}, "server.connect_timeout_seconds"),
        ({"server": {"timeout_seconds": -1.0}}, "server.timeout_seconds"),
        ({"server": {"timeout_seconds": "soon"}}, "server.timeout_seconds"),
        ({"server": "http://x"}, "server"),
        ({"audio": {"max_seconds": 0}}, "audio.max_seconds"),
        ({"audio": {"max_seconds": 99999}}, "audio.max_seconds"),
        ({"audio": {"max_seconds": "long"}}, "audio.max_seconds"),
        ({"audio": {"sample_rate": 0}}, "audio.sample_rate"),
        ({"audio": {"sample_rate": 44.1}}, "audio.sample_rate"),
        ({"audio": {"channels": 3}}, "audio.channels"),
        ({"audio": {"input_device": "   "}}, "audio.input_device"),
        ({"audio": {"input_device": True}}, "audio.input_device"),
        ({"hotkeys": {"bindings": {}}}, "hotkeys.bindings"),
        ({"hotkeys": {"bindings": []}}, "hotkeys.bindings"),
        ({"hotkeys": {"bindings": {"context": "ctrl+alt"}}}, "hotkeys.bindings.context"),
        ({"hotkeys": {"bindings": {"context": "ctrl+a+b"}}}, "hotkeys.bindings.context"),
        ({"hotkeys": {"bindings": {"context": 42}}}, "hotkeys.bindings.context"),
        ({"hotkeys": {"cancel": "ctrl+"}}, "hotkeys.cancel"),
        ({"paste": {"shortcut": "ctrl"}}, "paste.shortcut"),
        ({"paste": {"restore_delay_ms": -1}}, "paste.restore_delay_ms"),
        ({"paste": {"restore_delay_ms": 1.5}}, "paste.restore_delay_ms"),
        ({"paste": {"enabled": "yes"}}, "paste.enabled"),
        ({"overlay": {"position": "left"}}, "overlay.position"),
        ({"overlay": {"hide_delay_ms": -5}}, "overlay.hide_delay_ms"),
        ({"logging": {"level": "LOUD"}}, "logging.level"),
        ({"logging": {"log_text": 1}}, "logging.log_text"),
    ],
)
def test_every_invalid_value_names_its_key(
    tmp_path: Path, document: dict[str, object], key: str
) -> None:
    with pytest.raises(ConfigError) as excinfo:
        load_config(_write(tmp_path, document))

    assert key in str(excinfo.value)


def test_a_non_mapping_document_is_rejected(tmp_path: Path) -> None:
    with pytest.raises(ConfigError, match="mapping"):
        load_config(_write(tmp_path, ["server", "audio"]))


def test_invalid_yaml_is_rejected(tmp_path: Path) -> None:
    with pytest.raises(ConfigError, match="invalid YAML"):
        load_config(_write_raw(tmp_path, "server: [unclosed\n  paste: }\n"))


def test_the_example_config_shipped_with_the_repository_loads(repo_root: Path) -> None:
    config = load_config(repo_root / "config" / "client.example.yaml")

    assert config.hotkeys.bindings == EXPECTED_BINDINGS
    assert config.hotkeys.cancel == "esc"
    assert config.paste.auto_submit is False
    assert config.paste.shortcut == "ctrl+v"
    assert config.server.base_url == "http://127.0.0.1:8765"
    assert config.overlay.position in {"bottom-center", "top-center"}
    assert config == default_config()
