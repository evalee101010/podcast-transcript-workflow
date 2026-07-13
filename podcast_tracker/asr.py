from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import urllib.parse
import urllib.request
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .config import DATA_DIR, PROJECT_ROOT
from .models import Episode, TranscriptSegment
from .transcript import write_transcript_markdown


MAX_AUDIO_BYTES = 25 * 1024 * 1024
DEFAULT_CHUNK_SECONDS = 20 * 60
DEFAULT_MODEL = "gpt-4o-transcribe-diarize"
DEFAULT_RESPONSE_FORMAT = "diarized_json"
DEFAULT_CHUNKING_STRATEGY = "auto"
DEFAULT_WORK_DIR = DATA_DIR / "audio"
SUPPORTED_AUDIO_SUFFIXES = {".mp3", ".mp4", ".mpeg", ".mpga", ".m4a", ".wav", ".webm"}


@dataclass(frozen=True)
class AutoTranscribeOptions:
    language: str | None = "zh"
    chunk_seconds: int = DEFAULT_CHUNK_SECONDS
    max_bytes: int = MAX_AUDIO_BYTES
    work_dir: Path = DEFAULT_WORK_DIR


@dataclass(frozen=True)
class AutoTranscribeResult:
    transcript_path: Path
    work_dir: Path
    audio_path: Path
    chunks: list[Path]
    raw_outputs: list[Path]
    segment_count: int


def transcribe_episode_auto(
    episode: Episode,
    options: AutoTranscribeOptions | None = None,
) -> AutoTranscribeResult:
    options = options or AutoTranscribeOptions()
    validate_ready_for_live_transcribe()

    run_dir = make_run_dir(options.work_dir, episode.id)
    audio_path = download_audio(episode, run_dir)
    chunks = prepare_audio_chunks(audio_path, run_dir, options)
    raw_outputs = transcribe_chunks(chunks, run_dir, options)

    segments: list[TranscriptSegment] = []
    for index, raw_output in enumerate(raw_outputs):
        offset = index * options.chunk_seconds if len(raw_outputs) > 1 else 0
        payload = json.loads(raw_output.read_text(encoding="utf-8"))
        segments.extend(segments_from_diarized_payload(payload, offset_seconds=offset))

    if not segments:
        raise RuntimeError("ASR completed, but no transcript segments were returned.")

    transcript_path = write_transcript_markdown(episode, merge_adjacent_segments(segments))
    return AutoTranscribeResult(
        transcript_path=transcript_path,
        work_dir=run_dir,
        audio_path=audio_path,
        chunks=chunks,
        raw_outputs=raw_outputs,
        segment_count=len(segments),
    )


def dry_run_report(
    episode: Episode,
    options: AutoTranscribeOptions | None = None,
) -> list[str]:
    options = options or AutoTranscribeOptions()
    lines = [
        f"Episode: {episode.id}",
        f"Title: {episode.title}",
        f"Audio URL: {episode.audio_url or '(missing)'}",
        f"Work dir: {options.work_dir / episode.id}",
        f"Chunk seconds: {options.chunk_seconds}",
        f"Max request bytes: {options.max_bytes}",
        f"Language hint: {options.language or '(none)'}",
        f"Transcribe CLI: {transcribe_cli_path()}",
        f"Python for transcribe CLI: {python_for_transcribe_cli()}",
        f"ffmpeg: {shutil.which('ffmpeg') or '(missing)'}",
        f"OPENAI_API_KEY: {'set' if os.getenv('OPENAI_API_KEY') else 'missing'}",
    ]
    if not episode.audio_url:
        lines.append("Status: cannot run; episode has no audio_url.")
    elif not transcribe_cli_path().exists():
        lines.append("Status: cannot run; install the official transcribe skill first.")
    elif not os.getenv("OPENAI_API_KEY"):
        lines.append("Status: ready after OPENAI_API_KEY is exported locally.")
    elif not shutil.which("ffmpeg"):
        lines.append("Status: can only run files under 25MB; install ffmpeg for large audio.")
    else:
        lines.append("Status: ready to download, split if needed, and transcribe.")
    return lines


