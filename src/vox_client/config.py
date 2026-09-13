"""Configuration for the Windows companion, loaded from a YAML file.

Every block is optional: a missing file, an empty file or a partial one falls back to the
built-in defaults block by block, so a user file only has to contain what it changes.
"""

from __future__ import annotations

import logging
import sys
from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

from vox_client.hotkeys import TRIGGER_KEYS
from vox_client.project import MAX_PROJECT_BYTES, PROJECT_FILE_NAME
from vox_client.state import Hotkey

MAX_RECORDING_SECONDS = 3600.0
OVERLAY_POSITIONS = frozenset({"bottom-center", "top-center"})


class ConfigError(RuntimeError):
    """Raised when a configuration file exists but cannot be used as written."""


@dataclass(frozen=True, slots=True)
class ServerConfig:
    """Backend endpoint. ``base_url`` is stored without a trailing slash."""

    base_url: str = "http://127.0.0.1:8765"
    connect_timeout_seconds: float = 3.0
    timeout_seconds: float = 180.0


@dataclass(frozen=True, slots=True)
class AudioConfig:
    """Microphone capture.

    ``input_device`` selects the system default when None, a device index when an int, and
    the first input device whose name contains it when a str. ``sample_rate`` None means the
    native rate of that device. ``max_seconds`` is a hard cap on a single recording.
    """

    input_device: int | str | None = None
    max_seconds: float = 120.0
    sample_rate: int | None = None
    channels: int = 1


def _default_bindings() -> dict[str, str]:
    return {
        "context": "ctrl+alt+space",
        "dictation": "ctrl+alt+d",
        "clean": "ctrl+alt+c",
        "task": "ctrl+alt+t",
    }


@dataclass(frozen=True, slots=True)
class HotkeyConfig:
    """Push-to-talk bindings as mode name -> combo string, plus the cancel combo."""

    bindings: dict[str, str] = field(default_factory=_default_bindings)
    cancel: str = "esc"


@dataclass(frozen=True, slots=True)
class PasteConfig:
    """Delivery into the focused window. ``auto_submit`` presses Enter after the paste."""

    enabled: bool = True
    shortcut: str = "ctrl+v"
    preserve_clipboard: bool = True
    only_if_target_window_unchanged: bool = True
    restore_delay_ms: int = 600
    auto_submit: bool = False


@dataclass(frozen=True, slots=True)
class OverlayConfig:
    enabled: bool = True
    position: str = "bottom-center"
    hide_delay_ms: int = 1200


@dataclass(frozen=True, slots=True)
class ProjectConfig:
    """Where to look for a per-project ``.vox.md`` and how much of it to send.

    ``roots`` are directories that may each hold one; the companion picks the one whose
    folder name appears in the title of the focused window. No roots, or
    ``detect_from_window`` false, means no project context is ever sent.
    """

    roots: tuple[str, ...] = ()
    file_name: str = PROJECT_FILE_NAME
    detect_from_window: bool = True
    max_bytes: int = MAX_PROJECT_BYTES


@dataclass(frozen=True, slots=True)
class LoggingConfig:
    """``level`` is a standard logging level name, stored upper-cased."""

    level: str = "INFO"
    log_text: bool = False


@dataclass(frozen=True, slots=True)
class ClientConfig:
    server: ServerConfig = field(default_factory=ServerConfig)
    audio: AudioConfig = field(default_factory=AudioConfig)
    hotkeys: HotkeyConfig = field(default_factory=HotkeyConfig)
    paste: PasteConfig = field(default_factory=PasteConfig)
    overlay: OverlayConfig = field(default_factory=OverlayConfig)
    project: ProjectConfig = field(default_factory=ProjectConfig)
    logging: LoggingConfig = field(default_factory=LoggingConfig)


def default_config() -> ClientConfig:
    """Return the built-in configuration, identical to what an empty config file yields."""
    return ClientConfig()


