from __future__ import annotations

import argparse
import logging
import os
import sys
import tempfile
import threading
import time
from logging.handlers import RotatingFileHandler
from pathlib import Path
from typing import Any

from voice_code_client.api_client import (
    CLIENT_VERSION,
    ApiError,
    ApiTimeoutError,
    ApiUnavailableError,
    ProcessResult,
    VoiceCodeClient,
)
from voice_code_client.clipboard import (
    ClipboardError,
    foreground_window,
    preserved_clipboard,
    send_enter,
    send_paste,
    set_text,
)
from voice_code_client.config import (
    ClientConfig,
    ConfigError,
    LoggingConfig,
    default_config_path,
    load_config,
)
from voice_code_client.hotkeys import HotkeyListener
from voice_code_client.overlay import Overlay
from voice_code_client.recorder import Recorder, RecorderError, list_input_devices
from voice_code_client.state import (
    CancelRecording,
    Hotkey,
    HotkeyStateMachine,
    StartRecording,
    StopRecording,
)

logger = logging.getLogger("voice_code_client")

_TICK_MS = 200
_FORMATTING_HINT_S = 0.9
_RETRY_DELAY_S = 1.0
_MODIFIER_RELEASE_TIMEOUT_S = 1.0


class VoiceCodeApp:
    """The Windows companion: hotkeys, microphone, backend call and paste delivery.

    The tk overlay owns the main thread; the pynput listener and each backend request run
    on their own threads and only touch the UI through the queue-based Overlay.
    """

    def __init__(self, config: ClientConfig, *, config_path: Path) -> None:
        self._config = config
        self._config_path = config_path
        bindings = {name: Hotkey.parse(spec) for name, spec in config.hotkeys.bindings.items()}
        cancel = Hotkey.parse(config.hotkeys.cancel) if config.hotkeys.cancel else None
        self._machine = HotkeyStateMachine(bindings, cancel)
        self._recorder = Recorder(config.audio)
        self._client = VoiceCodeClient(config.server)
        self._overlay = Overlay(config.overlay)
        self._listener = HotkeyListener(self._machine, self._on_event)
        self._stop_event = threading.Event()
        self._tray: Any = None
        self._recording_mode: str | None = None
        self._recording_started = 0.0
        self._target_hwnd: int | None = None
        self._job = 0
        self._pending_job = 0

    def run(self) -> int:
        """Start every thread and block until the tray Quit item or Ctrl+C."""
        self._overlay.start()
        self._listener.start()
        self._start_tray()
        threading.Thread(target=self._probe_backend, name="voice-code-health", daemon=True).start()
        logger.info("listening for %s", self._bindings_summary())
        self._overlay.show(f"voice-code ready - {self._bindings_summary()}", style="info")
        self._overlay.hide()
        try:
            if self._overlay.enabled:
                self._overlay.run_forever()
            else:
                self._stop_event.wait()
        except KeyboardInterrupt:
            logger.info("interrupted")
        finally:
            self._shutdown()
        return 0

    def _on_event(self, event: object) -> None:
        try:
            if isinstance(event, StartRecording):
                self._start_recording(event.mode)
            elif isinstance(event, StopRecording):
                self._stop_recording(event.mode)
            elif isinstance(event, CancelRecording):
                self._cancel(event.mode)
        except Exception:
            logger.exception("hotkey event handling failed")
            self._machine.reset()
            self._recording_mode = None
            self._fail("Internal error")

    def _start_recording(self, mode: str) -> None:
        self._target_hwnd = foreground_window()
        try:
            self._recorder.start()
        except RecorderError as exc:
            logger.warning("microphone unavailable: %s", exc)
            self._machine.reset()
            self._fail("Microphone unavailable")
            return
        self._recording_mode = mode
        self._recording_started = time.monotonic()
        self._overlay.show(_recording_text(mode, 0.0), style="recording")
        self._overlay.after(_TICK_MS, self._tick)

    def _stop_recording(self, mode: str) -> None:
        self._recording_mode = None
        self._machine.processing_started()
        try:
            wav, seconds = self._recorder.stop()
        except RecorderError as exc:
            logger.warning("recording failed: %s", exc)
            self._fail("Microphone error")
            self._machine.processing_finished()
            return
        if not wav or seconds <= 0.0:
            logger.info("nothing captured for mode %s", mode)
            self._fail("No audio captured")
            self._machine.processing_finished()
            return
        self._job += 1
        self._overlay.show("Transcribing...", style="info")
        threading.Thread(
            target=self._worker,
            args=(wav, mode, seconds, self._job, self._target_hwnd),
            name="voice-code-request",
            daemon=True,
        ).start()

    def _cancel(self, mode: str) -> None:
        self._recording_mode = None
        self._recorder.cancel()
        logger.info("cancelled recording for mode %s", mode)
        self._overlay.show("Cancelled", style="info")
        self._overlay.hide()

    def _worker(
        self, wav: bytes, mode: str, seconds: float, job: int, target_hwnd: int | None
    ) -> None:
        try:
            result = self._request(wav, mode, seconds, job)
            if result is None:
                return
            logger.info(
                "request %s mode=%s audio=%.1fs server_ms=%s chars=%d",
                result.request_id,
                result.mode,
                seconds,
                result.server_ms,
                len(result.output),
            )
            if self._config.logging.log_text:
                logger.debug("output for %s: %s", result.request_id, result.output)
            self._deliver(result.output, target_hwnd)
        except ClipboardError as exc:
            logger.warning("delivery failed: %s", exc)
            self._fail("Clipboard busy")
        except Exception:
            logger.exception("request failed")
            self._fail("Client error")
        finally:
            self._machine.processing_finished()

    def _request(self, wav: bytes, mode: str, seconds: float, job: int) -> ProcessResult | None:
        self._pending_job = job
        hint = threading.Timer(_FORMATTING_HINT_S, self._hint_formatting, args=(job,))
        hint.daemon = True
        hint.start()
        try:
            for attempt in (1, 2):
                try:
                    return self._client.process(wav, mode, audio_seconds=seconds)
                except ApiUnavailableError:
                    logger.warning("backend unreachable (attempt %d)", attempt)
                    if attempt == 2:
                        logger.warning("discarding %.1fs of audio for mode %s", seconds, mode)
                        self._fail("Service unavailable")
                        return None
                    self._overlay.show("Service unavailable", style="error")
                    time.sleep(_RETRY_DELAY_S)
                except ApiTimeoutError:
                    logger.warning("backend timed out after %.1fs of audio", seconds)
                    self._fail("Backend timeout")
                    return None
                except ApiError as exc:
                    logger.warning("backend error code=%s status=%s", exc.code, exc.status)
                    self._fail(exc.message)
                    return None
            return None
        finally:
            self._pending_job = 0
            hint.cancel()

    def _deliver(self, output: str, target_hwnd: int | None) -> None:
        paste = self._config.paste
        if not paste.enabled:
            set_text(output)
            self._overlay.show("Copied", style="ok")
            self._overlay.hide()
            return

        current_hwnd = foreground_window()
        if (
            paste.only_if_target_window_unchanged
            and target_hwnd is not None
            and current_hwnd != target_hwnd
        ):
            set_text(output)
            logger.info(
                "focus moved from %s to %s, result copied instead of pasted",
                target_hwnd,
                current_hwnd,
            )
            self._overlay.show("Copied - focus changed", style="ok")
            self._overlay.hide()
            return

        self._await_modifier_release()
        with preserved_clipboard(paste.preserve_clipboard, paste.restore_delay_ms):
            set_text(output)
            send_paste(paste.shortcut)
            if paste.auto_submit:
                send_enter()
            self._overlay.show("Ready", style="ok")
            self._overlay.hide()

    def _fail(self, text: str) -> None:
        self._overlay.show(text, style="error")
        self._overlay.hide()

    def _tick(self) -> None:
        mode = self._recording_mode
        if mode is None:
            return
        elapsed = time.monotonic() - self._recording_started
        self._overlay.show(_recording_text(mode, elapsed), style="recording")
        self._overlay.after(_TICK_MS, self._tick)

    def _hint_formatting(self, job: int) -> None:
        if self._pending_job == job:
            self._overlay.show("Formatting...", style="info")

    def _await_modifier_release(self) -> None:
        # Pasting while the user still holds ctrl+alt would send ctrl+alt+v to the target
        # window instead of ctrl+v.
        deadline = time.monotonic() + _MODIFIER_RELEASE_TIMEOUT_S
        while self._machine.held and time.monotonic() < deadline:
            time.sleep(0.02)

    def _probe_backend(self) -> None:
        try:
            health = self._client.health()
        except ApiError as exc:
            logger.warning("backend health probe failed: %s", exc.message)
            self._overlay.show("Backend unavailable", style="error")
            self._overlay.hide()
            return
        status = health.get("status")
        logger.info("backend status=%s", status)
        if status != "ready":
            self._overlay.show("Backend warming up", style="info")
            self._overlay.hide()

    def _bindings_summary(self) -> str:
        return ", ".join(
            f"{spec} {name}" for name, spec in sorted(self._config.hotkeys.bindings.items())
        )

    def _start_tray(self) -> None:
        try:
            import pystray
            from PIL import Image, ImageDraw

            image = Image.new("RGBA", (64, 64), (0, 0, 0, 0))
            draw = ImageDraw.Draw(image)
            draw.ellipse((6, 6, 58, 58), fill=(62, 116, 214, 255))
            draw.rounded_rectangle((26, 16, 38, 38), radius=6, fill=(255, 255, 255, 255))
            draw.rectangle((21, 42, 43, 46), fill=(255, 255, 255, 255))
            icon = pystray.Icon(
                "voice-code",
                image,
                "voice-code",
                menu=pystray.Menu(
                    pystray.MenuItem("Status", self._tray_status),
                    pystray.MenuItem("Open config folder", self._tray_open_config),
                    pystray.MenuItem("Quit", self._tray_quit),
                ),
            )
        except Exception:
            logger.warning("tray icon unavailable", exc_info=True)
            return
        self._tray = icon
        threading.Thread(target=self._run_tray, name="voice-code-tray", daemon=True).start()

    def _run_tray(self) -> None:
        try:
            self._tray.run()
        except Exception:
            logger.warning("tray icon stopped", exc_info=True)

    def _tray_status(self, _icon: object, _item: object) -> None:
        try:
            health = self._client.health()
            text = f"Backend: {health.get('status', 'unknown')}"
        except ApiError as exc:
            text = f"Backend: {exc.message}"
        self._overlay.show(text, style="info")
        self._overlay.hide()

    def _tray_open_config(self, _icon: object, _item: object) -> None:
        folder = self._config_path.resolve().parent
        if not folder.is_dir():
            folder = Path.cwd()
        try:
            os.startfile(folder)
        except OSError:
            logger.warning("could not open %s", folder, exc_info=True)

    def _tray_quit(self, _icon: object, _item: object) -> None:
        logger.info("quit requested from the tray")
        self._stop_event.set()
        self._overlay.stop()

    def _shutdown(self) -> None:
        self._stop_event.set()
        self._listener.stop()
        try:
            self._recorder.cancel()
        except Exception:
            logger.debug("recorder cleanup failed", exc_info=True)
        if self._tray is not None:
            try:
                self._tray.stop()
            except Exception:
                logger.debug("tray shutdown failed", exc_info=True)
        self._overlay.stop()
        self._client.close()