def validate_ready_for_live_transcribe() -> None:
    if not transcribe_cli_path().exists():
        raise RuntimeError(
            "Official transcribe skill is not installed. "
            "Install openai/skills skills/.curated/transcribe first."
        )
    if not os.getenv("OPENAI_API_KEY"):
        raise RuntimeError(
            "OPENAI_API_KEY is not set. Export it locally before running live ASR."
        )


def make_run_dir(base_dir: Path, episode_id: str) -> Path:
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    root = base_dir / episode_id
    candidate = root / stamp
    index = 2
    while candidate.exists():
        candidate = root / f"{stamp}-{index}"
        index += 1
    candidate.mkdir(parents=True, exist_ok=False)
    return candidate


def download_audio(episode: Episode, run_dir: Path) -> Path:
    if not episode.audio_url:
        raise RuntimeError(f"Episode has no audio_url: {episode.id}")

    suffix = audio_suffix_from_url(episode.audio_url)
    output_path = run_dir / f"source{suffix}"
    request = urllib.request.Request(
        episode.audio_url,
        headers={"User-Agent": "podcast-tracker/0.1"},
    )
    with urllib.request.urlopen(request) as response:
        with output_path.open("wb") as file:
            shutil.copyfileobj(response, file)
    if output_path.stat().st_size == 0:
        raise RuntimeError("Downloaded audio is empty.")
    return output_path


def audio_suffix_from_url(url: str) -> str:
    path = urllib.parse.urlparse(url).path
    suffix = Path(path).suffix.lower()
    return suffix if suffix in SUPPORTED_AUDIO_SUFFIXES else ".m4a"


def prepare_audio_chunks(
    audio_path: Path,
    run_dir: Path,
    options: AutoTranscribeOptions,
) -> list[Path]:
    if audio_path.stat().st_size <= options.max_bytes:
        return [audio_path]

    ffmpeg = shutil.which("ffmpeg")
    if not ffmpeg:
        raise RuntimeError("Audio exceeds 25MB and ffmpeg is not installed.")

    chunks_dir = run_dir / "chunks"
    chunks_dir.mkdir(parents=True, exist_ok=True)
    chunk_pattern = chunks_dir / "chunk_%03d.m4a"
    command = [
        ffmpeg,
        "-hide_banner",
        "-loglevel",
        "error",
        "-y",
        "-i",
        str(audio_path),
        "-vn",
        "-ac",
        "1",
        "-ar",
        "16000",
        "-c:a",
        "aac",
        "-b:a",
        "64k",
        "-f",
        "segment",
        "-segment_time",
        str(options.chunk_seconds),
        "-reset_timestamps",
        "1",
        str(chunk_pattern),
    ]
    run_command(command, "ffmpeg split failed")

    chunks = sorted(chunks_dir.glob("chunk_*.m4a"))
    if not chunks:
        raise RuntimeError("ffmpeg did not produce any audio chunks.")

    oversize = [path for path in chunks if path.stat().st_size > options.max_bytes]
    if oversize:
        names = ", ".join(path.name for path in oversize)
        raise RuntimeError(
            f"Audio chunks still exceed 25MB: {names}. "
            "Retry with a smaller --chunk-seconds value."
        )
    return chunks