def default_config_path() -> Path:
    """Return the path used when :func:`load_config` is called without one.

    ``config/client.yaml`` relative to the current directory when it exists there, then
    beside the executable (the PyInstaller build), then the repository checkout, so the
    companion finds its configuration from any working directory.
    """
    local = Path("config/client.yaml")
    if local.is_file():
        return local
    for base in _config_search_roots():
        candidate = base / "config" / "client.yaml"
        if candidate.is_file():
            return candidate
    return Path(__file__).resolve().parents[2] / "config" / "client.yaml"


def _config_search_roots() -> list[Path]:
    roots: list[Path] = []
    if getattr(sys, "frozen", False):
        # In a one-file build __file__ points inside the extraction directory under %TEMP%,
        # so the repository-relative fallback cannot work; look beside the .exe instead.
        executable = Path(sys.executable).resolve().parent
        roots.extend((executable, executable.parent))
    roots.append(Path(__file__).resolve().parents[2])
    return roots


def load_config(path: Path | None = None) -> ClientConfig:
    """Load the client configuration, merging a YAML file over the defaults.

    A missing or empty file yields :func:`default_config`; unknown keys are ignored. Raises
    ConfigError, naming the offending key, for unreadable or malformed YAML and for any value
    that has the wrong type, is out of range, or is an unparseable hotkey.
    """
    target = default_config_path() if path is None else path
    defaults = default_config()
    if not target.is_file():
        return defaults
    try:
        raw = yaml.safe_load(target.read_text(encoding="utf-8"))
    except OSError as exc:
        raise ConfigError(f"cannot read {target}: {exc}") from exc
    except yaml.YAMLError as exc:
        raise ConfigError(f"invalid YAML in {target}: {exc}") from exc
    if raw is None:
        return defaults
    if not isinstance(raw, dict):
        raise ConfigError(f"{target}: the top level must be a mapping of config sections")
    return ClientConfig(
        server=_server(_section(raw, "server"), defaults.server),
        audio=_audio(_section(raw, "audio"), defaults.audio),
        hotkeys=_hotkeys(_section(raw, "hotkeys"), defaults.hotkeys),
        paste=_paste(_section(raw, "paste"), defaults.paste),
        overlay=_overlay(_section(raw, "overlay"), defaults.overlay),
        project=_project(_section(raw, "project"), defaults.project),
        logging=_logging(_section(raw, "logging"), defaults.logging),
    )


def _type_name(value: object) -> str:
    if value is None:
        return "null"
    return type(value).__name__


def _section(data: Mapping[str, Any], name: str) -> Mapping[str, Any]:
    value = data.get(name)
    if value is None:
        return {}
    if not isinstance(value, dict):
        raise ConfigError(f"{name}: expected a mapping, got {_type_name(value)}")
    return value


def _read_str(data: Mapping[str, Any], path: str, key: str, default: str) -> str:
    value = data.get(key, default)
    if not isinstance(value, str):
        raise ConfigError(f"{path}.{key}: expected a string, got {_type_name(value)}")
    return value


def _read_bool(data: Mapping[str, Any], path: str, key: str, default: bool) -> bool:
    value = data.get(key, default)
    if not isinstance(value, bool):
        raise ConfigError(f"{path}.{key}: expected true or false, got {_type_name(value)}")
    return value


def _read_float(data: Mapping[str, Any], path: str, key: str, default: float) -> float:
    value = data.get(key, default)
    if isinstance(value, bool) or not isinstance(value, int | float):
        raise ConfigError(f"{path}.{key}: expected a number, got {_type_name(value)}")
    return float(value)


def _read_int(data: Mapping[str, Any], path: str, key: str, default: int) -> int:
    value = data.get(key, default)
    if isinstance(value, bool) or not isinstance(value, int):
        raise ConfigError(f"{path}.{key}: expected an integer, got {_type_name(value)}")
    return value


