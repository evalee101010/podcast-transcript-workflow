from __future__ import annotations

from pathlib import Path
import tempfile
import unittest
from unittest import mock

from podcast_tracker.lark_export import LarkExport
from podcast_tracker.models import Episode
from podcast_tracker.publishing import PostGeneratePublisher


def _episode(transcript_path: Path | None) -> Episode:
    return Episode(
        id="ep1",
        subscription_id="sub1",
        program_title="节目",
        title="标题",
        source_url="https://example.com/ep1",
        audio_url="https://cdn.example.com/ep1.mp3",
        published_at="2026-06-22T00:00:00+00:00",
        created_at="2026-06-22T00:00:00+00:00",
        transcript_status="transcribed" if transcript_path else "pending",
        transcript_path=str(transcript_path) if transcript_path else None,
    )


class StubExporter:
    def __init__(self, error: str | None = None) -> None:
        self.error = error
        self.exported: list[tuple[str, Path]] = []

    def export_readable(self, episode: Episode, readable_path: Path) -> LarkExport:
        self.exported.append((episode.id, readable_path))
        if self.error:
            raise RuntimeError(self.error)
        return LarkExport(
            episode_id=episode.id,
            readable_path=str(readable_path),
            folder_name="Podcast Transcripts",
            folder_token="folder1",
            chat_name="Podcast Alerts",
            chat_id="chat1",
            lark_doc_url=f"https://example.feishu.cn/docx/{episode.id}",
            lark_doc_token=f"doc-{episode.id}",
            message_id=f"msg-{episode.id}",
            exported_at="now",
        )


class PostGeneratePublisherTests(unittest.TestCase):
    def test_default_publisher_skips_lark_when_not_enabled(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            readable_path = Path(tmpdir) / "episode-阅读版.md"
            readable_path.write_text("# 阅读版\n", encoding="utf-8")

            with mock.patch.dict("os.environ", {"PODCAST_TRACKER_ENABLE_LARK": ""}):
                result = PostGeneratePublisher().publish_readable(_episode(None), readable_path)

            self.assertFalse(result.ok)
            self.assertIsNone(result.lark_export)
            self.assertIsNone(result.lark_error)

    def test_publish_episode_exports_existing_readable(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            transcript_path = Path(tmpdir) / "episode.md"
            readable_path = Path(tmpdir) / "episode-阅读版.md"
            transcript_path.write_text("# 逐字稿\n", encoding="utf-8")
            readable_path.write_text("# 阅读版\n", encoding="utf-8")
            exporter = StubExporter()

            result = PostGeneratePublisher(exporter).publish_episode(_episode(transcript_path))

            self.assertTrue(result.ok)
            self.assertEqual(result.lark_export.lark_doc_url, "https://example.feishu.cn/docx/ep1")
            self.assertEqual(exporter.exported, [("ep1", readable_path)])

    def test_publish_episode_reports_missing_readable(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            transcript_path = Path(tmpdir) / "episode.md"
            transcript_path.write_text("# 逐字稿\n", encoding="utf-8")
            exporter = StubExporter()

            result = PostGeneratePublisher(exporter).publish_episode(_episode(transcript_path))

            self.assertFalse(result.ok)
            self.assertIn("Readable document not found", result.lark_error)
            self.assertEqual(exporter.exported, [])

    def test_publish_readable_reports_lark_error_without_raising(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            readable_path = Path(tmpdir) / "episode-阅读版.md"
            readable_path.write_text("# 阅读版\n", encoding="utf-8")

            result = PostGeneratePublisher(StubExporter(error="lark down")).publish_readable(
                _episode(None),
                readable_path,
            )

            self.assertFalse(result.ok)
            self.assertEqual(result.lark_error, "lark down")


if __name__ == "__main__":
    unittest.main()
