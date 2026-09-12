from __future__ import annotations

import pytest

from voice_code_client.state import (
    CancelRecording,
    Hotkey,
    HotkeyStateMachine,
    Ignore,
    Phase,
    StartRecording,
    StopRecording,
    canonical_key,
)

BINDINGS = {
    "context": "ctrl+alt+space",
    "dictation": "ctrl+alt+d",
    "clean": "ctrl+alt+c",
    "task": "ctrl+alt+t",
}


def _machine(cancel: str | None = "esc") -> HotkeyStateMachine:
    bindings = {mode: Hotkey.parse(spec) for mode, spec in BINDINGS.items()}
    return HotkeyStateMachine(bindings, Hotkey.parse(cancel) if cancel else None)


def _start(machine: HotkeyStateMachine, trigger: str = "space") -> object:
    machine.key_down("ctrl")
    machine.key_down("alt")
    return machine.key_down(trigger)


@pytest.mark.parametrize(
    ("spec", "mods", "key"),
    [
        ("ctrl+alt+space", {"ctrl", "alt"}, "space"),
        ("  CTRL + ALT + SPACE  ", {"ctrl", "alt"}, "space"),
        ("control+option+escape", {"ctrl", "alt"}, "esc"),
        ("cmd+d", {"win"}, "d"),
        ("super+q", {"win"}, "q"),
        ("meta+shift+f5", {"win", "shift"}, "f5"),
        ("windows+spacebar", {"win"}, "space"),
        ("esc", set(), "esc"),
        ("ctrl+alt+shift+win+d", {"ctrl", "alt", "shift", "win"}, "d"),
    ],
)
def test_hotkey_parse_canonicalises_the_spec(spec: str, mods: set[str], key: str) -> None:
    hotkey = Hotkey.parse(spec)

    assert hotkey.mods == frozenset(mods)
    assert hotkey.key == key


@pytest.mark.parametrize("spec", ["ctrl+alt", "ctrl+", "", "   ", "shift"])
def test_hotkey_parse_rejects_a_spec_without_a_trigger(spec: str) -> None:
    with pytest.raises(ValueError, match="no trigger key"):
        Hotkey.parse(spec)


@pytest.mark.parametrize("spec", ["ctrl+a+b", "space+d", "ctrl+alt+space+enter"])
def test_hotkey_parse_rejects_more_than_one_trigger(spec: str) -> None:
    with pytest.raises(ValueError, match="more than one trigger key"):
        Hotkey.parse(spec)


def test_hotkey_str_uses_the_canonical_modifier_order() -> None:
    assert str(Hotkey.parse("win+shift+alt+ctrl+d")) == "ctrl+alt+shift+win+d"
    assert str(Hotkey.parse("space")) == "space"
    assert str(Hotkey.parse("ALT+ctrl+SPACE")) == "ctrl+alt+space"


def test_canonical_key_resolves_aliases_and_leaves_the_rest_alone() -> None:
    assert canonical_key("  CTRL_L ") == "ctrl"
    assert canonical_key("Escape") == "esc"
    assert canonical_key("q") == "q"
    assert canonical_key("f9") == "f9"


def test_a_modifier_alone_starts_nothing() -> None:
    machine = _machine()

    assert isinstance(machine.key_down("ctrl"), Ignore)
    assert isinstance(machine.key_down("alt"), Ignore)
    assert machine.phase is Phase.IDLE
    assert machine.held == frozenset({"ctrl", "alt"})


def test_a_trigger_without_its_modifiers_starts_nothing() -> None:
    machine = _machine()

    assert isinstance(machine.key_down("space"), Ignore)
    assert machine.phase is Phase.IDLE


def test_a_partial_chord_starts_nothing() -> None:
    machine = _machine()
    machine.key_down("ctrl")

    assert isinstance(machine.key_down("space"), Ignore)
    assert machine.phase is Phase.IDLE


def test_the_full_chord_starts_recording() -> None:
    machine = _machine()

    event = _start(machine)

    assert event == StartRecording("context")
    assert machine.phase is Phase.RECORDING
    assert machine.active_mode == "context"


def test_each_binding_starts_its_own_mode() -> None:
    for trigger, mode in (("d", "dictation"), ("c", "clean"), ("t", "task")):
        machine = _machine()

        assert _start(machine, trigger) == StartRecording(mode)


def test_an_extra_modifier_does_not_match_the_binding() -> None:
    machine = _machine()
    machine.key_down("ctrl")
    machine.key_down("alt")
    machine.key_down("shift")

    assert isinstance(machine.key_down("space"), Ignore)
    assert machine.phase is Phase.IDLE


def test_auto_repeat_starts_recording_exactly_once() -> None:
    machine = _machine()
    events = [_start(machine)]
    events.extend(machine.key_down("space") for _ in range(5))

    assert events[0] == StartRecording("context")
    assert all(isinstance(event, Ignore) for event in events[1:])
    assert machine.phase is Phase.RECORDING


def test_releasing_the_trigger_stops_recording() -> None:
    machine = _machine()
    _start(machine)

    assert machine.key_up("space") == StopRecording("context")


def test_releasing_the_trigger_while_modifiers_are_held_stops_recording() -> None:
    machine = _machine()
    _start(machine)

    assert machine.key_up("space") == StopRecording("context")
    assert machine.held == frozenset({"ctrl", "alt"})


