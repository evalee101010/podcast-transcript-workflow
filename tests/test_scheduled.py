from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path
import tempfile
import unittest

from podcast_tracker.feed import FeedEpisode, ParsedFeed
from podcast_tracker.jobs import ReadableJob
from podcast_tracker.lark_export import LarkExport
from podcast_tracker.models import Episode, Subscription
from podcast_tracker.scheduled import render_scheduled_update_report, run_scheduled_update
from podcast_tracker.store import Store


def _store(data_dir: Path) -> Store:
    return Store(
        data_dir=data_dir,
        subscriptions_file=data_dir / "subscriptions.json",
        episodes_file=data_dir / "episodes.json",
    )


def _subscription() -> Subscription:
    return Subscription(
        id="sub1",
        title="测试节目",
        feed_url="https://example.com/feed.xml",
        source_url="https://example.com",
        created_at="2026-06-22T00:00:00+00:00",
    )


def _feed(*episodes: FeedEpisode) -> ParsedFeed:
    return ParsedFeed(
        id="sub1",
        title="测试节目",
        feed_url="https://example.com/feed.xml",
        source_url="https://example.com",
        episodes=list(episodes),
    )


def _feed_episode(episode_id: str) -> FeedEpisode:
    return FeedEpisode(
        id=episode_id,
        title=f"第 {episode_id} 期",
        source_url=f"https://example.com/{episode_id}",
        audio_url=f"https://cdn.example.com/{episode_id}.mp3",
        published_at="2026-06-22T00:00:00+00:00",
    )


class StubJobManager:
    def __init__(
        self,
        status: str = "succeeded",
        store: Store | None = None,
        docs_dir: Path | None = None,
    ) -> None:
        self.status = status
        self.store = store
        self.docs_dir = docs_dir
        self.started: list[str] = []

    def start_readable_job(self, episode_id: str) -> ReadableJob:
        self.started.append(episode_id)
        if self.status == "succeeded" and self.store and self.docs_dir:
            self.docs_dir.mkdir(parents=True, exist_ok=True)
            transcript_path = self.docs_dir / f"{episode_id}.md"
            readable_path = self.docs_dir / f"{episode_id}-阅读版.md"
            transcript_path.write_text("# 逐字稿\n", encoding="utf-8")
            readable_path.write_text("# 阅读版\n", encoding="utf-8")
            self.store.mark_transcribed(episode_id, transcript_path)
        return ReadableJob(
            id=f"job-{episode_id}",
            episode_id=episode_id,
            status=self.status,
            created_at="now",
            stage=self.status,
            progress=100 if self.status == "succeeded" else 75,
            error=None if self.status == "succeeded" else "boom",
        )


class StubLarkExporter:
    def __init__(self, error: str | None = None) -> None:
        self.error = error
        self.exported: list[tuple[str, Path]] = []

    def export_readable(self, episode, readable_path: Path) -> LarkExport:
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


