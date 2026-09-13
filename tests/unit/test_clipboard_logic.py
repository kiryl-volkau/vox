from __future__ import annotations

import sys

import pytest

from vox_client import clipboard
from vox_client.clipboard import (
    VK_CONTROL,
    VK_INSERT,
    VK_SHIFT,
    VK_V,
    ClipboardError,
    parse_shortcut,
    preserved_clipboard,
)


@pytest.mark.parametrize(
    ("spec", "modifiers", "trigger"),
    [
        ("ctrl+v", [VK_CONTROL], VK_V),
        ("CTRL + V", [VK_CONTROL], VK_V),
        ("control+v", [VK_CONTROL], VK_V),
        ("shift+insert", [VK_SHIFT], VK_INSERT),
        ("shift+ins", [VK_SHIFT], VK_INSERT),
        ("ctrl+shift+v", [VK_CONTROL, VK_SHIFT], VK_V),
        ("shift+ctrl+v", [VK_SHIFT, VK_CONTROL], VK_V),
    ],
)
def test_parse_shortcut_returns_the_virtual_key_codes(
    spec: str, modifiers: list[int], trigger: int
) -> None:
    assert parse_shortcut(spec) == (modifiers, trigger)


@pytest.mark.parametrize(
    "spec",
    ["", "   ", "+", "ctrl", "ctrl+b", "v", "insert", "ctrl+v+insert", "ctrl+enter"],
)
def test_parse_shortcut_rejects_what_it_cannot_synthesise(spec: str) -> None:
    with pytest.raises(ClipboardError):
        parse_shortcut(spec)


def test_parse_shortcut_does_not_repeat_a_modifier() -> None:
    assert parse_shortcut("ctrl+ctrl+v") == ([VK_CONTROL], VK_V)


def test_preserved_clipboard_restores_the_previous_text(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    written: list[str] = []
    monkeypatch.setattr(clipboard, "get_text", lambda: "то, что было раньше")
    monkeypatch.setattr(clipboard, "set_text", written.append)

    with preserved_clipboard(enabled=True, restore_delay_ms=0):
        written.append("новый результат")

    assert written == ["новый результат", "то, что было раньше"]


def test_preserved_clipboard_does_nothing_when_disabled(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[str] = []

    def reader() -> str:
        calls.append("read")
        return "старое"

    monkeypatch.setattr(clipboard, "get_text", reader)
    monkeypatch.setattr(clipboard, "set_text", lambda text: calls.append(f"write:{text}"))

    with preserved_clipboard(enabled=False, restore_delay_ms=0):
        pass

    assert calls == []


def test_preserved_clipboard_restores_nothing_when_the_clipboard_held_no_text(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    written: list[str] = []
    monkeypatch.setattr(clipboard, "get_text", lambda: None)
    monkeypatch.setattr(clipboard, "set_text", written.append)

    with preserved_clipboard(enabled=True, restore_delay_ms=0):
        pass

    assert written == []


def test_a_capture_failure_never_breaks_the_delivery(monkeypatch: pytest.MonkeyPatch) -> None:
    written: list[str] = []

    def broken_reader() -> str:
        raise ClipboardError("clipboard locked")

    monkeypatch.setattr(clipboard, "get_text", broken_reader)
    monkeypatch.setattr(clipboard, "set_text", written.append)

    with preserved_clipboard(enabled=True, restore_delay_ms=0):
        written.append("новый результат")

    assert written == ["новый результат"]


def test_a_restore_failure_never_breaks_the_delivery(monkeypatch: pytest.MonkeyPatch) -> None:
    attempts: list[str] = []

    def broken_writer(text: str) -> None:
        attempts.append(text)
        raise ClipboardError("clipboard locked")

    monkeypatch.setattr(clipboard, "get_text", lambda: "старое")
    monkeypatch.setattr(clipboard, "set_text", broken_writer)

    with preserved_clipboard(enabled=True, restore_delay_ms=0):
        pass

    assert attempts == ["старое"]


def test_an_exception_inside_the_body_still_restores(monkeypatch: pytest.MonkeyPatch) -> None:
    written: list[str] = []
    monkeypatch.setattr(clipboard, "get_text", lambda: "старое")
    monkeypatch.setattr(clipboard, "set_text", written.append)

    with pytest.raises(RuntimeError), preserved_clipboard(enabled=True, restore_delay_ms=0):
        raise RuntimeError("paste failed")

    assert written == ["старое"]


def test_a_missing_pywin32_is_reported_as_a_clipboard_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setitem(sys.modules, "win32clipboard", None)

    with pytest.raises(ClipboardError, match="pywin32"):
        clipboard.get_text()

    with pytest.raises(ClipboardError, match="pywin32"):
        clipboard.set_text("что-нибудь")
