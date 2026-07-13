"""Shared audio helpers for local ASR backends.

FunASR and Whisper both work best on 16 kHz mono PCM WAV. This module wraps the
single ffmpeg call used to normalise downloaded podcast audio (usually .m4a)
into that format. No resampling/encoding decisions are left to the ASR engine,
which keeps results reproducible across backends.
"""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path


def ffmpeg_path() -> str:
    path = shutil.which("ffmpeg")
    if not path:
        raise RuntimeError(
            "ffmpeg is not installed. Install it first (macOS: `brew install ffmpeg`)."
        )
    return path


def to_wav_16k_mono(src: Path, dst: Path) -> Path:
    """Convert any audio file to 16 kHz mono 16-bit PCM WAV.

    This is the input format FunASR (Paraformer/SenseVoice) and faster-whisper
    expect. The conversion is lossless from the model's point of view and is the
    only preprocessing we apply — we never trim, denoise, or otherwise alter the
    content, so the transcript stays verbatim.
    """
    dst.parent.mkdir(parents=True, exist_ok=True)
    command = [
        ffmpeg_path(),
        "-hide_banner",
        "-loglevel",
        "error",
        "-y",
        "-i",
        str(src),
        "-vn",
        "-ac",
        "1",
        "-ar",
        "16000",
        "-sample_fmt",
        "s16",
        str(dst),
    ]
    try:
        subprocess.run(command, check=True, capture_output=True, text=True)
    except subprocess.CalledProcessError as exc:
        detail = (exc.stderr or exc.stdout or "").strip()
        raise RuntimeError(f"ffmpeg conversion failed: {detail or 'unknown error'}") from exc
    if not dst.exists() or dst.stat().st_size == 0:
        raise RuntimeError("ffmpeg produced an empty WAV file.")
    return dst


def ms_to_timestamp(value: object) -> str | None:
    """Convert milliseconds (int/float/str) to HH:MM:SS.mmm."""
    seconds = _to_seconds(value, divisor=1000.0)
    return _format_seconds(seconds)


def seconds_to_timestamp(value: object) -> str | None:
    """Convert seconds (int/float/str) to HH:MM:SS.mmm."""
    seconds = _to_seconds(value, divisor=1.0)
    return _format_seconds(seconds)


def _to_seconds(value: object, divisor: float) -> float | None:
    if value is None:
        return None
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return float(value) / divisor
    if isinstance(value, str):
        text = value.strip()
        if not text:
            return None
        try:
            return float(text) / divisor
        except ValueError:
            return None
    return None


def _format_seconds(seconds: float | None) -> str | None:
    if seconds is None:
        return None
    total = max(0.0, seconds)
    hours = int(total // 3600)
    minutes = int((total % 3600) // 60)
    secs = total % 60
    return f"{hours:02d}:{minutes:02d}:{secs:06.3f}"
