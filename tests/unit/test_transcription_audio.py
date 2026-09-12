from __future__ import annotations

import io
import struct
import wave
from collections.abc import Callable

import numpy as np
import pytest

from voice_code_server.transcription import (
    TranscriptionError,
    decode_audio,
    decode_wav,
    resample_linear,
    resolve_device,
)

WAVE_FORMAT_PCM = 0x0001
WAVE_FORMAT_IEEE_FLOAT = 0x0003
WAVE_FORMAT_EXTENSIBLE = 0xFFFE
KSDATAFORMAT_SUBTYPE_TAIL = b"\x00\x00\x00\x00\x10\x00\x80\x00\x00\xaa\x00\x38\x9b\x71"


def _stdlib_wav(frames: bytes, *, channels: int, width: int, rate: int) -> bytes:
    buffer = io.BytesIO()
    with wave.open(buffer, "wb") as wav:
        wav.setnchannels(channels)
        wav.setsampwidth(width)
        wav.setframerate(rate)
        wav.writeframes(frames)
    return buffer.getvalue()


def _riff(fmt_body: bytes, data_body: bytes, *, declared_data_size: int | None = None) -> bytes:
    size = len(data_body) if declared_data_size is None else declared_data_size
    body = b"".join(
        [
            b"WAVE",
            b"fmt ",
            len(fmt_body).to_bytes(4, "little"),
            fmt_body,
            b"data",
            size.to_bytes(4, "little"),
            data_body,
        ]
    )
    return b"RIFF" + len(body).to_bytes(4, "little") + body


def _fmt(tag: int, channels: int, rate: int, bits: int) -> bytes:
    block_align = channels * bits // 8
    return struct.pack("<HHIIHH", tag, channels, rate, rate * block_align, block_align, bits)


def _extensible_fmt(channels: int, rate: int, bits: int, subformat: int) -> bytes:
    base = _fmt(WAVE_FORMAT_EXTENSIBLE, channels, rate, bits)
    extension = struct.pack("<HHI", 22, bits, 0x3)
    guid = struct.pack("<H", subformat) + KSDATAFORMAT_SUBTYPE_TAIL
    return base + extension + guid


def test_decode_wav_reads_16_bit_mono() -> None:
    values = [0, 16384, -16384, 32767, -32768]
    payload = _stdlib_wav(struct.pack(f"<{len(values)}h", *values), channels=1, width=2, rate=16000)

    samples, rate = decode_wav(payload)

    assert rate == 16000
    assert samples.dtype == np.float32
    assert samples.shape == (5,)
    assert samples.tolist() == pytest.approx([0.0, 0.5, -0.5, 0.99997, -1.0], abs=1e-4)


def test_decode_wav_downmixes_stereo_to_mono() -> None:
    interleaved = [10000, 20000, -8000, 0, 32767, -32768]
    payload = _stdlib_wav(
        struct.pack(f"<{len(interleaved)}h", *interleaved), channels=2, width=2, rate=44100
    )

    samples, rate = decode_wav(payload)

    assert rate == 44100
    assert samples.shape == (3,)
    expected = [
        (10000 + 20000) / 2 / 32768.0,
        (-8000 + 0) / 2 / 32768.0,
        (32767 - 32768) / 2 / 32768.0,
    ]
    assert samples.tolist() == pytest.approx(expected, abs=1e-5)


def test_decode_wav_reads_8_bit_unsigned() -> None:
    payload = _stdlib_wav(bytes([128, 255, 0, 192]), channels=1, width=1, rate=8000)

    samples, rate = decode_wav(payload)

    assert rate == 8000
    assert samples.tolist() == pytest.approx([0.0, 127 / 128.0, -1.0, 0.5], abs=1e-5)


def test_decode_wav_reads_32_bit_float() -> None:
    values = [0.0, 0.25, -0.75, 1.0]
    payload = _riff(
        _fmt(WAVE_FORMAT_IEEE_FLOAT, 1, 16000, 32), struct.pack(f"<{len(values)}f", *values)
    )

    samples, rate = decode_wav(payload)

    assert rate == 16000
    assert samples.tolist() == pytest.approx(values, abs=1e-6)


def test_decode_wav_clamps_out_of_range_and_non_finite_floats() -> None:
    payload = _riff(
        _fmt(WAVE_FORMAT_IEEE_FLOAT, 1, 16000, 32),
        struct.pack("<5f", 2.0, -2.0, float("nan"), float("inf"), float("-inf")),
    )

    samples, _rate = decode_wav(payload)

    assert samples.tolist() == pytest.approx([1.0, -1.0, 0.0, 1.0, -1.0])