def _recording_text(mode: str, elapsed_s: float) -> str:
    total = max(0, int(elapsed_s))
    return f"{mode.upper()} - Recording {total // 60:02d}:{total % 60:02d}"


def log_directory() -> Path:
    """Directory the companion writes its log file to (created on demand)."""
    base = os.environ.get("LOCALAPPDATA") or tempfile.gettempdir()
    return Path(base) / "voice-code"


def _configure_logging(config: LoggingConfig) -> None:
    level = logging.getLevelNamesMapping().get(config.level.upper(), logging.INFO)
    fmt = logging.Formatter("%(asctime)s %(levelname)-7s %(name)s %(message)s")
    handlers: list[logging.Handler] = []

    # pythonw.exe (how start.ps1 launches us, so there is no console window) leaves
    # sys.stderr as None, so a stream handler alone would drop every line.
    if sys.stderr is not None:
        stream = logging.StreamHandler(sys.stderr)
        stream.setFormatter(fmt)
        handlers.append(stream)

    try:
        directory = log_directory()
        directory.mkdir(parents=True, exist_ok=True)
        # utf-8 explicitly: the console default on Windows is cp1252 and transcripts are Russian.
        rotating = RotatingFileHandler(
            directory / "client.log", maxBytes=1_000_000, backupCount=2, encoding="utf-8"
        )
        rotating.setFormatter(fmt)
        handlers.append(rotating)
    except OSError as exc:
        if sys.stderr is not None:
            print(f"could not open the log file: {exc}", file=sys.stderr)

    logging.basicConfig(level=level, handlers=handlers, force=True)
    # One INFO line per HTTP call would bury the companion's own status lines.
    logging.getLogger("httpx").setLevel(max(level, logging.WARNING))
    logging.getLogger("httpcore").setLevel(max(level, logging.WARNING))


