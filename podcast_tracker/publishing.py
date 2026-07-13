from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .lark_export import LarkExport, LarkExporter
from .models import Episode
from .readable import readable_path_for_episode


@dataclass(frozen=True)
class PublishResult:
    episode_id: str
    lark_export: LarkExport | None = None
    lark_error: str | None = None

    @property
    def ok(self) -> bool:
        return self.lark_export is not None and self.lark_error is None


class PostGeneratePublisher:
    """Shared publish step after a readable transcript has been generated."""

    def __init__(self, lark_exporter: Any | None = None) -> None:
        self.lark_exporter = lark_exporter
        if self.lark_exporter is None and _env_flag("PODCAST_TRACKER_ENABLE_LARK"):
            self.lark_exporter = LarkExporter()

    def publish_episode(self, episode: Episode) -> PublishResult:
        readable_path = readable_path_for_episode(episode)
        if readable_path is None:
            return PublishResult(
                episode_id=episode.id,
                lark_error="Readable document not found after successful generation; skipped Feishu export.",
            )
        return self.publish_readable(episode, readable_path)

    def publish_readable(self, episode: Episode, readable_path: Path) -> PublishResult:
        if self.lark_exporter is None:
            return PublishResult(episode_id=episode.id)
        try:
            return PublishResult(
                episode_id=episode.id,
                lark_export=self.lark_exporter.export_readable(episode, readable_path),
            )
        except Exception as exc:
            return PublishResult(episode_id=episode.id, lark_error=str(exc))


def _env_flag(name: str) -> bool:
    value = os.getenv(name, "")
    return value.strip().lower() in {"1", "true", "yes", "on"}
