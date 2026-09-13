"""Picking the right .vox.md from the window title, and never failing a request over it."""

from __future__ import annotations

import logging
from dataclasses import replace
from pathlib import Path
from typing import Any

import pytest

from vox_client import main as client_main
from vox_client.config import default_config
from vox_client.project import PROJECT_FILE_NAME, resolve_project

VOX_MD = "# jigward\n\nmembership - подписка пользователя, не участие в группе.\n"


def _project(tmp_path: Path, name: str, text: str | None = VOX_MD) -> Path:
    root = tmp_path / name
    root.mkdir(parents=True, exist_ok=True)
    if text is not None:
        (root / PROJECT_FILE_NAME).write_text(text, encoding="utf-8")
    return root


def test_the_root_named_in_the_window_title_is_read(tmp_path: Path) -> None:
    root = _project(tmp_path, "jigward")
    other = _project(tmp_path, "vox", "# vox\n")

    name, text = resolve_project([other, root], "jigward – MembershipService.java")

    assert name == "jigward"
    assert text == VOX_MD


def test_the_folder_name_is_matched_case_insensitively(tmp_path: Path) -> None:
    root = _project(tmp_path, "JigWard")

    name, text = resolve_project([root], "Working on jigward in the terminal")

    assert name == "JigWard"
    assert text == VOX_MD


def test_the_longest_matching_folder_name_wins(tmp_path: Path) -> None:
    short = _project(tmp_path, "vox", "# vox\n")
    long = _project(tmp_path, "vox-client", "# vox-client\n")

    name, text = resolve_project([short, long], "vox-client – main.py")

    assert name == "vox-client"
    assert text == "# vox-client\n"


def test_a_title_naming_no_root_yields_nothing(tmp_path: Path) -> None:
    root = _project(tmp_path, "jigward")

    assert resolve_project([root], "Telegram (14)") == (None, None)


def test_no_roots_at_all_yields_nothing() -> None:
    assert resolve_project([], "jigward – Main.java") == (None, None)


def test_an_empty_window_title_yields_nothing(tmp_path: Path) -> None:
    root = _project(tmp_path, "jigward")

    assert resolve_project([root], "") == (None, None)


def test_detection_turned_off_reads_nothing(tmp_path: Path) -> None:
    root = _project(tmp_path, "jigward")

    result = resolve_project([root], "jigward – Main.java", detect_from_window=False)

    assert result == (None, None)


def test_a_root_without_the_file_yields_nothing(tmp_path: Path) -> None:
    root = _project(tmp_path, "jigward", text=None)

    assert resolve_project([root], "jigward – Main.java") == (None, None)


def test_an_empty_file_yields_nothing(tmp_path: Path) -> None:
    root = _project(tmp_path, "jigward", "   \n\n")

    assert resolve_project([root], "jigward – Main.java") == (None, None)


def test_an_unreadable_file_is_a_warning_not_a_failure(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    """A .vox.md that cannot be opened - here a directory wearing its name - is not fatal."""
    root = tmp_path / "jigward"
    (root / PROJECT_FILE_NAME).mkdir(parents=True)

    with caplog.at_level(logging.WARNING, logger="vox_client.project"):
        result = resolve_project([root], "jigward – Main.java")

    assert result == (None, None)
    assert caplog.records


def test_a_file_that_is_not_utf8_is_a_warning_not_a_failure(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    root = tmp_path / "jigward"
    root.mkdir()
    (root / PROJECT_FILE_NAME).write_bytes(b"\xff\xfe membership \x00")

    with caplog.at_level(logging.WARNING, logger="vox_client.project"):
        result = resolve_project([root], "jigward – Main.java")

    assert result == (None, None)
    assert caplog.records


def test_an_oversized_file_is_cut_on_a_line_boundary(tmp_path: Path) -> None:
    body = "".join(f"line {index:03d} padded to a fixed width\n" for index in range(100))
    root = _project(tmp_path, "jigward", body)

    name, text = resolve_project([root], "jigward – Main.java", max_bytes=200)

    assert name == "jigward"
    assert text is not None
    assert len(text.encode("utf-8")) <= 200
    assert text.splitlines() == [f"line {index:03d} padded to a fixed width" for index in range(6)]


def test_truncation_never_leaves_half_a_character(tmp_path: Path) -> None:
    """Russian is two bytes per letter, so a byte cut lands inside a character routinely."""
    root = _project(tmp_path, "jigward", "подписка пользователя без единого переноса строки")

    name, text = resolve_project([root], "jigward – Main.java", max_bytes=15)

    assert name == "jigward"
    assert text is not None
    assert text == "подписка"[: len(text)]
    assert len(text.encode("utf-8")) <= 15


def test_a_custom_file_name_is_honoured(tmp_path: Path) -> None:
    root = tmp_path / "jigward"
    root.mkdir()
    (root / "PROJECT.md").write_text(VOX_MD, encoding="utf-8")

    assert resolve_project([root], "jigward – Main.java") == (None, None)
    assert resolve_project([root], "jigward – Main.java", "PROJECT.md") == ("jigward", VOX_MD)


def test_roots_may_be_plain_strings(tmp_path: Path) -> None:
    root = _project(tmp_path, "jigward")

    name, text = resolve_project([str(root)], "jigward – Main.java")

    assert name == "jigward"
    assert text == VOX_MD


def _app(tmp_path: Path, **project_overrides: Any) -> client_main.VoiceCodeApp:
    config = default_config()
    config = replace(
        config,
        overlay=replace(config.overlay, enabled=False),
        project=replace(config.project, **project_overrides),
    )
    return client_main.VoiceCodeApp(config, config_path=tmp_path / "client.yaml")


def test_the_companion_reads_the_project_of_the_window_it_recorded_from(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = _project(tmp_path, "jigward")
    monkeypatch.setattr(client_main, "window_title", lambda _hwnd: "jigward – Main.java")
    app = _app(tmp_path, roots=(str(root),))

    context = app._resolve_project(1000)

    assert context.name == "jigward"
    assert context.text == VOX_MD


def test_a_lookup_that_blows_up_costs_the_request_nothing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def explode(_hwnd: int) -> str:
        raise OSError("user32 said no")

    root = _project(tmp_path, "jigward")
    monkeypatch.setattr(client_main, "window_title", explode)
    app = _app(tmp_path, roots=(str(root),))

    context = app._resolve_project(1000)

    assert (context.name, context.text) == (None, None)


def test_no_configured_roots_means_the_window_title_is_never_even_read(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def explode(_hwnd: int) -> str:
        raise AssertionError("the title must not be read when no root is configured")

    monkeypatch.setattr(client_main, "window_title", explode)
    app = _app(tmp_path)

    assert app._resolve_project(1000) == client_main._ProjectContext()
