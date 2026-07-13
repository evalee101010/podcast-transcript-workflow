from __future__ import annotations

import json
import re
from pathlib import Path

from .config import DOCS_DIR
from .models import Episode, TranscriptSegment, utc_now_iso


def load_segments(path: Path) -> list[TranscriptSegment]:
    with path.open("r", encoding="utf-8") as file:
        data = json.load(file)
    rows = data.get("segments", data) if isinstance(data, dict) else data
    if not isinstance(rows, list):
        raise ValueError("Transcript JSON must be a list or an object with a 'segments' list")
    return [TranscriptSegment.from_dict(row) for row in rows]


def load_plain_text(path: Path, speaker: str) -> list[TranscriptSegment]:
    with path.open("r", encoding="utf-8") as file:
        return [TranscriptSegment(speaker=speaker, text=file.read())]


def write_transcript_markdown(
    episode: Episode,
    segments: list[TranscriptSegment],
    output_dir: Path = DOCS_DIR,
) -> Path:
    if not segments:
        raise ValueError("At least one transcript segment is required")

    output_dir.mkdir(parents=True, exist_ok=True)
    path = _unique_path(output_dir / transcript_filename(episode))
    lines = [
        f"# {episode.title}",
        "",
        f"- 节目：{episode.program_title}",
        f"- 发布时间：{episode.published_at or ''}",
        f"- 原始链接：{episode.source_url}",
        f"- 音频链接：{episode.audio_url or ''}",
        f"- 转写时间：{utc_now_iso()}",
        "- 整理方式：逐字稿；按说话人分段；不删减、不总结",
        "",
        "## 逐字稿",
        "",
    ]

    for segment in segments:
        lines.extend(_format_segment(segment))

    path.write_text("\n".join(lines).rstrip() + "\n", encoding="utf-8")
    return path


def transcript_filename(episode: Episode) -> str:
    date_part = (episode.published_at or episode.created_at or "unknown")[:10]
    base = f"{date_part}-{episode.program_title}-{episode.title}"
    return f"{safe_filename(base)}.md"


def safe_filename(value: str, max_length: int = 120) -> str:
    cleaned = re.sub(r"[\\/:*?\"<>|\n\r\t]+", "-", value)
    cleaned = re.sub(r"\s+", " ", cleaned).strip(" .-")
    if not cleaned:
        cleaned = "transcript"
    return cleaned[:max_length].rstrip(" .-")


def _format_segment(segment: TranscriptSegment) -> list[str]:
    speaker = segment.speaker or "Speaker"
    lines = [f"### {speaker}"]
    timestamp = _format_timestamp(segment)
    if timestamp:
        lines.append("")
        lines.append(timestamp)
    lines.append("")
    lines.append(segment.text)
    lines.append("")
    return lines


def _format_timestamp(segment: TranscriptSegment) -> str | None:
    if segment.start and segment.end:
        return f"[{segment.start} - {segment.end}]"
    if segment.start:
        return f"[{segment.start}]"
    return None


def _unique_path(path: Path) -> Path:
    if not path.exists():
        return path
    stem = path.stem
    suffix = path.suffix
    for index in range(2, 1000):
        candidate = path.with_name(f"{stem}-{index}{suffix}")
        if not candidate.exists():
            return candidate
    raise FileExistsError(f"Too many duplicate transcript files for {path.name}")
