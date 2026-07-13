from pathlib import Path
import tempfile
import unittest

from podcast_tracker.checker import render_check_report, run_check
from podcast_tracker.feed import FeedEpisode, ParsedFeed
from podcast_tracker.models import Episode, Subscription
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


def _feed_episode(episode_id: str, title: str, published_at: str) -> FeedEpisode:
    return FeedEpisode(
        id=episode_id,
        title=title,
        source_url=f"https://example.com/{episode_id}",
        audio_url=f"https://cdn.example.com/{episode_id}.mp3",
        published_at=published_at,
    )


class CheckerTests(unittest.TestCase):
    def test_run_check_indexes_new_episodes_and_updates_subscription(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            store = _store(Path(tmpdir))
            store.upsert_subscription(_subscription())
            feed = _feed(
                _feed_episode("ep2", "第二期", "2026-06-22T00:00:00+00:00"),
                _feed_episode("ep1", "第一期", "2026-06-15T00:00:00+00:00"),
            )

            report = run_check(store, resolver=lambda _url: feed, now=lambda: "now")

            self.assertEqual(report.total_new, 2)
            self.assertEqual(report.failed_count, 0)
            self.assertEqual(store.get_episode("ep2").transcript_status, "pending")
            saved_subscription = store.load_subscriptions()["sub1"]
            self.assertEqual(saved_subscription.latest_episode_id, "ep2")
            self.assertEqual(saved_subscription.last_checked_at, "now")
            self.assertIsNone(saved_subscription.last_check_error)

    def test_run_check_is_idempotent(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            store = _store(Path(tmpdir))
            store.upsert_subscription(_subscription())
            feed = _feed(_feed_episode("ep1", "第一期", "2026-06-15T00:00:00+00:00"))

            first = run_check(store, resolver=lambda _url: feed, now=lambda: "now")
            second = run_check(store, resolver=lambda _url: feed, now=lambda: "now")

            self.assertEqual(first.total_new, 1)
            self.assertEqual(second.total_new, 0)
            self.assertEqual(len(store.load_episodes()), 1)

    def test_run_check_records_failures_without_stopping_other_subscriptions(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            store = _store(Path(tmpdir))
            store.upsert_subscription(_subscription())

            def resolver(_url: str) -> ParsedFeed:
                raise ValueError("network down")

            report = run_check(store, resolver=resolver, now=lambda: "now")

            self.assertEqual(report.total_new, 0)
            self.assertEqual(report.failed_count, 1)
            self.assertIn("network down", report.results[0].error or "")
            saved_subscription = store.load_subscriptions()["sub1"]
            self.assertEqual(saved_subscription.last_check_error, "network down")
            self.assertEqual(saved_subscription.last_checked_at, "now")

    def test_render_check_report_includes_next_commands(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            store = _store(Path(tmpdir))
            store.upsert_subscription(_subscription())
            feed = _feed(_feed_episode("ep1", "第一期", "2026-06-15T00:00:00+00:00"))

            report = run_check(store, resolver=lambda _url: feed, now=lambda: "now")
            rendered = render_check_report(report)

            self.assertIn("New episodes: 1", rendered)
            self.assertIn("python -m podcast_tracker transcribe-auto ep1", rendered)
            self.assertIn("python -m podcast_tracker episodes --pending", rendered)

    def test_check_preserves_existing_transcript_state(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            store = _store(Path(tmpdir))
            store.upsert_subscription(_subscription())
            store.upsert_episode(
                Episode(
                    id="ep1",
                    subscription_id="sub1",
                    program_title="测试节目",
                    title="旧标题",
                    source_url="https://example.com/ep1",
                    audio_url="https://cdn.example.com/ep1.mp3",
                    published_at="2026-06-15T00:00:00+00:00",
                    created_at="2026-06-15T00:00:00+00:00",
                    transcript_status="transcribed",
                    transcript_path="/tmp/ep1.md",
                )
            )
            feed = _feed(_feed_episode("ep1", "新标题", "2026-06-15T00:00:00+00:00"))

            report = run_check(store, resolver=lambda _url: feed, now=lambda: "now")
            saved = store.get_episode("ep1")

            self.assertEqual(report.total_new, 0)
            self.assertEqual(saved.title, "新标题")
            self.assertEqual(saved.transcript_status, "transcribed")
            self.assertEqual(saved.transcript_path, "/tmp/ep1.md")


if __name__ == "__main__":
    unittest.main()