def transcribe_chunks(
    chunks: list[Path],
    run_dir: Path,
    options: AutoTranscribeOptions,
) -> list[Path]:
    raw_dir = run_dir / "raw"
    raw_dir.mkdir(parents=True, exist_ok=True)
    outputs: list[Path] = []
    for index, chunk in enumerate(chunks):
        output_path = raw_dir / f"chunk_{index:03d}.json"
        command = [
            str(python_for_transcribe_cli()),
            str(transcribe_cli_path()),
            str(chunk),
            "--model",
            DEFAULT_MODEL,
            "--response-format",
            DEFAULT_RESPONSE_FORMAT,
            "--chunking-strategy",
            DEFAULT_CHUNKING_STRATEGY,
            "--out",
            str(output_path),
        ]
        if options.language:
            command.extend(["--language", options.language])
        run_command(command, f"OpenAI transcription failed for {chunk.name}")
        outputs.append(output_path)
    return outputs


def transcribe_cli_path() -> Path:
    configured = os.getenv("TRANSCRIBE_CLI")
    if configured:
        return Path(configured).expanduser()
    codex_home = Path(os.getenv("CODEX_HOME", str(Path.home() / ".codex"))).expanduser()
    return codex_home / "skills" / "transcribe" / "scripts" / "transcribe_diarize.py"


def python_for_transcribe_cli() -> Path:
    venv_python = PROJECT_ROOT / ".venv" / "bin" / "python"
    return venv_python if venv_python.exists() else Path(sys.executable)


def run_command(command: list[str], label: str) -> None:
    try:
        subprocess.run(command, check=True, capture_output=True, text=True)
    except subprocess.CalledProcessError as exc:
        detail = (exc.stderr or exc.stdout or "").strip()
        if detail:
            raise RuntimeError(f"{label}: {detail}") from exc
        raise RuntimeError(label) from exc


def segments_from_diarized_payload(
    payload: Any,
    offset_seconds: float = 0,
) -> list[TranscriptSegment]:
    rows = extract_segment_rows(payload)
    if not rows and isinstance(payload, dict) and payload.get("text"):
        rows = [
            {
                "speaker": "Speaker",
                "text": payload["text"],
                "start": payload.get("start"),
                "end": payload.get("end"),
            }
        ]

    segments: list[TranscriptSegment] = []
    for row in rows:
        if not isinstance(row, dict):
            continue
        text = str(row.get("text") or row.get("transcript") or row.get("content") or "")
        if not text.strip():
            continue
        speaker = str(
            row.get("speaker")
            or row.get("speaker_label")
            or row.get("speaker_id")
            or row.get("role")
            or "Speaker"
        )
        segments.append(
            TranscriptSegment(
                speaker=speaker,
                text=text.strip(),
                start=format_timestamp(row.get("start"), offset_seconds),
                end=format_timestamp(row.get("end"), offset_seconds),
            )
        )
    return segments


def extract_segment_rows(payload: Any) -> list[Any]:
    if isinstance(payload, list):
        return payload
    if not isinstance(payload, dict):
        return []
    for key in ("segments", "transcript", "utterances"):
        value = payload.get(key)
        if isinstance(value, list):
            return value
    return []


def format_timestamp(value: Any, offset_seconds: float = 0) -> str | None:
    seconds = seconds_value(value)
    if seconds is None:
        return str(value) if value else None
    total = max(0, seconds + offset_seconds)
    hours = int(total // 3600)
    minutes = int((total % 3600) // 60)
    secs = total % 60
    return f"{hours:02d}:{minutes:02d}:{secs:06.3f}"


def seconds_value(value: Any) -> float | None:
    if value is None:
        return None
    if isinstance(value, (int, float)):
        return float(value)
    if isinstance(value, str):
        text = value.strip()
        if not text:
            return None
        try:
            return float(text)
        except ValueError:
            return None
    return None


def merge_adjacent_segments(segments: list[TranscriptSegment]) -> list[TranscriptSegment]:
    merged: list[TranscriptSegment] = []
    for segment in segments:
        if merged and merged[-1].speaker == segment.speaker:
            previous = merged[-1]
            merged[-1] = TranscriptSegment(
                speaker=previous.speaker,
                text=f"{previous.text}\n{segment.text}",
                start=previous.start,
                end=segment.end or previous.end,
            )
            continue
        merged.append(segment)
    return merged