def test_decode_wav_reads_32_bit_signed_pcm() -> None:
    values = [0, 2**30, -(2**30)]
    payload = _riff(_fmt(WAVE_FORMAT_PCM, 1, 16000, 32), struct.pack(f"<{len(values)}i", *values))

    samples, _rate = decode_wav(payload)

    assert samples.tolist() == pytest.approx([0.0, 0.5, -0.5], abs=1e-6)


def test_decode_wav_reads_24_bit_signed_pcm() -> None:
    frames = b"".join(value.to_bytes(3, "little", signed=True) for value in (0, 4194304, -4194304))
    payload = _riff(_fmt(WAVE_FORMAT_PCM, 1, 48000, 24), frames)

    samples, rate = decode_wav(payload)

    assert rate == 48000
    assert samples.tolist() == pytest.approx([0.0, 0.5, -0.5], abs=1e-6)


def test_decode_wav_reads_a_wave_format_extensible_header() -> None:
    values = [0, 16384, -16384]
    payload = _riff(
        _extensible_fmt(1, 16000, 16, WAVE_FORMAT_PCM),
        struct.pack(f"<{len(values)}h", *values),
    )

    samples, rate = decode_wav(payload)

    assert rate == 16000
    assert samples.tolist() == pytest.approx([0.0, 0.5, -0.5], abs=1e-4)


def test_decode_wav_skips_unknown_chunks() -> None:
    values = [0, 16384]
    fmt_body = _fmt(WAVE_FORMAT_PCM, 1, 16000, 16)
    data_body = struct.pack(f"<{len(values)}h", *values)
    junk = b"LIST" + (5).to_bytes(4, "little") + b"INFOx" + b"\x00"
    body = b"".join(
        [
            b"WAVE",
            junk,
            b"fmt ",
            len(fmt_body).to_bytes(4, "little"),
            fmt_body,
            b"data",
            len(data_body).to_bytes(4, "little"),
            data_body,
        ]
    )
    payload = b"RIFF" + len(body).to_bytes(4, "little") + body

    samples, rate = decode_wav(payload)

    assert rate == 16000
    assert samples.shape == (2,)


def test_decode_wav_returns_the_frames_of_a_truncated_data_chunk() -> None:
    values = [1000, -1000, 2000]
    payload = _riff(
        _fmt(WAVE_FORMAT_PCM, 1, 16000, 16),
        struct.pack(f"<{len(values)}h", *values),
        declared_data_size=999,
    )

    samples, _rate = decode_wav(payload)

    assert samples.shape == (3,)


def test_decode_wav_ignores_a_trailing_partial_frame() -> None:
    payload = _riff(_fmt(WAVE_FORMAT_PCM, 1, 16000, 16), b"\x00\x01\x00")

    samples, _rate = decode_wav(payload)

    assert samples.shape == (1,)


@pytest.mark.parametrize(
    "payload",
    [
        b"",
        b"short",
        b"NOTARIFFHERE" + b"\x00" * 40,
        b"RIFF" + (4).to_bytes(4, "little") + b"AVI ",
    ],
)
def test_decode_wav_rejects_non_wave_payloads(payload: bytes) -> None:
    with pytest.raises(TranscriptionError):
        decode_wav(payload)


def test_decode_wav_rejects_a_wave_without_a_fmt_chunk() -> None:
    body = b"WAVE" + b"data" + (2).to_bytes(4, "little") + b"\x00\x00"
    payload = b"RIFF" + len(body).to_bytes(4, "little") + body

    with pytest.raises(TranscriptionError, match="fmt"):
        decode_wav(payload)


def test_decode_wav_rejects_a_wave_without_a_data_chunk() -> None:
    fmt_body = _fmt(WAVE_FORMAT_PCM, 1, 16000, 16)
    body = b"WAVE" + b"fmt " + len(fmt_body).to_bytes(4, "little") + fmt_body
    payload = b"RIFF" + len(body).to_bytes(4, "little") + body

    with pytest.raises(TranscriptionError, match="data"):
        decode_wav(payload)


def test_decode_wav_rejects_an_unsupported_format_tag() -> None:
    payload = _riff(_fmt(0x0011, 1, 16000, 16), b"\x00\x00\x00\x00")

    with pytest.raises(TranscriptionError, match="format tag"):
        decode_wav(payload)


def test_decode_wav_rejects_an_unsupported_bit_depth() -> None:
    payload = _riff(_fmt(WAVE_FORMAT_PCM, 1, 16000, 12), b"\x00" * 6)

    with pytest.raises(TranscriptionError, match="bit depth"):
        decode_wav(payload)


def test_decode_wav_rejects_a_zero_sample_rate() -> None:
    payload = _riff(_fmt(WAVE_FORMAT_PCM, 1, 0, 16), b"\x00\x00")

    with pytest.raises(TranscriptionError, match="sample rate"):
        decode_wav(payload)


