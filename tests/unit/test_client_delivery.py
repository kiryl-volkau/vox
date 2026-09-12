"""Delivery-path behaviour: what reaches the clipboard, the target window, and Enter."""

from __future__ import annotations

from dataclasses import replace
from pathlib import Path
from typing import Any

import pytest

from voice_code_client import main as client_main
from voice_code_client.clipboard import ClipboardError
from voice_code_client.config import default_config


class _Spy:
    def __init__(self) -> None:
        self.clipboard: str | None = "PREVIOUS CLIPBOARD"
        self.pastes: list[str] = []
        self.enters = 0
        self.paste_error: Exception | None = None
        self.foreground = 1000

    def set_text(self, text: str) -> None:
        self.clipboard = text

    def get_text(self) -> str | None:
        return self.clipboard

    def send_paste(self, shortcut: str) -> None:
        if self.paste_error is not None:
            raise self.paste_error
        self.pastes.append(shortcut)

    def send_enter(self) -> None:
        self.enters += 1

    def foreground_window(self) -> int | None:
        return self.foreground


@pytest.fixture
def spy(monkeypatch: pytest.MonkeyPatch) -> _Spy:
    spy = _Spy()
    for name in ("set_text", "send_paste", "send_enter", "foreground_window"):
        monkeypatch.setattr(client_main, name, getattr(spy, name))
    # preserved_clipboard lives in the clipboard module and calls its own get/set.
    monkeypatch.setattr("voice_code_client.clipboard.get_text", spy.get_text)
    monkeypatch.setattr("voice_code_client.clipboard.set_text", spy.set_text)
    return spy


def _app(tmp_path: Path, **paste_overrides: Any) -> client_main.VoiceCodeApp:
    config = default_config()
    config = replace(
        config,
        paste=replace(config.paste, restore_delay_ms=0, **paste_overrides),
        overlay=replace(config.overlay, enabled=False),
    )
    return client_main.VoiceCodeApp(config, config_path=tmp_path / "client.yaml")


def test_a_successful_paste_restores_the_previous_clipboard(tmp_path: Path, spy: _Spy) -> None:
    app = _app(tmp_path)

    app._deliver("НОВЫЙ ПРОМПТ", target_hwnd=spy.foreground)

    assert spy.pastes == ["ctrl+v"]
    assert spy.clipboard == "PREVIOUS CLIPBOARD"


def test_enter_is_never_sent_unless_auto_submit_is_enabled(tmp_path: Path, spy: _Spy) -> None:
    """The whole point of the tool: the user reviews the prompt and submits it themselves."""
    app = _app(tmp_path)

    app._deliver("НОВЫЙ ПРОМПТ", target_hwnd=spy.foreground)

    assert spy.enters == 0


def test_auto_submit_sends_enter_when_explicitly_enabled(tmp_path: Path, spy: _Spy) -> None:
    app = _app(tmp_path, auto_submit=True)

    app._deliver("НОВЫЙ ПРОМПТ", target_hwnd=spy.foreground)

    assert spy.enters == 1


def test_a_changed_foreground_window_copies_instead_of_pasting(tmp_path: Path, spy: _Spy) -> None:
    app = _app(tmp_path)
    spy.foreground = 2222

    app._deliver("НОВЫЙ ПРОМПТ", target_hwnd=1000)

    assert spy.pastes == []
    assert spy.clipboard == "НОВЫЙ ПРОМПТ"


def test_pasting_into_a_changed_window_is_allowed_when_the_guard_is_off(
    tmp_path: Path, spy: _Spy
) -> None:
    app = _app(tmp_path, only_if_target_window_unchanged=False)
    spy.foreground = 2222

    app._deliver("НОВЫЙ ПРОМПТ", target_hwnd=1000)

    assert spy.pastes == ["ctrl+v"]


def test_a_failed_paste_leaves_the_result_on_the_clipboard(tmp_path: Path, spy: _Spy) -> None:
    """Otherwise preserved_clipboard's restore discards the transcription entirely.

    The user would have neither the pasted text nor anything to paste by hand, and would
    have to dictate the whole request again.
    """
    app = _app(tmp_path)
    spy.paste_error = ClipboardError("SendInput refused by UIPI")

    app._deliver("НОВЫЙ ПРОМПТ", target_hwnd=spy.foreground)

    assert spy.pastes == []
    assert spy.clipboard == "НОВЫЙ ПРОМПТ"


def test_paste_disabled_only_copies(tmp_path: Path, spy: _Spy) -> None:
    app = _app(tmp_path, enabled=False)

    app._deliver("НОВЫЙ ПРОМПТ", target_hwnd=spy.foreground)

    assert spy.pastes == []
    assert spy.enters == 0
    assert spy.clipboard == "НОВЫЙ ПРОМПТ"
