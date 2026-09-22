import asyncio
import logging
from pathlib import Path

import numpy as np
import soundfile as sf

from app.config import settings
from app.services.converter import _run_ffmpeg


# Matches the sample rate the transcription pipeline already uses.
TARGET_RATE = 22050

LEVELS = ("off", "light", "deep")


def _reduce_noise(data: np.ndarray, rate: int) -> np.ndarray:
    """Run noisereduce with the level/strength/sensitivity from settings.

    light: stationary spectral gate — the noise profile is learned once from the
    quietest parts of the whole recording. Best against constant hum, hiss and
    equipment noise.
    deep: non-stationary mode — the profile is re-estimated continuously, so it
    also follows slowly changing background noise.
    """
    import noisereduce as nr

    strength = float(min(max(settings.denoise_strength, 0.0), 1.0))
    sensitivity = float(settings.denoise_sensitivity)
    if settings.denoise_level == "light":
        return nr.reduce_noise(
            y=data,
            sr=rate,
            stationary=True,
            prop_decrease=strength,
            n_std_thresh_stationary=sensitivity,
            n_fft=1024,
        )
    return nr.reduce_noise(
        y=data,
        sr=rate,
        stationary=False,
        prop_decrease=strength,
        thresh_n_mult_nonstationary=sensitivity,
        n_fft=1024,
    )


async def denoise_to_mp3(source: Path, output_mp3: Path) -> None:
    """Suppress background noise in an audio file and write an MP3 for transcription.

    The source is decoded to mono WAV at TARGET_RATE, the noise profile is
    estimated from the recording itself (no reference sample needed), and the
    cleaned signal is encoded with the same parameters as convert_to_mp3.
    """
    if settings.denoise_level not in ("light", "deep"):
        raise ValueError(f"Unknown denoise level: {settings.denoise_level}")

    output_mp3.parent.mkdir(parents=True, exist_ok=True)
    raw_wav = output_mp3.parent / "denoise-src.tmp.wav"
    clean_wav = output_mp3.parent / "denoise-clean.tmp.wav"

    try:
        rc, _, err = await _run_ffmpeg(
            "-i", str(source),
            "-vn",
            "-ac", "1",
            "-ar", str(TARGET_RATE),
            "-c:a", "pcm_f32le",
            str(raw_wav),
        )
        if rc != 0:
            raise ValueError(f"ffmpeg decode for denoising failed: {err}")

        data, rate = await asyncio.to_thread(sf.read, str(raw_wav), dtype="float32")
        if data.size == 0:
            raise ValueError("No audio samples for denoising")

        logging.info(
            "Denoising %s (%.1fs, level=%s, strength=%.2f)",
            source.name,
            data.shape[0] / rate,
            settings.denoise_level,
            settings.denoise_strength,
        )
        reduced = await asyncio.to_thread(_reduce_noise, data, rate)
        await asyncio.to_thread(sf.write, str(clean_wav), reduced, rate, subtype="PCM_16")

        kbps = settings.target_mp3_kbps
        rc, _, err = await _run_ffmpeg(
            "-i", str(clean_wav),
            "-vn",
            "-ar", str(TARGET_RATE),
            "-ac", "1",
            "-b:a", f"{kbps}k",
            "-map_metadata", "-1",
            str(output_mp3),
        )
        if rc != 0:
            raise ValueError(f"ffmpeg encode after denoising failed: {err}")
        logging.info("Denoising finished: %s", output_mp3.name)
    finally:
        raw_wav.unlink(missing_ok=True)
        clean_wav.unlink(missing_ok=True)
