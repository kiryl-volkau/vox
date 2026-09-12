from __future__ import annotations

import io
import struct
import wave
from typing import NamedTuple

import numpy as np
import pytest

from voice_code_client.recorder import encode_wav
from voice_code_server.transcription import decode_wav


class Decoded(NamedTuple):
    channels: int
    sample_width: int
    frame_rate: int
    frames: int
    data: bytes


def _read(payload: bytes) -> Decoded:
    with wave.open(io.BytesIO(payload), "rb") as wav:
        return Decoded(
            channels=wav.getnchannels(),
            sample_width=wav.getsampwidth(),
            frame_rate=wav.getframerate(),
            frames=wav.getnframes(),
            data=wav.readframes(wav.getnframes()),
        )


def test_encode_wav_writes_a_16_bit_mono_riff() -> None:
    samples = np.array([0.0, 0.5, -0.5, 1.0, -1.0], dtype=np.float32)

    payload = encode_wav(samples, 16000)
    decoded = _read(payload)

    assert payload[:4] == b"RIFF"
    assert payload[8:12] == b"WAVE"
    assert decoded.channels == 1
    assert decoded.sample_width == 2
    assert decoded.frame_rate == 16000
    assert decoded.frames == 5
    assert struct.unpack("<5h", decoded.data) == (0, 16383, -16383, 32767, -32767)


def test_encode_wav_clips_out_of_range_samples() -> None:
    samples = np.array([2.5, -2.5, 1.0001, -1.0001], dtype=np.float32)

    decoded = _read(encode_wav(samples, 16000))

    assert struct.unpack("<4h", decoded.data) == (32767, -32767, 32767, -32767)


def test_encode_wav_flattens_a_multi_dimensional_array() -> None:
    samples = np.array([[0.25], [-0.25], [0.5]], dtype=np.float32)

    decoded = _read(encode_wav(samples, 8000))

    assert decoded.frames == 3
    assert len(decoded.data) == 6


def test_encode_wav_accepts_an_empty_recording() -> None:
    decoded = _read(encode_wav(np.zeros(0, dtype=np.float32), 44100))

    assert decoded.frames == 0
    assert decoded.frame_rate == 44100
    assert decoded.data == b""


def test_encode_wav_never_writes_a_zero_sample_rate() -> None:
    assert _read(encode_wav(np.zeros(4, dtype=np.float32), 0)).frame_rate >= 1


@pytest.mark.parametrize("sample_rate", [8000, 16000, 44100, 48000])
def test_the_client_wav_round_trips_through_the_server_decoder(sample_rate: int) -> None:
    moments = np.arange(sample_rate // 10, dtype=np.float32) / sample_rate
    original = (0.6 * np.sin(2.0 * np.pi * 440.0 * moments)).astype(np.float32)

    decoded, decoded_rate = decode_wav(encode_wav(original, sample_rate))

    assert decoded_rate == sample_rate
    assert decoded.shape == original.shape
    assert decoded.dtype == np.float32
    assert np.max(np.abs(decoded - original)) < 1e-3


def test_an_empty_client_wav_is_still_decodable_by_the_server() -> None:
    decoded, rate = decode_wav(encode_wav(np.zeros(0, dtype=np.float32), 16000))

    assert rate == 16000
    assert decoded.shape == (0,)


def test_the_round_trip_preserves_clipped_extremes() -> None:
    original = np.array([1.0, -1.0, 0.0], dtype=np.float32)

    decoded, _rate = decode_wav(encode_wav(original, 16000))

    assert decoded.tolist() == pytest.approx([1.0, -1.0, 0.0], abs=1e-4)
