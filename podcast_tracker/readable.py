from __future__ import annotations

from pathlib import Path
import json

from .models import Episode


def readable_path_for_episode(episode: Episode) -> Path | None:
    if not episode.transcript_path:
        return None
    transcript_path = Path(episode.transcript_path)
    readable_path = transcript_path.with_name(f"{transcript_path.stem}-阅读版{transcript_path.suffix}")
    if readable_path.exists() and readable_path.is_file():
        return readable_path
    return None


def has_readable(episode: Episode) -> bool:
    return readable_path_for_episode(episode) is not None


def speaker_map_path_for_readable(readable_path: Path) -> Path:
    return readable_path.with_suffix(".speaker-map.json")


def load_speaker_map_for_readable(readable_path: Path) -> dict[str, str]:
    path = speaker_map_path_for_readable(readable_path)
    if not path.exists():
        return {}
    data = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(data, dict):
        raise ValueError(f"Speaker map must be a JSON object: {path}")
    mapping: dict[str, str] = {}
    for source, target in data.items():
        if not isinstance(source, str) or not isinstance(target, str):
            raise ValueError(f"Speaker map keys and values must be strings: {path}")
        source = source.strip()
        target = target.strip()
        if source and target:
            mapping[source] = target
    return mapping


def format_speaker_map_arg(mapping: dict[str, str]) -> str:
    return ",".join(f"{source}={target}" for source, target in mapping.items())