@pytest.mark.parametrize("order", [("ctrl", "alt"), ("alt", "ctrl")])
def test_modifiers_may_be_released_first_in_any_order(order: tuple[str, str]) -> None:
    machine = _machine()
    _start(machine)

    for modifier in order:
        assert isinstance(machine.key_up(modifier), Ignore)
        assert machine.phase is Phase.RECORDING

    assert machine.key_up("space") == StopRecording("context")


def test_releasing_another_key_does_not_stop_recording() -> None:
    machine = _machine()
    _start(machine)

    assert isinstance(machine.key_up("d"), Ignore)
    assert machine.phase is Phase.RECORDING
    assert machine.key_up("space") == StopRecording("context")


def test_another_modes_chord_is_ignored_while_recording() -> None:
    machine = _machine()
    _start(machine)

    assert isinstance(machine.key_down("d"), Ignore)
    assert machine.active_mode == "context"


def test_another_modes_chord_is_ignored_while_processing() -> None:
    machine = _machine()
    _start(machine)
    machine.key_up("space")
    machine.processing_started()

    assert machine.phase is Phase.PROCESSING
    assert isinstance(machine.key_down("d"), Ignore)
    assert isinstance(machine.key_down("space"), Ignore)


def test_escape_cancels_an_in_flight_recording() -> None:
    machine = _machine()
    _start(machine)

    assert machine.key_down("esc") == CancelRecording("context")
    assert machine.phase is Phase.IDLE
    assert machine.active_mode is None


def test_escape_is_ignored_while_idle() -> None:
    machine = _machine()

    assert isinstance(machine.key_down("esc"), Ignore)
    assert machine.phase is Phase.IDLE


def test_escape_is_ignored_while_processing() -> None:
    machine = _machine()
    _start(machine)
    machine.key_up("space")
    machine.processing_started()

    assert isinstance(machine.key_down("esc"), Ignore)
    assert machine.phase is Phase.PROCESSING


def test_a_cancelled_recording_does_not_restart_from_auto_repeat() -> None:
    machine = _machine()
    _start(machine)
    machine.key_down("esc")

    assert isinstance(machine.key_down("space"), Ignore)
    assert machine.phase is Phase.IDLE

    assert isinstance(machine.key_up("space"), Ignore)
    assert machine.key_down("space") == StartRecording("context")


def test_the_cancel_key_is_optional() -> None:
    machine = _machine(cancel=None)
    _start(machine)

    assert isinstance(machine.key_down("esc"), Ignore)
    assert machine.phase is Phase.RECORDING


def test_releasing_a_key_that_was_never_down_is_ignored() -> None:
    machine = _machine()

    assert isinstance(machine.key_up("space"), Ignore)
    assert isinstance(machine.key_up("ctrl"), Ignore)
    assert isinstance(machine.key_up("f12"), Ignore)
    assert machine.phase is Phase.IDLE


def test_an_unknown_key_is_ignored() -> None:
    machine = _machine()
    machine.key_down("ctrl")
    machine.key_down("alt")

    assert isinstance(machine.key_down("z"), Ignore)
    assert isinstance(machine.key_down(""), Ignore)
    assert machine.phase is Phase.IDLE


def _phase(machine: HotkeyStateMachine) -> Phase:
    # Read through a call so mypy does not narrow the property across the mutating
    # processing_started/processing_finished transitions.
    return machine.phase


def test_a_full_cycle_can_be_repeated() -> None:
    machine = _machine()

    for _ in range(3):
        assert _start(machine) == StartRecording("context")
        assert machine.key_up("space") == StopRecording("context")
        machine.processing_started()
        assert _phase(machine) is Phase.PROCESSING
        machine.processing_finished()
        assert _phase(machine) is Phase.IDLE
        machine.key_up("ctrl")
        machine.key_up("alt")

    assert machine.active_mode is None


def test_processing_finished_recovers_from_any_phase() -> None:
    machine = _machine()
    _start(machine)

    machine.processing_finished()

    assert machine.phase is Phase.IDLE
    assert machine.active_mode is None


def test_reset_clears_the_held_modifiers_and_the_active_recording() -> None:
    machine = _machine()
    _start(machine)

    machine.reset()

    assert machine.phase is Phase.IDLE
    assert machine.active_mode is None
    assert machine.held == frozenset()
    assert isinstance(machine.key_down("space"), Ignore)


def test_recording_is_not_stopped_by_the_cancel_key_release() -> None:
    machine = _machine()
    _start(machine)
    machine.key_down("esc")

    assert isinstance(machine.key_up("esc"), Ignore)
    assert machine.phase is Phase.IDLE


def test_a_trigger_pressed_during_processing_cannot_auto_start_the_next_recording() -> None:
    """Holding the trigger through PROCESSING must not open the recorder mid-sentence.

    The press is ignored while busy, but the key is still physically down when the worker
    finishes, so its next auto-repeat would otherwise fire StartRecording and capture only
    the tail of what the user is saying.
    """
    machine = _machine()
    assert _start(machine) == StartRecording("context")
    assert machine.key_up("space") == StopRecording("context")
    machine.processing_started()

    assert isinstance(machine.key_down("space"), Ignore)
    assert isinstance(machine.key_down("space"), Ignore)
    machine.processing_finished()

    assert isinstance(machine.key_down("space"), Ignore)

    machine.key_up("space")
    assert machine.key_down("space") == StartRecording("context")