class ScheduledUpdateTests(unittest.TestCase):
    def test_scheduled_update_generates_only_new_episodes(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            store = _store(Path(tmpdir))
            store.upsert_subscription(_subscription())
            manager = StubJobManager(store=store, docs_dir=Path(tmpdir) / "docs")
            lark = StubLarkExporter()

            report = run_scheduled_update(
                store,
                resolver=lambda _url: _feed(_feed_episode("ep1")),
                job_manager=manager,
                lark_exporter=lark,
            )

            self.assertEqual(manager.started, ["ep1"])
            self.assertEqual([episode_id for episode_id, _path in lark.exported], ["ep1"])
            self.assertEqual(report.check_report.total_new, 1)
            self.assertEqual(report.succeeded_count, 1)
            self.assertEqual(report.lark_succeeded_count, 1)
            self.assertEqual(report.failed_count, 0)

            second = run_scheduled_update(
                store,
                resolver=lambda _url: _feed(_feed_episode("ep1")),
                job_manager=manager,
                lark_exporter=lark,
            )

            self.assertEqual(manager.started, ["ep1"])
            self.assertEqual([episode_id for episode_id, _path in lark.exported], ["ep1"])
            self.assertEqual(second.check_report.total_new, 0)
            self.assertEqual(second.attempted_count, 0)

    def test_scheduled_update_reports_generation_failure(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            store = _store(Path(tmpdir))
            store.upsert_subscription(_subscription())
            manager = StubJobManager(status="failed")

            report = run_scheduled_update(
                store,
                resolver=lambda _url: _feed(_feed_episode("ep1")),
                job_manager=manager,
                lark_exporter=StubLarkExporter(),
            )

            self.assertEqual(report.generation_failed_count, 1)
            self.assertEqual(report.lark_attempted_count, 0)
            self.assertEqual(report.failed_count, 1)
            rendered = render_scheduled_update_report(report)
            self.assertIn("Readable generation failed: 1", rendered)
            self.assertIn("boom", rendered)

    def test_scheduled_update_reports_lark_export_failure(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            store = _store(Path(tmpdir))
            store.upsert_subscription(_subscription())
            manager = StubJobManager(store=store, docs_dir=Path(tmpdir) / "docs")
            lark = StubLarkExporter(error="lark down")

            report = run_scheduled_update(
                store,
                resolver=lambda _url: _feed(_feed_episode("ep1")),
                job_manager=manager,
                lark_exporter=lark,
            )

            self.assertEqual(report.succeeded_count, 1)
            self.assertEqual(report.lark_failed_count, 1)
            self.assertEqual(report.failed_count, 1)
            rendered = render_scheduled_update_report(report)
            self.assertIn("Feishu link failed: 1", rendered)
            self.assertIn("lark down", rendered)

    def test_scheduled_update_retries_previously_failed_episode(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            store = _store(Path(tmpdir))
            store.upsert_subscription(_subscription())

            # 第一次：新单集生成失败。
            failing = StubJobManager(status="failed")
            first = run_scheduled_update(
                store,
                resolver=lambda _url: _feed(_feed_episode("ep1")),
                job_manager=failing,
                lark_exporter=StubLarkExporter(),
            )
            self.assertEqual(first.generation_failed_count, 1)

            # 第二次：没有新单集，但上次失败的单集被自动补跑并成功。
            retrying = RetryStubManager(
                failed_episode_ids=["ep1"],
                store=store,
                docs_dir=Path(tmpdir) / "docs",
            )
            lark = StubLarkExporter()
            second = run_scheduled_update(
                store,
                resolver=lambda _url: _feed(_feed_episode("ep1")),
                job_manager=retrying,
                lark_exporter=lark,
            )

            self.assertEqual(second.check_report.total_new, 0)
            self.assertEqual(retrying.started, ["ep1"])
            self.assertEqual(second.succeeded_count, 1)
            self.assertTrue(second.generated[0].retried)
            self.assertEqual([episode_id for episode_id, _path in lark.exported], ["ep1"])
            rendered = render_scheduled_update_report(second)
            self.assertIn("（自动补跑）", rendered)

    def test_scheduled_update_recovers_recent_unfinished_episode_without_job_history(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            store = _store(Path(tmpdir))
            store.upsert_subscription(_subscription())
            now = datetime.now(timezone.utc)
            store.upsert_episode(
                Episode(
                    id="missed",
                    subscription_id="sub1",
                    program_title="测试节目",
                    title="漏跑的一期",
                    source_url="https://example.com/missed",
                    audio_url="https://cdn.example.com/missed.mp3",
                    published_at=(now - timedelta(days=2)).isoformat(),
                    created_at=(now - timedelta(days=2)).isoformat(),
                )
            )
            manager = StubJobManager(store=store, docs_dir=Path(tmpdir) / "docs")

            report = run_scheduled_update(
                store,
                resolver=lambda _url: _feed(),
                job_manager=manager,
                lark_exporter=StubLarkExporter(),
            )

            self.assertEqual(manager.started, ["missed"])
            self.assertEqual(report.succeeded_count, 1)
            self.assertTrue(report.generated[0].retried)

    def test_scheduled_update_does_not_recover_old_unfinished_history(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            store = _store(Path(tmpdir))
            store.upsert_subscription(_subscription())
            old = datetime.now(timezone.utc) - timedelta(days=30)
            store.upsert_episode(
                Episode(
                    id="old",
                    subscription_id="sub1",
                    program_title="测试节目",
                    title="旧节目",
                    source_url="https://example.com/old",
                    audio_url="https://cdn.example.com/old.mp3",
                    published_at=old.isoformat(),
                    created_at=old.isoformat(),
                )
            )
            manager = StubJobManager(store=store, docs_dir=Path(tmpdir) / "docs")

            report = run_scheduled_update(
                store,
                resolver=lambda _url: _feed(),
                job_manager=manager,
                lark_exporter=StubLarkExporter(),
            )

            self.assertEqual(manager.started, [])
            self.assertEqual(report.attempted_count, 0)


class RetryStubManager(StubJobManager):
    """StubJobManager whose latest_by_episode reports previously failed jobs."""

    def __init__(self, failed_episode_ids: list[str], **kwargs) -> None:
        super().__init__(**kwargs)
        self.failed_episode_ids = failed_episode_ids

    def latest_by_episode(self) -> dict[str, ReadableJob]:
        return {
            episode_id: ReadableJob(
                id=f"old-{episode_id}",
                episode_id=episode_id,
                status="failed",
                created_at="before",
                stage="failed",
                progress=0,
            )
            for episode_id in self.failed_episode_ids
        }


if __name__ == "__main__":
    unittest.main()