def _server(data: Mapping[str, Any], default: ServerConfig) -> ServerConfig:
    base_url = _read_str(data, "server", "base_url", default.base_url).strip()
    if not base_url.startswith("http"):
        raise ConfigError("server.base_url: must be a non-empty URL starting with http")
    connect = _read_float(
        data, "server", "connect_timeout_seconds", default.connect_timeout_seconds
    )
    if connect <= 0:
        raise ConfigError("server.connect_timeout_seconds: must be greater than 0")
    timeout = _read_float(data, "server", "timeout_seconds", default.timeout_seconds)
    if timeout <= 0:
        raise ConfigError("server.timeout_seconds: must be greater than 0")
    return ServerConfig(
        base_url=base_url.rstrip("/"),
        connect_timeout_seconds=connect,
        timeout_seconds=timeout,
    )


def _audio(data: Mapping[str, Any], default: AudioConfig) -> AudioConfig:
    device = data.get("input_device", default.input_device)
    if isinstance(device, bool) or not isinstance(device, int | str | None):
        raise ConfigError(
            "audio.input_device: expected null, a device index or a device name, "
            f"got {_type_name(device)}"
        )
    if isinstance(device, str) and not device.strip():
        raise ConfigError("audio.input_device: a device name must not be empty")

    max_seconds = _read_float(data, "audio", "max_seconds", default.max_seconds)
    if max_seconds <= 0 or max_seconds > MAX_RECORDING_SECONDS:
        raise ConfigError(
            f"audio.max_seconds: must be greater than 0 and at most {MAX_RECORDING_SECONDS:.0f}"
        )

    sample_rate = data.get("sample_rate", default.sample_rate)
    if sample_rate is not None:
        if isinstance(sample_rate, bool) or not isinstance(sample_rate, int):
            raise ConfigError(
                f"audio.sample_rate: expected null or an integer, got {_type_name(sample_rate)}"
            )
        if sample_rate <= 0:
            raise ConfigError("audio.sample_rate: must be greater than 0")

    channels = _read_int(data, "audio", "channels", default.channels)
    if channels not in (1, 2):
        raise ConfigError("audio.channels: must be 1 or 2")

    return AudioConfig(
        input_device=device,
        max_seconds=max_seconds,
        sample_rate=sample_rate,
        channels=channels,
    )


def _check_trigger(where: str, key: str) -> None:
    """Reject a trigger key the keyboard listener never reports.

    Hotkey.parse only checks the shape of the combo, so a binding such as "ctrl+alt+pause"
    would load cleanly and then silently never fire.
    """
    if key not in TRIGGER_KEYS:
        raise ConfigError(
            f"{where}: '{key}' is not a key the listener reports, so the binding would never fire"
        )


def _hotkeys(data: Mapping[str, Any], default: HotkeyConfig) -> HotkeyConfig:
    raw = data.get("bindings")
    if raw is None:
        bindings = dict(default.bindings)
    elif isinstance(raw, dict):
        bindings = {}
        for mode, combo in raw.items():
            if not isinstance(mode, str) or not mode.strip():
                raise ConfigError("hotkeys.bindings: every mode name must be a non-empty string")
            if not isinstance(combo, str):
                raise ConfigError(
                    f"hotkeys.bindings.{mode}: expected a string, got {_type_name(combo)}"
                )
            bindings[mode.strip()] = combo
    else:
        raise ConfigError(f"hotkeys.bindings: expected a mapping, got {_type_name(raw)}")

    if not bindings:
        raise ConfigError("hotkeys.bindings: at least one mode must be bound")
    for mode, combo in bindings.items():
        try:
            parsed = Hotkey.parse(combo)
        except ValueError as exc:
            raise ConfigError(f"hotkeys.bindings.{mode}: {exc}") from exc
        _check_trigger(f"hotkeys.bindings.{mode}", parsed.key)

    cancel = _read_str(data, "hotkeys", "cancel", default.cancel)
    try:
        parsed_cancel = Hotkey.parse(cancel)
    except ValueError as exc:
        raise ConfigError(f"hotkeys.cancel: {exc}") from exc
    _check_trigger("hotkeys.cancel", parsed_cancel.key)

    return HotkeyConfig(bindings=bindings, cancel=cancel)


