from __future__ import annotations

import io
import logging
import threading
import wave
from typing import TYPE_CHECKING, Any

import numpy as np
from numpy.typing import NDArray

if TYPE_CHECKING:
    from .config import AudioConfig

logger = logging.getLogger(__name__)


class RecorderError(RuntimeError):
    """Raised when the microphone cannot be opened, used or closed."""


def _sounddevice() -> Any:
    try:
        import sounddevice
    except Exception as exc:
        raise RecorderError(f"audio backend unavailable: {exc}") from exc
    return sounddevice


def list_input_devices() -> list[tuple[int, str, int]]:
    """Return (index, name, default_samplerate_hz) for every input-capable device.

    Raises RecorderError when PortAudio is unavailable or the devices cannot be queried.
    """
    sounddevice = _sounddevice()
    try:
        devices = list(enumerate(sounddevice.query_devices()))
    except Exception as exc:
        raise RecorderError(f"cannot enumerate audio devices: {exc}") from exc
    found: list[tuple[int, str, int]] = []
    for index, info in devices:
        if int(info.get("max_input_channels", 0)) <= 0:
            continue
        name = str(info.get("name", "")).strip()
        rate = int(float(info.get("default_samplerate", 0.0)))
        found.append((index, name, rate))
    return found


def resolve_device(spec: int | str | None) -> int | None:
    """Resolve a configured device spec to a PortAudio device index.

    None (or an empty string) means the system default and returns None. An int is
    passed through unchecked. A string selects the first input device whose name
    contains it case-insensitively; RecorderError is raised when nothing matches.
    """
    if spec is None:
        return None
    if isinstance(spec, int):
        return spec
    needle = spec.strip().lower()
    if not needle:
        return None
    for index, name, _rate in list_input_devices():
        if needle in name.lower():
            return index
    raise RecorderError(f"no input device matching {spec!r}")


def encode_wav(samples: NDArray[Any], sample_rate: int) -> bytes:
    """Encode mono float samples in [-1, 1] as a 16-bit PCM RIFF/WAVE blob.

    The array is flattened, clipped to [-1, 1] and scaled by 32767. sample_rate is in
    hertz. An empty array yields a valid header with zero frames.
    """
    mono = np.asarray(samples, dtype=np.float32).reshape(-1)
    # RIFF/WAVE PCM is little-endian regardless of host byte order.
    pcm = (np.clip(mono, -1.0, 1.0) * 32767.0).astype("<i2")
    buffer = io.BytesIO()
    with wave.open(buffer, "wb") as wav:
        wav.setnchannels(1)
        wav.setsampwidth(2)
        wav.setframerate(max(1, int(sample_rate)))
        wav.writeframes(pcm.tobytes())
    return buffer.getvalue()


class Recorder:
    """Captures microphone audio into memory and returns it as a WAV blob.

    Audio stays at the device's native sample rate (the backend resamples) and is never
    written to disk. Capture is capped at ``config.max_seconds``: past the cap the stream
    keeps running but nothing more is buffered, and stop() still returns what was
    captured. Exactly one recording may be active at a time.
    """

    def __init__(self, config: AudioConfig) -> None:
        self._config = config
        self._lock = threading.Lock()
        self._blocks: list[NDArray[np.float32]] = []
        self._frames = 0
        self._max_frames = 0
        self._capped = False
        self._error: BaseException | None = None
        self._stream: Any = None
        self._sample_rate = int(config.sample_rate or 0)

    @property
    def recording(self) -> bool:
        """True between a successful start() and the matching stop() or cancel()."""
        return self._stream is not None

    @property
    def sample_rate(self) -> int:
        """Hertz of the active or most recent stream; 0 before the first start()."""
        return self._sample_rate

    def start(self) -> None:
        """Open the input stream and start buffering audio.

        Raises RecorderError when a recording is already active ("recording already
        active"), when the configured device cannot be found, or when the device is busy
        or refuses the format.
        """
        if self._stream is not None:
            raise RecorderError("recording already active")

        sounddevice = _sounddevice()
        device = resolve_device(self._config.input_device)
        rate = int(self._config.sample_rate or self._device_samplerate(device))
        if rate <= 0:
            raise RecorderError("could not determine an input sample rate")
        channels = max(1, int(self._config.channels))

        with self._lock:
            self._blocks = []
            self._frames = 0
            self._capped = False
            self._error = None
        self._sample_rate = rate
        self._max_frames = max(1, int(self._config.max_seconds * rate))

        try:
            stream = sounddevice.InputStream(
                samplerate=rate,
                device=device,
                channels=channels,
                dtype="float32",
                callback=self._callback,
            )
            stream.start()
        except Exception as exc:
            raise RecorderError(f"cannot open the audio input device: {exc}") from exc
        self._stream = stream

    def stop(self) -> tuple[bytes, float]:
        """Stop capturing and return (wav_bytes, duration_seconds).

        The WAV is 16-bit mono PCM at the stream's native rate; multi-channel input is
        downmixed by averaging. Returns (b"", 0.0) when nothing was captured. Raises
        RecorderError when no recording is active or when the stream failed while it was
        running.
        """
        stream = self._stream
        if stream is None:
            raise RecorderError("not recording")
        self._stream = None

        close_error: BaseException | None = None
        try:
            stream.stop()
            stream.close()
        except Exception as exc:
            close_error = exc

        with self._lock:
            blocks = self._blocks
            self._blocks = []
            self._frames = 0
            self._capped = False
            error = self._error
            self._error = None

        if error is not None:
            raise RecorderError(f"audio capture failed: {error}") from error
        if close_error is not None:
            raise RecorderError(f"audio device error: {close_error}") from close_error
        if not blocks:
            return b"", 0.0

        samples = np.concatenate(blocks, axis=0)
        if samples.ndim > 1 and samples.shape[1] > 1:
            mono = samples.mean(axis=1)
        else:
            mono = samples.reshape(-1)
        if mono.size == 0:
            return b"", 0.0
        return encode_wav(mono, self._sample_rate), mono.size / float(self._sample_rate)

    def cancel(self) -> None:
        """Stop capturing and discard the buffer. Does nothing when not recording."""
        stream = self._stream
        self._stream = None
        if stream is not None:
            try:
                stream.stop()
                stream.close()
            except Exception:
                logger.exception("failed to close the audio input stream")
        with self._lock:
            self._blocks = []
            self._frames = 0
            self._capped = False
            self._error = None

    def _device_samplerate(self, device: int | None) -> int:
        sounddevice = _sounddevice()
        try:
            info = sounddevice.query_devices(device, "input")
            return int(float(info["default_samplerate"]))
        except Exception as exc:
            raise RecorderError(f"cannot query the audio input device: {exc}") from exc

    def _callback(self, indata: NDArray[Any], _frames: int, _time: Any, status: Any) -> None:
        if status:
            logger.debug("audio input status: %s", status)
        try:
            with self._lock:
                if self._capped:
                    return
                room = self._max_frames - self._frames
                if room <= 0:
                    self._capped = True
                    return
                block = indata[:room].copy()
                self._blocks.append(block)
                self._frames += int(block.shape[0])
                if self._frames >= self._max_frames:
                    self._capped = True
        except Exception as exc:
            self._error = exc
