import asyncio
import json
import shutil
from pathlib import Path
from app.config import settings


async def _run_ffmpeg(*args, cwd: Path | None = None) -> tuple[int, str, str]:
    cmd = ["ffmpeg", "-y", *args]
    proc = await asyncio.create_subprocess_exec(
        *cmd,
        cwd=cwd,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    stdout, stderr = await proc.communicate()
    return proc.returncode, stdout.decode("utf-8", errors="ignore"), stderr.decode("utf-8", errors="ignore")


async def _run_ffprobe(*args) -> tuple[int, str, str]:
    cmd = ["ffprobe", "-v", "error", *args]
    proc = await asyncio.create_subprocess_exec(
        *cmd,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    stdout, stderr = await proc.communicate()
    return proc.returncode, stdout.decode("utf-8", errors="ignore"), stderr.decode("utf-8", errors="ignore")


async def get_media_info(path: Path) -> dict:
    rc, out, err = await _run_ffprobe(
        "-show_streams",
        "-show_format",
        "-print_format", "json",
        str(path),
    )
    if rc != 0:
        raise ValueError(f"ffprobe failed: {err}")
    return json.loads(out)


async def get_duration(path: Path) -> float:
    rc, out, err = await _run_ffprobe(
        "-show_entries", "format=duration",
        "-of", "default=noprint_wrappers=1:nokey=1",
        str(path),
    )
    if rc != 0:
        raise ValueError(f"ffprobe duration failed: {err}")
    out = out.strip()
    if not out:
        raise ValueError("ffprobe returned empty duration")
    return float(out)


def has_audio_stream(info: dict) -> bool:
    streams = info.get("streams", [])
    for s in streams:
        if s.get("codec_type") == "audio":
            return True
    return False


def get_audio_stream(info: dict) -> dict:
    for s in info.get("streams", []):
        if s.get("codec_type") == "audio":
            return s
    return {}


def _get_bitrate_from_dict(d: dict, key: str) -> int | None:
    value = d.get(key)
    if value is None:
        return None
    try:
        return int(value) // 1000
    except (ValueError, TypeError):
        return None


def _parse_bitrate_kbps(info: dict) -> int | None:
    bitrate = _get_bitrate_from_dict(info, "bit_rate")
    if bitrate is not None:
        return bitrate
    tags = info.get("tags") or {}
    for tag in ("BPS", "BPS-eng", "BPS_eng"):
        bitrate = _get_bitrate_from_dict(tags, tag)
        if bitrate is not None:
            return bitrate
    return None


def get_audio_bitrate_kbps(info: dict) -> int | None:
    stream = get_audio_stream(info)
    bitrate = _parse_bitrate_kbps(stream)
    if bitrate is not None:
        return bitrate
    return _parse_bitrate_kbps(info.get("format", {}))


def is_mp3_passthrough(info: dict, threshold_kbps: int | None = None) -> bool:
    threshold_kbps = threshold_kbps if threshold_kbps is not None else settings.mp3_passthrough_kbps
    stream = get_audio_stream(info)
    if "mp3" not in stream.get("codec_name", "").lower():
        return False
    bitrate = get_audio_bitrate_kbps(info)
    if bitrate is None:
        return False
    return bitrate <= threshold_kbps


def calculate_chunk_minutes(output_bitrate_kbps: int, target_chunk_mb: int, max_chunk_minutes: int) -> int:
    if output_bitrate_kbps <= 0:
        return max_chunk_minutes
    max_seconds = (target_chunk_mb * 8 * 1024) / output_bitrate_kbps
    max_minutes = int(max_seconds // 60)
    if max_minutes < 1:
        max_minutes = 1
    return min(max_minutes, max_chunk_minutes)


async def convert_to_mp3(input_path: Path, output_path: Path, kbps: int | None = None, passthrough: bool = False) -> None:
    output_path.parent.mkdir(parents=True, exist_ok=True)
    if passthrough:
        rc, out, err = await _run_ffmpeg(
            "-i", str(input_path),
            "-vn",
            "-c:a", "copy",
            "-map_metadata", "-1",
            str(output_path),
        )
    else:
        kbps = kbps or settings.target_mp3_kbps
        rc, out, err = await _run_ffmpeg(
            "-i", str(input_path),
            "-vn",
            "-ar", "22050",
            "-ac", "1",
            "-b:a", f"{kbps}k",
            "-map_metadata", "-1",
            str(output_path),
        )
    if rc != 0:
        raise ValueError(f"ffmpeg conversion failed: {err}")


async def estimate_split_points(input_path: Path, chunk_minutes: int) -> list[tuple[float, float]]:
    duration = await get_duration(input_path)
    chunk_seconds = chunk_minutes * 60
    points = []
    start = 0.0
    while start < duration:
        end = min(start + chunk_seconds, duration)
        points.append((start, end))
        start = end
    return points


async def split_audio(input_path: Path, output_dir: Path, chunk_minutes: int, kbps: int | None = None, passthrough: bool = False) -> list[Path]:
    output_dir.mkdir(parents=True, exist_ok=True)
    points = await estimate_split_points(input_path, chunk_minutes)
    files = []
    for idx, (start, end) in enumerate(points):
        output_file = output_dir / f"chunk_{idx:03d}.mp3"
        duration = end - start
        args = [
            "-ss", str(start),
            "-t", str(duration),
            "-i", str(input_path),
            "-vn",
        ]
        if passthrough:
            args.extend(["-c:a", "copy"])
        else:
            kbps = kbps or settings.target_mp3_kbps
            args.extend(["-ar", "22050", "-ac", "1", "-b:a", f"{kbps}k"])
        args.extend(["-map_metadata", "-1", str(output_file)])
        rc, out, err = await _run_ffmpeg(*args)
        if rc != 0:
            raise ValueError(f"ffmpeg split failed for chunk {idx}: {err}")
        files.append(output_file)
    return files


def get_file_size_mb(path: Path) -> float:
    return path.stat().st_size / (1024 * 1024)
