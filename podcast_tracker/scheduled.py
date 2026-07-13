from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from .checker import CheckReport, FeedResolver, render_check_report, run_check
from .jobs import ReadableJob, ReadableJobManager
from .lark_export import LarkExport, LarkExporter
from .models import Episode
from .publishing import PostGeneratePublisher
from .readable import readable_path_for_episode
from .store import Store


@dataclass(frozen=True)
class ScheduledEpisodeResult:
    episode: Episode
    job: ReadableJob
    lark_export: LarkExport | None = None
    lark_error: str | None = None
    retried: bool = False

    @property
    def generation_ok(self) -> bool:
        return self.job.status == "succeeded"

    @property
    def ok(self) -> bool:
        return self.generation_ok and self.lark_error is None

    def to_dict(self) -> dict:
        readable_path = readable_path_for_episode(self.episode)
        return {
            "episode_id": self.episode.id,
            "program_title": self.episode.program_title,
            "title": self.episode.title,
            "source_url": self.episode.source_url,
            "job": self.job.to_dict(),
            "readable_path": str(readable_path) if readable_path else None,
            "lark_export": self.lark_export.to_dict() if self.lark_export else None,
            "lark_error": self.lark_error,
            "retried": self.retried,
        }


@dataclass(frozen=True)
class ScheduledUpdateReport:
    check_report: CheckReport
    generated: tuple[ScheduledEpisodeResult, ...]

    @property
    def attempted_count(self) -> int:
        return len(self.generated)

    @property
    def succeeded_count(self) -> int:
        return sum(1 for result in self.generated if result.generation_ok)

    @property
    def generation_failed_count(self) -> int:
        return sum(1 for result in self.generated if not result.generation_ok)

    @property
    def lark_attempted_count(self) -> int:
        return sum(
            1
            for result in self.generated
            if result.generation_ok and (result.lark_export is not None or result.lark_error is not None)
        )

    @property
    def lark_succeeded_count(self) -> int:
        return sum(1 for result in self.generated if result.lark_export is not None)

    @property
    def lark_failed_count(self) -> int:
        return sum(1 for result in self.generated if result.lark_error is not None)

    @property
    def failed_count(self) -> int:
        return self.check_report.failed_count + self.generation_failed_count + self.lark_failed_count

    def to_dict(self) -> dict:
        return {
            "check": self.check_report.to_dict(),
            "generation": {
                "attempted_count": self.attempted_count,
                "succeeded_count": self.succeeded_count,
                "failed_count": self.generation_failed_count,
                "episodes": [result.to_dict() for result in self.generated],
            },
            "feishu": {
                "attempted_count": self.lark_attempted_count,
                "succeeded_count": self.lark_succeeded_count,
                "failed_count": self.lark_failed_count,
            },
            "failed_count": self.failed_count,
        }


def _process_episode(
    store: Store,
    manager: Any,
    publisher: PostGeneratePublisher,
    episode_id: str,
    retried: bool = False,
) -> ScheduledEpisodeResult:
    job = manager.start_readable_job(episode_id)
    stored_episode = store.get_episode(episode_id)
    lark_export: LarkExport | None = None
    lark_error: str | None = None

    if job.status == "succeeded":
        publish_result = publisher.publish_episode(stored_episode)
        lark_export = publish_result.lark_export
        lark_error = publish_result.lark_error

    return ScheduledEpisodeResult(
        episode=stored_episode,
        job=job,
        lark_export=lark_export,
        lark_error=lark_error,
        retried=retried,
    )


def _failed_episode_ids_to_retry(
    store: Store,
    manager: Any,
    already_processed: set[str],
) -> list[str]:
    """Episodes whose latest job failed and which still have no readable document.

    Only episodes that were attempted before are retried — historical episodes the
    user never generated are left alone.
    """
    latest_by_episode = getattr(manager, "latest_by_episode", None)
    if latest_by_episode is None:
        return []
    candidates: list[str] = []
    for episode_id, job in latest_by_episode().items():
        if episode_id in already_processed or job.status != "failed":
            continue
        try:
            episode = store.get_episode(episode_id)
        except KeyError:
            continue
        if readable_path_for_episode(episode) is not None:
            continue
        candidates.append(episode_id)
    return sorted(candidates)


def run_scheduled_update(
    store: Store,
    resolver: FeedResolver | None = None,
    job_manager: Any | None = None,
    lark_exporter: LarkExporter | None = None,
    publisher: PostGeneratePublisher | None = None,
) -> ScheduledUpdateReport:
    check_report = run_check(store, resolver=resolver) if resolver else run_check(store)
    manager = job_manager or ReadableJobManager(store, run_async=False)
    post_generate_publisher = publisher or PostGeneratePublisher(lark_exporter)
    generated: list[ScheduledEpisodeResult] = []

    for result in check_report.results:
        for episode in result.new_episodes:
            generated.append(_process_episode(store, manager, post_generate_publisher, episode.id))

    # Self-healing: retry episodes whose previous generation failed.
    processed_ids = {result.episode.id for result in generated}
    for episode_id in _failed_episode_ids_to_retry(store, manager, processed_ids):
        generated.append(
            _process_episode(store, manager, post_generate_publisher, episode_id, retried=True)
        )

    return ScheduledUpdateReport(check_report=check_report, generated=tuple(generated))


def render_scheduled_update_report(report: ScheduledUpdateReport) -> str:
    lines = [
        "Scheduled podcast update finished.",
        "",
        render_check_report(report.check_report),
        "",
        f"Readable generation attempted: {report.attempted_count}",
        f"Readable generation succeeded: {report.succeeded_count}",
        f"Feishu link sent: {report.lark_succeeded_count}",
    ]
    if report.generation_failed_count:
        lines.append(f"Readable generation failed: {report.generation_failed_count}")
    if report.lark_failed_count:
        lines.append(f"Feishu link failed: {report.lark_failed_count}")

    for result in report.generated:
        status = "ok" if result.ok else "failed"
        lines.append("")
        retry_marker = "（自动补跑）" if result.retried else ""
        lines.append(f"{result.episode.program_title} | {result.episode.title}{retry_marker}")
        lines.append(f"  Episode: {result.episode.id}")
        lines.append(f"  Status: {status}")
        if result.job.error:
            lines.append(f"  Error: {result.job.error}")
        readable_path = readable_path_for_episode(result.episode)
        if readable_path:
            lines.append(f"  Readable: {readable_path}")
        if result.lark_export:
            lines.append(f"  Feishu document: {result.lark_export.lark_doc_url}")
        if result.lark_error:
            lines.append(f"  Feishu error: {result.lark_error}")
        lines.append(f"  Source: {result.episode.source_url}")

    return "\n".join(lines)
