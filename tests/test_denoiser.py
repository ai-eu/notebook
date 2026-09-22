import asyncio

import numpy as np
import soundfile as sf

from app.config import settings
from app.services import processing
from app.services.converter import _run_ffmpeg, get_duration
from app.services.denoiser import denoise_to_mp3
from tests.test_processing import _create_recording, _load, _stub_media_pipeline

SR = 22050
SECONDS = 10


def _make_noisy_wav(path) -> None:
    """A 440 Hz tone buried in white noise, like a voice over room noise."""
    t = np.linspace(0, SECONDS, SR * SECONDS, endpoint=False)
    tone = 0.3 * np.sin(2 * np.pi * 440.0 * t)
    rng = np.random.default_rng(42)
    noise = 0.2 * rng.standard_normal(t.shape[0])
    sf.write(str(path), (tone + noise).astype(np.float32), SR, subtype="PCM_16")


def _decode_rms(path) -> tuple[float, float]:
    """Decode any audio file via ffmpeg and return (rms, duration)."""
    raw = path.parent / "probe.wav"
    try:
        rc, _, err = asyncio.run(_run_ffmpeg("-i", str(path), "-vn", "-ac", "1", "-ar", str(SR), "-f", "wav", str(raw)))
        assert rc == 0, err
        data, rate = sf.read(str(raw), dtype="float32")
        rms = float(np.sqrt(np.mean(data**2)))
        return rms, data.shape[0] / rate
    finally:
        raw.unlink(missing_ok=True)


def test_light_level_reduces_noise_and_keeps_duration(monkeypatch, tmp_path):
    monkeypatch.setattr(settings, "denoise_level", "light")
    monkeypatch.setattr(settings, "denoise_strength", 0.9)
    monkeypatch.setattr(settings, "denoise_sensitivity", 2.0)

    noisy = tmp_path / "noisy.wav"
    out = tmp_path / "clean.mp3"
    _make_noisy_wav(noisy)
    rms_before, _ = _decode_rms(noisy)

    asyncio.run(denoise_to_mp3(noisy, out))

    assert out.exists() and out.stat().st_size > 0
    rms_after, duration = _decode_rms(out)
    assert abs(duration - SECONDS) < 0.5
    assert rms_after < rms_before * 0.9


def test_deep_level_produces_output(monkeypatch, tmp_path):
    monkeypatch.setattr(settings, "denoise_level", "deep")
    monkeypatch.setattr(settings, "denoise_strength", 0.9)
    monkeypatch.setattr(settings, "denoise_sensitivity", 2.0)

    noisy = tmp_path / "noisy.wav"
    out = tmp_path / "clean.mp3"
    _make_noisy_wav(noisy)

    asyncio.run(denoise_to_mp3(noisy, out))

    assert out.exists() and out.stat().st_size > 0
    rms_after, duration = _decode_rms(out)
    assert abs(duration - SECONDS) < 0.5


def test_processing_falls_back_to_original_when_denoising_fails(monkeypatch, tmp_path):
    async def fake_transcribe(path, api_key, language=None):
        return {"text": "hi", "segments": [], "duration": 5.0}

    folder = _stub_media_pipeline(monkeypatch, tmp_path, fake_transcribe)

    async def broken_denoise(src, dst):
        raise ValueError("denoiser exploded")

    monkeypatch.setattr(processing, "denoise_to_mp3", broken_denoise)
    monkeypatch.setattr(settings, "mock_transcription", False)
    monkeypatch.setattr(settings, "denoise_level", "light")

    async def scenario():
        _, recording_id = await _create_recording()
        await processing.process_recording(recording_id)
        recording, _ = await _load(recording_id)
        assert recording.status == "done"
        assert (folder / "audio.mp3").exists()

    asyncio.run(scenario())


def test_denoising_is_skipped_in_mock_mode(monkeypatch, tmp_path):
    async def fake_transcribe(path, api_key, language=None):
        return {"text": "hi", "segments": [], "duration": 5.0}

    folder = _stub_media_pipeline(monkeypatch, tmp_path, fake_transcribe)
    monkeypatch.setattr(settings, "mock_transcription", True)
    monkeypatch.setattr(settings, "denoise_level", "deep")

    called = False

    async def spy_denoise(src, dst):
        nonlocal called
        called = True
        raise AssertionError("denoiser must not run in mock mode")

    monkeypatch.setattr(processing, "denoise_to_mp3", spy_denoise)

    async def scenario():
        _, recording_id = await _create_recording()
        await processing.process_recording(recording_id)
        recording, _ = await _load(recording_id)
        assert recording.status == "done"

    asyncio.run(scenario())
    assert called is False