def _paste(data: Mapping[str, Any], default: PasteConfig) -> PasteConfig:
    shortcut = _read_str(data, "paste", "shortcut", default.shortcut)
    # Validated with the paste layer's own parser, not the hotkey grammar: the hotkey grammar
    # accepts chords such as "ctrl+b" that SendInput has no mapping for, which used to be
    # discovered only at delivery time, once per failed request.
    from vox_client.clipboard import ClipboardError, parse_shortcut

    try:
        parse_shortcut(shortcut)
    except ClipboardError as exc:
        raise ConfigError(f"paste.shortcut: {exc}") from exc
    restore_delay_ms = _read_int(data, "paste", "restore_delay_ms", default.restore_delay_ms)
    if restore_delay_ms < 0:
        raise ConfigError("paste.restore_delay_ms: must not be negative")
    return PasteConfig(
        enabled=_read_bool(data, "paste", "enabled", default.enabled),
        shortcut=shortcut,
        preserve_clipboard=_read_bool(
            data, "paste", "preserve_clipboard", default.preserve_clipboard
        ),
        only_if_target_window_unchanged=_read_bool(
            data,
            "paste",
            "only_if_target_window_unchanged",
            default.only_if_target_window_unchanged,
        ),
        restore_delay_ms=restore_delay_ms,
        auto_submit=_read_bool(data, "paste", "auto_submit", default.auto_submit),
    )


def _overlay(data: Mapping[str, Any], default: OverlayConfig) -> OverlayConfig:
    position = _read_str(data, "overlay", "position", default.position).strip().lower()
    if position not in OVERLAY_POSITIONS:
        allowed = ", ".join(sorted(OVERLAY_POSITIONS))
        raise ConfigError(f"overlay.position: must be one of {allowed}")
    hide_delay_ms = _read_int(data, "overlay", "hide_delay_ms", default.hide_delay_ms)
    if hide_delay_ms < 0:
        raise ConfigError("overlay.hide_delay_ms: must not be negative")
    return OverlayConfig(
        enabled=_read_bool(data, "overlay", "enabled", default.enabled),
        position=position,
        hide_delay_ms=hide_delay_ms,
    )


def _project(data: Mapping[str, Any], default: ProjectConfig) -> ProjectConfig:
    raw = data.get("roots")
    if raw is None:
        roots = default.roots
    elif isinstance(raw, list):
        for entry in raw:
            if not isinstance(entry, str) or not entry.strip():
                raise ConfigError(
                    "project.roots: every entry must be a non-empty directory path, "
                    f"got {_type_name(entry)}"
                )
        roots = tuple(entry.strip() for entry in raw)
    else:
        raise ConfigError(f"project.roots: expected a list of paths, got {_type_name(raw)}")

    file_name = _read_str(data, "project", "file_name", default.file_name).strip()
    if not file_name:
        raise ConfigError("project.file_name: must not be empty")
    if "/" in file_name or "\\" in file_name:
        raise ConfigError("project.file_name: must be a file name, not a path")

    max_bytes = _read_int(data, "project", "max_bytes", default.max_bytes)
    if max_bytes <= 0:
        raise ConfigError("project.max_bytes: must be greater than 0")

    return ProjectConfig(
        roots=roots,
        file_name=file_name,
        detect_from_window=_read_bool(
            data, "project", "detect_from_window", default.detect_from_window
        ),
        max_bytes=max_bytes,
    )


def _logging(data: Mapping[str, Any], default: LoggingConfig) -> LoggingConfig:
    level = _read_str(data, "logging", "level", default.level).strip().upper()
    if level not in logging.getLevelNamesMapping():
        raise ConfigError(f"logging.level: unknown level name {level!r}")
    return LoggingConfig(
        level=level,
        log_text=_read_bool(data, "logging", "log_text", default.log_text),
    )
