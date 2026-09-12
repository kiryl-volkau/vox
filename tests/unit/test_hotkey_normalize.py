from __future__ import annotations

import pytest

from voice_code_client.hotkeys import HotkeyListener, normalize_key
from voice_code_client.state import Hotkey, HotkeyStateMachine, StartRecording


class NamedKey:
    """Stands in for ``pynput.keyboard.Key``, which exposes only ``.name``."""

    def __init__(self, name: str) -> None:
        self.name = name


class CharKey:
    """Stands in for ``pynput.keyboard.KeyCode``, which exposes ``.char`` and ``.vk``."""

    def __init__(self, char: str | None, vk: int | None = None) -> None:
        self.char = char
        self.vk = vk


@pytest.mark.parametrize(
    ("name", "expected"),
    [
        ("ctrl", "ctrl"),
        ("ctrl_l", "ctrl"),
        ("ctrl_r", "ctrl"),
        ("CTRL_L", "ctrl"),
        ("alt", "alt"),
        ("alt_l", "alt"),
        ("alt_gr", "alt"),
        ("shift", "shift"),
        ("shift_r", "shift"),
        ("cmd", "win"),
        ("cmd_l", "win"),
        ("space", "space"),
        ("esc", "esc"),
        ("enter", "enter"),
        ("tab", "tab"),
        ("f1", "f1"),
        ("f12", "f12"),
        ("f24", "f24"),
        ("home", "home"),
    ],
)
def test_named_keys_collapse_onto_canonical_names(name: str, expected: str) -> None:
    assert normalize_key(NamedKey(name)) == expected


@pytest.mark.parametrize(
    "name",
    ["media_play_pause", "print_screen", "caps_lock", "num_lock", "f25", "menu"],
)
def test_untracked_named_keys_are_dropped(name: str) -> None:
    assert normalize_key(NamedKey(name)) is None


@pytest.mark.parametrize(
    ("char", "expected"),
    [("d", "d"), ("D", "d"), ("Q", "q"), ("5", "5"), ("0", "0")],
)
def test_ascii_alphanumeric_characters_come_back_lowercase(char: str, expected: str) -> None:
    assert normalize_key(CharKey(char)) == expected


def test_a_cyrillic_character_resolves_through_the_virtual_key_code() -> None:
    assert normalize_key(CharKey("в", vk=68)) == "d"
    assert normalize_key(CharKey("ц", vk=87)) == "w"
    assert normalize_key(CharKey("Я", vk=90)) == "z"


def test_a_dead_key_with_no_character_resolves_through_the_virtual_key_code() -> None:
    assert normalize_key(CharKey(None, vk=68)) == "d"
    assert normalize_key(CharKey(None, vk=65)) == "a"
    assert normalize_key(CharKey(None, vk=90)) == "z"
    assert normalize_key(CharKey(None, vk=49)) == "1"
    assert normalize_key(CharKey(None, vk=48)) == "0"


def test_the_numeric_keypad_maps_onto_digits() -> None:
    assert normalize_key(CharKey(None, vk=96)) == "0"
    assert normalize_key(CharKey(None, vk=97)) == "1"
    assert normalize_key(CharKey(None, vk=105)) == "9"


def test_a_punctuation_character_falls_through_to_the_virtual_key_code() -> None:
    assert normalize_key(CharKey("!", vk=49)) == "1"


@pytest.mark.parametrize(
    "key",
    [
        CharKey(None),
        CharKey(None, vk=1000),
        CharKey(None, vk=32),
        CharKey("\x04", vk=None),
        object(),
    ],
)
def test_unresolvable_keys_are_dropped(key: object) -> None:
    assert normalize_key(key) is None


def test_a_named_key_wins_over_a_character() -> None:
    class Both:
        name = "space"
        char = "d"
        vk = 68

    assert normalize_key(Both()) == "space"


def _stub(name: str | None = None, char: str | None = None, vk: int | None = None) -> object:
    return NamedKey(name) if name is not None else CharKey(char, vk)


def test_injected_events_are_ignored_so_our_own_paste_does_not_break_the_chord() -> None:
    """send_paste synthesises ctrl+v and the global hook reports it straight back.

    Feeding that synthetic ctrl-up to the machine would clear a ctrl the user is still
    physically holding, and every later chord would then fail to match.
    """
    machine = HotkeyStateMachine({"context": Hotkey.parse("ctrl+alt+space")}, Hotkey.parse("esc"))
    events: list[object] = []
    listener = HotkeyListener(machine, events.append)

    listener._dispatch(_stub(name="ctrl"), pressed=True)
    listener._dispatch(_stub(name="alt"), pressed=True)
    listener._dispatch(_stub(name="space"), pressed=True)
    listener._dispatch(_stub(name="space"), pressed=False)
    machine.processing_started()

    for name, pressed in (("ctrl", True), (None, True), (None, False), ("ctrl", False)):
        stub = _stub(name=name) if name else _stub(char="v", vk=86)
        listener._dispatch(stub, pressed=pressed, injected=True)

    assert machine.held == frozenset({"ctrl", "alt"})

    machine.processing_finished()
    listener._dispatch(_stub(name="space"), pressed=True)

    assert [e for e in events if isinstance(e, StartRecording)] == [
        StartRecording("context"),
        StartRecording("context"),
    ]
