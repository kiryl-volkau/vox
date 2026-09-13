from __future__ import annotations

import io
import logging
import struct
import threading
import time
from typing import TYPE_CHECKING, NoReturn

import numpy as np
from numpy.typing import NDArray

from .config import Settings
from .models import TranscriptionResult

if TYPE_CHECKING:
    from faster_whisper import WhisperModel

logger = logging.getLogger(__name__)

type FloatArray = NDArray[np.float32]

DEFAULT_SAMPLE_RATE = 16000
MAX_DECODED_SECONDS = 600.0
# Bounds on the rate a WAV header may declare. The value drives the resample ratio, so an
# absurd one turns a few hundred bytes into gigabytes of float32.
MIN_SAMPLE_RATE = 4000
MAX_SAMPLE_RATE = 768000

_WAVE_FORMAT_PCM = 0x0001
_WAVE_FORMAT_IEEE_FLOAT = 0x0003
_WAVE_FORMAT_EXTENSIBLE = 0xFFFE

_CPU_UNSUPPORTED_COMPUTE_TYPES = frozenset({"float16", "bfloat16", "int8_float16", "int8_bfloat16"})


class TranscriptionError(RuntimeError):
    """Audio could not be decoded, or the speech-to-text model could not run."""


def _cuda_device_count() -> int:
    try:
        import ctranslate2

        return int(ctranslate2.get_cuda_device_count())
    except Exception as exc:
        logger.warning("CUDA device count unavailable (%s: %s)", type(exc).__name__, exc)
        return 0


def resolve_device(requested: str, compute_type: str) -> tuple[str, str]:
    """Return the (device, compute_type) pair that is actually usable.

    ``requested`` is "auto", "cuda" or "cpu"; "auto" becomes "cuda" when CTranslate2 reports at
    least one CUDA device and "cpu" otherwise. CPU inference cannot run the half-precision
    compute types, so those are downgraded to "int8" with a warning. Never raises: a missing or
    broken CTranslate2 install resolves to CPU.
    """
    device = requested
    if device == "auto":
        device = "cuda" if _cuda_device_count() > 0 else "cpu"
    if device == "cpu" and compute_type in _CPU_UNSUPPORTED_COMPUTE_TYPES:
        logger.warning("compute type %s is not supported on CPU, using int8", compute_type)
        return "cpu", "int8"
    return device, compute_type


def _unpack_s24(raw: bytes) -> FloatArray:
    triplets = np.frombuffer(raw, dtype=np.uint8).reshape(-1, 3).astype(np.int32)
    values = triplets[:, 0] | (triplets[:, 1] << 8) | (triplets[:, 2] << 16)
    signed = np.where(values >= 0x800000, values - 0x1000000, values)
    return signed.astype(np.float32)


