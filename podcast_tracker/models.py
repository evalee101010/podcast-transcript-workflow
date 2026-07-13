from __future__ import annotations

from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from typing import Any


def utc_now_iso() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


@dataclass(frozen=True)
class Subscription:
    id: str
    title: str
    feed_url: str
    source_url: str
    created_at: str
    latest_episode_id: str | None = None
    last_checked_at: str | None = None
    last_check_error: str | None = None
    avatar_url: str | None = None

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "Subscription":
        return cls(**data)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True)
class Episode:
    id: str
    subscription_id: str
    program_title: str
    title: str
    source_url: str
    audio_url: str | None
    published_at: str | None
    created_at: str
    transcript_status: str = "pending"
    transcript_path: str | None = None

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "Episode":
        return cls(**data)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True)
class TranscriptSegment:
    speaker: str
    text: str
    start: str | None = None
    end: str | None = None

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "TranscriptSegment":
        return cls(
            speaker=str(data.get("speaker") or data.get("role") or "Speaker"),
            text=str(data.get("text") or ""),
            start=data.get("start"),
            end=data.get("end"),
        )