def _print_devices() -> int:
    try:
        devices = list_input_devices()
    except Exception as exc:
        print(f"could not list input devices: {exc}", file=sys.stderr)
        return 1
    for index, name, rate in devices:
        print(f"{index:>3}  {name}  ({rate} Hz)")
    return 0


def _parse_args(argv: list[str] | None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        prog="voice-code-client",
        description="Windows push-to-talk companion for the voice-code backend.",
    )
    parser.add_argument("--config", type=Path, default=None, help="path to client.yaml")
    parser.add_argument(
        "--list-devices", action="store_true", help="print the audio input devices and exit"
    )
    parser.add_argument(
        "--version", action="version", version=f"voice-code-client {CLIENT_VERSION}"
    )
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    """Entry point for the voice-code-client console script. Returns the process exit code."""
    args = _parse_args(argv)
    if args.list_devices:
        return _print_devices()
    try:
        config = load_config(args.config)
    except ConfigError as exc:
        print(f"config error: {exc}", file=sys.stderr)
        return 2
    _configure_logging(config.logging)
    try:
        app = VoiceCodeApp(config, config_path=args.config or default_config_path())
    except ValueError as exc:
        print(f"hotkey error: {exc}", file=sys.stderr)
        return 2
    return app.run()


if __name__ == "__main__":
    raise SystemExit(main())