def _samples_to_float(raw: bytes, audio_format: int, bits: int, channels: int) -> FloatArray:
    if bits <= 0 or bits % 8:
        raise TranscriptionError(f"unsupported WAVE bit depth: {bits}")
    frame_bytes = (bits // 8) * channels
    usable = raw[: len(raw) - (len(raw) % frame_bytes)]

    if audio_format == _WAVE_FORMAT_IEEE_FLOAT:
        if bits != 32:
            raise TranscriptionError(f"unsupported IEEE float WAVE bit depth: {bits}")
        flat = np.frombuffer(usable, dtype="<f4").astype(np.float32)
    elif audio_format == _WAVE_FORMAT_PCM:
        if bits == 8:
            flat = (np.frombuffer(usable, dtype=np.uint8).astype(np.float32) - 128.0) / 128.0
        elif bits == 16:
            flat = np.frombuffer(usable, dtype="<i2").astype(np.float32) / 32768.0
        elif bits == 24:
            flat = _unpack_s24(usable) / 8388608.0
        elif bits == 32:
            flat = np.frombuffer(usable, dtype="<i4").astype(np.float32) / 2147483648.0
        else:
            raise TranscriptionError(f"unsupported PCM WAVE bit depth: {bits}")
    else:
        raise TranscriptionError(f"unsupported WAVE format tag: 0x{audio_format:04X}")

    if channels > 1:
        flat = np.asarray(
            flat.reshape(-1, channels).mean(axis=1, dtype=np.float32), dtype=np.float32
        )
    finite = np.nan_to_num(flat, nan=0.0, posinf=1.0, neginf=-1.0)
    return np.clip(finite, -1.0, 1.0).astype(np.float32)


def decode_wav(data: bytes) -> tuple[FloatArray, int]:
    """Decode a RIFF/WAVE byte string into (float32 mono samples in [-1, 1], sample rate).

    Pure numpy, no ffmpeg. Supports PCM 8-bit unsigned, 16/24/32-bit signed and 32-bit IEEE
    float, plus WAVE_FORMAT_EXTENSIBLE whose real format tag lives in the SubFormat GUID.
    Chunks are walked in order, so fmt and data need not be adjacent and unknown chunks are
    skipped. Multi-channel audio is downmixed by averaging the channels. A truncated data
    chunk yields the frames that are present. Raises TranscriptionError when the header is not
    RIFF/WAVE, when the fmt or data chunk is missing, or when the sample format is unsupported.
    """
    if len(data) < 12 or data[0:4] != b"RIFF" or data[8:12] != b"WAVE":
        raise TranscriptionError("payload is not a RIFF/WAVE stream")

    fmt_body: bytes | None = None
    data_body: bytes | None = None
    pos = 12
    while pos + 8 <= len(data):
        chunk_id = data[pos : pos + 4]
        declared = int.from_bytes(data[pos + 4 : pos + 8], "little")
        body = data[pos + 8 : pos + 8 + declared]
        if chunk_id == b"fmt " and fmt_body is None:
            fmt_body = body
        elif chunk_id == b"data" and data_body is None:
            data_body = body
        # A RIFF chunk body is padded to an even length and the pad byte is not in the size.
        pos += 8 + declared + (declared & 1)

    if fmt_body is None:
        raise TranscriptionError("WAVE stream has no fmt chunk")
    if data_body is None:
        raise TranscriptionError("WAVE stream has no data chunk")
    if len(fmt_body) < 16:
        raise TranscriptionError("WAVE fmt chunk is truncated")

    audio_format, channels, sample_rate, _byte_rate, _block_align, bits = struct.unpack(
        "<HHIIHH", fmt_body[:16]
    )
    if audio_format == _WAVE_FORMAT_EXTENSIBLE:
        if len(fmt_body) < 40:
            raise TranscriptionError("WAVE_FORMAT_EXTENSIBLE fmt chunk is truncated")
        audio_format = int.from_bytes(fmt_body[24:26], "little")
    if channels < 1:
        raise TranscriptionError("WAVE stream declares zero channels")
    if not MIN_SAMPLE_RATE <= sample_rate <= MAX_SAMPLE_RATE:
        raise TranscriptionError(f"WAVE stream declares an unusable sample rate: {sample_rate}")

    return _samples_to_float(data_body, audio_format, bits, channels), sample_rate


def resample_linear(audio: FloatArray, src_rate: int, dst_rate: int) -> FloatArray:
    """Resample mono float32 ``audio`` by linear interpolation.

    Returns the input unchanged when the rates match or the input is empty. Raises
    TranscriptionError when either rate is below 1 Hz.
    """
    if src_rate < 1 or dst_rate < 1:
        raise TranscriptionError(f"invalid resample rates: {src_rate} -> {dst_rate}")
    if src_rate == dst_rate or audio.size == 0:
        return audio
    target_len = round(audio.shape[0] * dst_rate / src_rate)
    if target_len < 1:
        return np.zeros(0, dtype=np.float32)
    wanted = np.arange(target_len, dtype=np.float64) * (src_rate / dst_rate)
    known = np.arange(audio.shape[0], dtype=np.float64)
    return np.interp(wanted, known, audio).astype(np.float32)


def decode_audio(
    data: bytes,
    target_rate: int = DEFAULT_SAMPLE_RATE,
    max_seconds: float = MAX_DECODED_SECONDS,
) -> tuple[FloatArray, float]:
    """Decode any supported audio payload to (float32 mono at ``target_rate``, seconds).

    RIFF/WAVE payloads are decoded by :func:`decode_wav` and resampled in-process; anything
    else is handed to the PyAV-backed decoder shipped with faster-whisper. Raises
    TranscriptionError for an empty or undecodable payload, or for one whose decoded
    length exceeds ``max_seconds``.
    """
    if not data:
        raise TranscriptionError("audio payload is empty")

    if len(data) >= 12 and data[0:4] == b"RIFF" and data[8:12] == b"WAVE":
        samples, sample_rate = decode_wav(data)
        if samples.shape[0] / float(sample_rate) > max_seconds:
            raise TranscriptionError(f"audio is longer than the {max_seconds:.0f}s limit")
        audio = resample_linear(samples, sample_rate, target_rate)
    else:
        try:
            from faster_whisper.audio import decode_audio as decode_with_pyav

            audio = np.asarray(
                decode_with_pyav(io.BytesIO(data), sampling_rate=target_rate), dtype=np.float32
            )
        except Exception as exc:
            raise TranscriptionError(
                f"could not decode audio payload ({type(exc).__name__}: {exc})"
            ) from exc

    seconds = audio.shape[0] / float(target_rate)
    if seconds > max_seconds:
        raise TranscriptionError(f"audio is longer than the {max_seconds:.0f}s limit")
    return audio, seconds


class Transcriber:
    """Blocking faster-whisper wrapper owning one loaded model.

    Construct it, then call :meth:`load` and optionally :meth:`warmup` before
    :meth:`transcribe`. Model access is serialised by an internal lock because a CTranslate2
    model cannot run concurrent transcriptions, so ``transcribe`` may be driven from several
    ``asyncio.to_thread`` workers.
    """

    def __init__(self, settings: Settings, hotwords: str | None = None) -> None:
        self._settings = settings
        self._hotwords = hotwords or None
        self._model: WhisperModel | None = None
        self._device: str = settings.stt_device
        self._compute_type: str = settings.stt_compute_type
        self._error: str | None = None
        self._lock = threading.Lock()

    @property
    def ready(self) -> bool:
        return self._model is not None

    @property
    def device(self) -> str:
        return self._device

    @property
    def compute_type(self) -> str:
        return self._compute_type

    @property
    def model_name(self) -> str:
        return self._settings.stt_model

    @property
    def error(self) -> str | None:
        return self._error

    def load(self) -> None:
        """Load the model, blocking until it is resident.

        Resolves the device first. A CUDA load failure is logged and retried once on
        ("cpu", "int8"); only when that also fails is ``error`` set and TranscriptionError
        raised.
        """
        device, compute_type = resolve_device(
            self._settings.stt_device, self._settings.stt_compute_type
        )
        try:
            model = self._build(device, compute_type)
        except Exception as exc:
            if device != "cuda":
                self._fail(exc)
            logger.warning(
                "CUDA STT load failed (%s: %s), falling back to CPU", type(exc).__name__, exc
            )
            device, compute_type = "cpu", "int8"
            try:
                model = self._build(device, compute_type)
            except Exception as cpu_exc:
                self._fail(cpu_exc)

        self._model = model
        self._device = device
        self._compute_type = compute_type
        self._error = None
        logger.info("STT model %s loaded on %s (%s)", self.model_name, device, compute_type)

    def warmup(self) -> None:
        """Transcribe one second of synthetic audio so the first real request is not cold.

        Never raises and does nothing when the model is not loaded: a failed warmup is logged
        and the model still serves.
        """
        model = self._model
        if model is None:
            return
        moments = np.arange(DEFAULT_SAMPLE_RATE, dtype=np.float32) / DEFAULT_SAMPLE_RATE
        tone = (0.01 * np.sin(2.0 * np.pi * 220.0 * moments)).astype(np.float32)
        language = self._settings.stt_language or None
        started = time.perf_counter()
        try:
            with self._lock:
                # Two passes on purpose. A tone is filtered out as non-speech, so only the
                # vad_filter=False pass reaches the encoder and decoder - and that is what
                # triggers CTranslate2's one-off CUDA kernel build. The second pass loads the
                # Silero VAD graph that real requests use.
                for vad in (False, True):
                    segments, _info = model.transcribe(
                        tone,
                        language=language,
                        beam_size=self._settings.stt_beam_size,
                        vad_filter=vad,
                        condition_on_previous_text=False,
                    )
                    for _segment in segments:
                        pass
        except Exception as exc:
            logger.warning("STT warmup failed (%s: %s)", type(exc).__name__, exc)
            return
        logger.info("STT warmup finished in %d ms", int((time.perf_counter() - started) * 1000))

    def transcribe(self, data: bytes, language: str | None = None) -> TranscriptionResult:
        """Transcribe an audio payload. Blocking; intended for a worker thread.

        ``language`` overrides the configured language; when neither is set the language is
        auto-detected and reported from the model. ``duration_ms`` covers decoding plus
        inference, ``audio_duration_s`` is the length of the decoded audio. Raises
        TranscriptionError when the payload cannot be decoded, the model is not loaded, or
        inference fails.
        """
        started = time.perf_counter()
        audio, audio_seconds = decode_audio(data, max_seconds=self._settings.max_audio_seconds)
        forced = language or self._settings.stt_language or None
        text, detected, probability = self._run(audio, forced)
        return TranscriptionResult(
            text=text,
            language=forced or detected,
            language_probability=probability,
            audio_duration_s=round(audio_seconds, 3),
            duration_ms=int((time.perf_counter() - started) * 1000),
        )

    def close(self) -> None:
        """Release the model. Idempotent."""
        with self._lock:
            self._model = None

    def _build(self, device: str, compute_type: str) -> WhisperModel:
        from faster_whisper import WhisperModel

        return WhisperModel(self._settings.stt_model, device=device, compute_type=compute_type)

    def _fail(self, cause: BaseException) -> NoReturn:
        self._error = f"{type(cause).__name__}: {cause}"
        raise TranscriptionError(
            f"could not load STT model {self.model_name}: {self._error}"
        ) from cause

    def _run(self, audio: FloatArray, language: str | None) -> tuple[str, str, float]:
        model = self._model
        if model is None:
            raise TranscriptionError("STT model is not loaded")
        settings = self._settings
        prompt = self._hotwords if settings.stt_glossary_hotwords else None
        try:
            with self._lock:
                segments, info = model.transcribe(
                    audio,
                    language=language,
                    beam_size=settings.stt_beam_size,
                    vad_filter=settings.stt_vad_filter,
                    condition_on_previous_text=False,
                    initial_prompt=prompt,
                )
                # faster-whisper decodes lazily; the generator must be drained under the lock.
                text = "".join(str(segment.text) for segment in segments).strip()
        except Exception as exc:
            logger.error("STT inference failed", exc_info=exc)
            raise TranscriptionError(f"transcription failed: {type(exc).__name__}") from exc
        detected = str(getattr(info, "language", "") or "")
        probability = float(getattr(info, "language_probability", 0.0) or 0.0)
        return text, detected, probability
