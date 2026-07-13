from __future__ import annotations

from dataclasses import dataclass
from typing import Callable

from .feed import ParsedFeed, discover_feed, parse_feed, to_episode, to_subscription
from .models import Episode, Subscription, utc_now_iso
from .store import Store
from .xiaoyuzhou import (
    is_episode_url,
    is_xiaoyuzhou_url,
    parse_podcast_as_feed,
    podcast_url_from_episode_url,
)

FeedResolver = Callable[[str], ParsedFeed]
Clock = Callable[[], str]


@dataclass(frozen=True)
class SubscriptionCheckResult:
    subscription: Subscription
    checked_at: str
    latest_episode_id: str | None
    new_episodes: tuple[Episode, ...] = ()
    error: str | None = None

    @property
    def ok(self) -> bool:
        return self.error is None

    def to_dict(self) -> dict:
        return {
            "subscription_id": self.subscription.id,
            "title": self.subscription.title,
            "feed_url": self.subscription.feed_url,
            "checked_at": self.checked_at,
            "latest_episode_id": self.latest_episode_id,
            "status": "ok" if self.ok else "failed",
            "error": self.error,
            "new_episodes": [_episode_report_item(episode) for episode in self.new_episodes],
        }


@dataclass(frozen=True)
class CheckReport:
    started_at: str
    finished_at: str
    results: tuple[SubscriptionCheckResult, ...]

    @property
    def subscription_count(self) -> int:
        return len(self.results)

    @property
    def total_new(self) -> int:
        return sum(len(result.new_episodes) for result in self.results)

    @property
    def failed_count(self) -> int:
        return sum(1 for result in self.results if not result.ok)

    def to_dict(self) -> dict:
        return {
            "started_at": self.started_at,
            "finished_at": self.finished_at,
            "subscription_count": self.subscription_count,
            "new_episode_count": self.total_new,
            "failed_count": self.failed_count,
            "results": [result.to_dict() for result in self.results],
        }


def resolve_feed(url: str) -> ParsedFeed:
    if is_xiaoyuzhou_url(url):
        if is_episode_url(url):
            url = podcast_url_from_episode_url(url)
        return parse_podcast_as_feed(url)
    feed_url, xml_text = discover_feed(url)
    return parse_feed(feed_url, url, xml_text)


def run_check(
    store: Store,
    resolver: FeedResolver = resolve_feed,
    now: Clock = utc_now_iso,
) -> CheckReport:
    started_at = now()
    results: list[SubscriptionCheckResult] = []
    subscriptions = store.load_subscriptions()

    for subscription in subscriptions.values():
        checked_at = now()
        try:
            feed = resolver(subscription.feed_url)
        except Exception as exc:
            error = str(exc)
            store.mark_subscription_checked(
                subscription.id,
                latest_episode_id=subscription.latest_episode_id,
                checked_at=checked_at,
                error=error,
            )
            results.append(
                SubscriptionCheckResult(
                    subscription=subscription,
                    checked_at=checked_at,
                    latest_episode_id=subscription.latest_episode_id,
                    error=error,
                )
            )
            continue

        store.upsert_subscription(to_subscription(feed))
        new_episodes: list[Episode] = []
        for item in feed.episodes:
            episode = to_episode(feed, item)
            if store.upsert_episode(episode):
                new_episodes.append(episode)

        latest_episode_id = feed.episodes[0].id if feed.episodes else subscription.latest_episode_id
        store.mark_subscription_checked(
            feed.id,
            latest_episode_id=latest_episode_id,
            checked_at=checked_at,
            error=None,
        )
        refreshed_subscription = store.load_subscriptions()[feed.id]
        results.append(
            SubscriptionCheckResult(
                subscription=refreshed_subscription,
                checked_at=checked_at,
                latest_episode_id=latest_episode_id,
                new_episodes=tuple(new_episodes),
            )
        )

    return CheckReport(
        started_at=started_at,
        finished_at=now(),
        results=tuple(results),
    )


def render_check_report(report: CheckReport) -> str:
    if report.subscription_count == 0:
        return "No subscriptions yet.\nAdd one with:\n  python -m podcast_tracker add <podcast_url_or_rss>"

    lines = [
        f"Check finished: {report.finished_at}",
        f"Subscriptions checked: {report.subscription_count}",
        f"New episodes: {report.total_new}",
    ]
    if report.failed_count:
        lines.append(f"Failed subscriptions: {report.failed_count}")

    for result in report.results:
        lines.append("")
        if result.error:
            lines.append(f"{result.subscription.title}: check failed")
            lines.append(f"  Error: {result.error}")
            continue

        lines.append(f"{result.subscription.title}: {len(result.new_episodes)} new")
        for episode in result.new_episodes:
            lines.extend(_render_episode_lines(episode))

    if report.total_new:
        lines.extend(
            [
                "",
                "Pending queue:",
                "  python -m podcast_tracker episodes --pending",
            ]
        )
    return "\n".join(lines)


def _render_episode_lines(episode: Episode) -> list[str]:
    published_at = episode.published_at or "unknown date"
    return [
        f"  - {published_at} | {episode.id} | {episode.title}",
        f"    Link: {episode.source_url}",
        f"    Status: {episode.transcript_status}",
        f"    Transcribe: python -m podcast_tracker transcribe-auto {episode.id}",
        f"    Readable after transcription: python -m podcast_tracker readable {episode.id}",
    ]


def _episode_report_item(episode: Episode) -> dict:
    item = episode.to_dict()
    item["transcribe_command"] = f"python -m podcast_tracker transcribe-auto {episode.id}"
    item["readable_command"] = f"python -m podcast_tracker readable {episode.id}"
    return item