def test_resample_linear_returns_the_input_when_rates_match() -> None:
    audio = np.linspace(-1.0, 1.0, 32, dtype=np.float32)

    assert resample_linear(audio, 16000, 16000) is audio


def test_resample_linear_returns_empty_input_unchanged() -> None:
    audio = np.zeros(0, dtype=np.float32)

    assert resample_linear(audio, 8000, 16000) is audio


@pytest.mark.parametrize(
    ("src", "dst", "length", "expected"),
    [(8000, 16000, 100, 200), (16000, 8000, 100, 50), (44100, 16000, 44100, 16000)],
)
def test_resample_linear_produces_the_expected_length(
    src: int, dst: int, length: int, expected: int
) -> None:
    audio = np.zeros(length, dtype=np.float32)

    assert resample_linear(audio, src, dst).shape == (expected,)


def test_resample_linear_interpolates_between_samples() -> None:
    audio = np.array([0.0, 1.0], dtype=np.float32)

    upsampled = resample_linear(audio, 1, 2)

    assert upsampled.dtype == np.float32
    assert upsampled.tolist() == pytest.approx([0.0, 0.5, 1.0, 1.0])


@pytest.mark.parametrize(("src", "dst"), [(0, 16000), (16000, 0), (-1, 16000)])
def test_resample_linear_rejects_impossible_rates(src: int, dst: int) -> None:
    with pytest.raises(TranscriptionError):
        resample_linear(np.zeros(4, dtype=np.float32), src, dst)


def test_decode_audio_reports_the_duration_of_a_matching_rate_wav() -> None:
    payload = _stdlib_wav(struct.pack("<8000h", *([0] * 8000)), channels=1, width=2, rate=16000)

    audio, seconds = decode_audio(payload)

    assert audio.shape == (8000,)
    assert seconds == pytest.approx(0.5)


def test_decode_audio_resamples_to_the_target_rate() -> None:
    payload = _stdlib_wav(struct.pack("<4000h", *([0] * 4000)), channels=1, width=2, rate=8000)

    audio, seconds = decode_audio(payload)

    assert audio.shape == (8000,)
    assert seconds == pytest.approx(0.5)


def test_decode_audio_honours_an_explicit_target_rate() -> None:
    payload = _stdlib_wav(struct.pack("<16000h", *([0] * 16000)), channels=1, width=2, rate=16000)

    audio, seconds = decode_audio(payload, target_rate=8000)

    assert audio.shape == (8000,)
    assert seconds == pytest.approx(1.0)


def test_decode_audio_rejects_an_empty_payload() -> None:
    with pytest.raises(TranscriptionError, match="empty"):
        decode_audio(b"")


def test_decode_audio_rejects_an_undecodable_payload() -> None:
    with pytest.raises(TranscriptionError):
        decode_audio(b"this is definitely not audio" * 4)


@pytest.fixture
def ctranslate2_devices(monkeypatch: pytest.MonkeyPatch) -> Callable[[int], None]:
    module = pytest.importorskip("ctranslate2")

    def install(count: int) -> None:
        monkeypatch.setattr(module, "get_cuda_device_count", lambda: count)

    return install


def test_resolve_device_picks_cuda_when_a_gpu_is_visible(
    ctranslate2_devices: Callable[[int], None],
) -> None:
    ctranslate2_devices(1)

    assert resolve_device("auto", "float16") == ("cuda", "float16")


def test_resolve_device_falls_back_to_cpu_and_int8_without_a_gpu(
    ctranslate2_devices: Callable[[int], None],
) -> None:
    ctranslate2_devices(0)

    assert resolve_device("auto", "float16") == ("cpu", "int8")


def test_resolve_device_never_raises_when_ctranslate2_explodes(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    module = pytest.importorskip("ctranslate2")

    def boom() -> int:
        raise RuntimeError("no cuda driver")

    monkeypatch.setattr(module, "get_cuda_device_count", boom)

    assert resolve_device("auto", "float16") == ("cpu", "int8")


@pytest.mark.parametrize(
    ("requested", "compute_type", "expected"),
    [
        ("cpu", "float16", ("cpu", "int8")),
        ("cpu", "bfloat16", ("cpu", "int8")),
        ("cpu", "int8_float16", ("cpu", "int8")),
        ("cpu", "int8", ("cpu", "int8")),
        ("cpu", "float32", ("cpu", "float32")),
        ("cuda", "float16", ("cuda", "float16")),
        ("cuda", "int8", ("cuda", "int8")),
    ],
)
def test_resolve_device_respects_an_explicit_device(
    requested: str, compute_type: str, expected: tuple[str, str]
) -> None:
    assert resolve_device(requested, compute_type) == expected
