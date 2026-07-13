from __future__ import annotations

import argparse
import json
import shlex
import sys
import time
from pathlib import Path

from .config import READABLE_SKILL_DIR
from .checker import render_check_report, run_check
from .feed import (
    stable_id,
    to_episode,
    to_subscription,
)
from .models import Episode, utc_now_iso
from .readable import speaker_map_path_for_readable
from .store import Store
from .transcript import load_plain_text, load_segments, write_transcript_markdown
from .xiaoyuzhou import (
    parse_episode_record,
)

DEFAULT_BACKEND = "funasr"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="podcast-tracker")
    subparsers = parser.add_subparsers(dest="command", required=True)

    add_parser = subparsers.add_parser(
        "add", help="Subscribe to an RSS/Atom feed, page URL, or 小宇宙 podcast link"
    )
    add_parser.add_argument("url")

    check_parser = subparsers.add_parser(
        "check", help="Check all subscriptions for new episodes"
    )
    check_parser.add_argument(
        "--json",
        action="store_true",
        help="Print a machine-readable report for scheduled jobs",
    )

    scheduled_parser = subparsers.add_parser(
        "scheduled-update",
        help="Check subscriptions and generate readable transcripts for newly found episodes",
    )
    scheduled_parser.add_argument(
        "--json",
        action="store_true",
        help="Print a machine-readable report for automation logs",
    )

    subparsers.add_parser("subscriptions", help="List saved subscriptions")

    web_parser = subparsers.add_parser("web", help="Start the local web manager")
    web_parser.add_argument("--host", default="127.0.0.1")
    web_parser.add_argument("--port", type=int, default=8765)

    episodes_parser = subparsers.add_parser("episodes", help="List saved episodes")
    episodes_parser.add_argument(
        "--pending", action="store_true", help="Only show pending transcripts"
    )

    episode_add_parser = subparsers.add_parser(
        "episode-add", help="Add a historical episode manually"
    )
    episode_add_parser.add_argument("--program", required=True)
    episode_add_parser.add_argument("--title", required=True)
    episode_add_parser.add_argument("--url", required=True)
    episode_add_parser.add_argument("--audio-url")
    episode_add_parser.add_argument("--published-at")

    from_url_parser = subparsers.add_parser(
        "episode-from-url",
        help="Resolve a 小宇宙 single-episode link to an episode (auto audio URL)",
    )
    from_url_parser.add_argument("url")

    transcript_parser = subparsers.add_parser(
        "transcript", help="Save a verbatim Markdown transcript from JSON/text"
    )
    transcript_parser.add_argument("episode_id")
    transcript_input = transcript_parser.add_mutually_exclusive_group(required=True)
    transcript_input.add_argument(
        "--segments", type=Path, help="JSON list of speaker segments"
    )
    transcript_input.add_argument("--text", type=Path, help="Plain text transcript file")
    transcript_parser.add_argument("--speaker", default="Speaker 1")

    auto_parser = subparsers.add_parser(
        "transcribe-auto",
        help="Download episode audio, transcribe locally, and save verbatim Markdown",
    )
    auto_parser.add_argument("episode_id")
    auto_parser.add_argument(
        "--backend",
        choices=["funasr", "openai", "whisper"],
        default=DEFAULT_BACKEND,
        help="ASR backend (default: funasr — local, free, diarized)",
    )
    auto_parser.add_argument("--dry-run", action="store_true", help="Check setup only")
    auto_parser.add_argument("--work-dir", type=Path, help="Directory for audio/raw files")
    auto_parser.add_argument(
        "--progress-file",
        type=Path,
        help=argparse.SUPPRESS,
    )
    # FunASR options
    auto_parser.add_argument(
        "--asr-model", default="paraformer-zh", help="FunASR ASR model (funasr backend)"
    )
    auto_parser.add_argument(
        "--device", default="cpu", help="FunASR/Whisper device, e.g. cpu or cuda"
    )
    auto_parser.add_argument(
        "--hub", default="ms", choices=["ms", "hf"], help="FunASR model hub"
    )
    auto_parser.add_argument(
        "--speakers",
        help="Comma-separated speaker names mapped to spk 0,1,... e.g. 主持人,嘉宾",
    )
    auto_parser.add_argument(
        "--no-hotwords",
        action="store_true",
        help="Disable FunASR glossary hotwords for this run.",
    )
    # OpenAI / Whisper shared
    auto_parser.add_argument("--language", default="zh", help="Language hint")
    auto_parser.add_argument(
        "--chunk-seconds", type=int, help="Seconds per chunk for OpenAI 25MB limit"
    )
    auto_parser.add_argument(
        "--model-size", default="large-v3", help="Whisper model size (whisper backend)"
    )

    readable_parser = subparsers.add_parser(
        "readable",
        help="Print source/target paths and the verification command for a readable transcript",
    )
    readable_parser.add_argument("episode_id")

    args = parser.parse_args(argv)
    store = Store()
    store.ensure()

    handlers = {
        "add": lambda: _add_subscription(store, args.url),
        "check": lambda: _check(store, json_output=args.json),
        "scheduled-update": lambda: _scheduled_update(store, json_output=args.json),
        "subscriptions": lambda: _list_subscriptions(store),
        "episodes": lambda: _list_episodes(store, pending_only=args.pending),
        "episode-add": lambda: _episode_add(store, args),
        "episode-from-url": lambda: _episode_from_url(store, args.url),
        "transcript": lambda: _transcript(store, args),
        "transcribe-auto": lambda: _transcribe_auto(store, args),
        "readable": lambda: _readable(store, args),
        "web": lambda: _web(args),
    }
    handler = handlers.get(args.command)
    if handler is None:
        return 1
    try:
        return handler()
    except KeyError as exc:
        message = exc.args[0] if exc.args else str(exc)
        print(f"Error: {message}", file=sys.stderr)
        return 1


def _add_subscription(store: Store, url: str) -> int:
    from .checker import resolve_feed

    feed = resolve_feed(url)
    subscription = to_subscription(feed)
    store.upsert_subscription(subscription)

    new_count = sum(
        1
        for item in feed.episodes
        if store.upsert_episode(to_episode(feed, item))
    )
    print(f"Subscribed: {feed.title}")
    print(f"Feed: {feed.feed_url}")
    print(f"Episodes indexed: {len(feed.episodes)} ({new_count} new)")
    return 0


def _check(store: Store, json_output: bool = False) -> int:
    report = run_check(store)
    if json_output:
        print(json.dumps(report.to_dict(), ensure_ascii=False, indent=2))
    else:
        print(render_check_report(report))
    return 1 if report.failed_count else 0


def _scheduled_update(store: Store, json_output: bool = False) -> int:
    from .scheduled import render_scheduled_update_report, run_scheduled_update

    report = run_scheduled_update(store)
    if json_output:
        print(json.dumps(report.to_dict(), ensure_ascii=False, indent=2))
    else:
        print(render_scheduled_update_report(report))
    return 1 if report.failed_count else 0


def _list_subscriptions(store: Store) -> int:
    subscriptions = store.load_subscriptions()
    if not subscriptions:
        print("No subscriptions yet.")
        return 0
    for subscription in subscriptions.values():
        print(
            "\t".join(
                [
                    subscription.id,
                    subscription.title,
                    subscription.last_checked_at or "",
                    subscription.latest_episode_id or "",
                    subscription.feed_url,
                ]
            )
        )
    return 0


def _web(args: argparse.Namespace) -> int:
    from .web import run_server

    run_server(host=args.host, port=args.port)
    return 0


def _list_episodes(store: Store, pending_only: bool) -> int:
    episodes = store.load_episodes()
    for episode in episodes.values():
        if pending_only and episode.transcript_status != "pending":
            continue
        print(
            "\t".join(
                [
                    episode.id,
                    episode.transcript_status,
                    episode.program_title,
                    episode.title,
                    episode.published_at or "",
                ]
            )
        )
    return 0


def _episode_add(store: Store, args: argparse.Namespace) -> int:
    subscription_id = stable_id("manual", args.program)
    episode = Episode(
        id=stable_id("manual", args.program, args.url, args.title),
        subscription_id=subscription_id,
        program_title=args.program,
        title=args.title,
        source_url=args.url,
        audio_url=args.audio_url,
        published_at=args.published_at,
        created_at=utc_now_iso(),
    )
    is_new = store.upsert_episode(episode)
    print(f"{'Added' if is_new else 'Updated'} episode: {episode.id}")
    return 0


def _episode_from_url(store: Store, url: str) -> int:
    if not is_xiaoyuzhou_url(url):
        print("episode-from-url currently supports 小宇宙 (xiaoyuzhoufm.com) links only.")
        return 2
    try:
        episode = parse_episode_record(url)
    except Exception as exc:
        print(f"Error: {exc}")
        return 2
    is_new = store.upsert_episode(episode)
    print(f"{'Added' if is_new else 'Updated'} episode: {episode.id}")
    print(f"Title: {episode.title}")
    print(f"Audio: {episode.audio_url}")
    print("Transcribe with:")
    print(f"  python -m podcast_tracker transcribe-auto {episode.id}")
    return 0


def _transcript(store: Store, args: argparse.Namespace) -> int:
    episode = store.get_episode(args.episode_id)
    segments = (
        load_segments(args.segments)
        if args.segments
        else load_plain_text(args.text, args.speaker)
    )
    output_path = write_transcript_markdown(episode, segments)
    store.mark_transcribed(episode.id, output_path)
    print(f"Transcript saved: {output_path}")
    return 0


def _transcribe_auto(store: Store, args: argparse.Namespace) -> int:
    episode = store.get_episode(args.episode_id)
    if args.backend == "funasr":
        return _run_funasr(store, episode, args)
    if args.backend == "whisper":
        return _run_whisper(store, episode, args)
    return _run_openai(store, episode, args)


def _readable(store: Store, args: argparse.Namespace) -> int:
    episode = store.get_episode(args.episode_id)
    if not episode.transcript_path:
        print(f"Episode has no transcript yet: {episode.id}")
        return 2

    transcript_path = Path(episode.transcript_path)
    readable_path = transcript_path.with_name(f"{transcript_path.stem}-阅读版{transcript_path.suffix}")
    project_dir = Path(__file__).resolve().parents[1]
    applier = project_dir / "scripts" / "apply_glossary.py"
    verifier = project_dir / "scripts" / "verify_readable.py"
    candidate_path = readable_path.with_name(readable_path.stem + ".glossary-candidates.json")

    print(f"Transcript: {transcript_path}")
    print(f"Readable target: {readable_path}")
    print(f"Skill: {READABLE_SKILL_DIR / 'SKILL.md'}")
    speaker_map_path = speaker_map_path_for_readable(readable_path)
    print("Apply glossary command:")
    print(
        "  python "
        f"{shlex.quote(str(applier))} "
        f"{shlex.quote(str(readable_path))}"
    )
    print("Verify command:")
    print(
        "  python "
        f"{shlex.quote(str(verifier))} "
        f"{shlex.quote(str(transcript_path))} "
        f"{shlex.quote(str(readable_path))}"
        " --strict "
        "--emit-glossary-candidates "
        f"{shlex.quote(str(candidate_path))}"
        + (
            " --speaker-map-file "
            f"{shlex.quote(str(speaker_map_path))}"
            if speaker_map_path.exists()
            else ""
        )
    )
    return 0


def _run_funasr(store: Store, episode: Episode, args: argparse.Namespace) -> int:
    from .asr_funasr import FunasrOptions, dry_run_report, transcribe_episode_funasr

    kwargs: dict = {
        "asr_model": args.asr_model,
        "device": args.device,
        "model_hub": args.hub,
        "speaker_names": tuple(s.strip() for s in args.speakers.split(",") if s.strip())
        if args.speakers
        else (),
        "use_hotwords": not args.no_hotwords,
    }
    if args.work_dir:
        kwargs["work_dir"] = args.work_dir
    options = FunasrOptions(**kwargs)

    if args.dry_run:
        print("\n".join(dry_run_report(episode, options)))
        return 0
    progress_file: Path | None = args.progress_file

    def report_progress(stage: str) -> None:
        if progress_file is None:
            return
        progress_file.parent.mkdir(parents=True, exist_ok=True)
        progress_file.write_text(
            json.dumps(
                {
                    "stage": stage,
                    "updated_at": time.time(),
                    "episode_id": episode.id,
                },
                ensure_ascii=False,
            )
            + "\n",
            encoding="utf-8",
        )

    try:
        result = transcribe_episode_funasr(episode, options, progress_callback=report_progress)
    except RuntimeError as exc:
        print(f"Error: {exc}")
        return 2
    store.mark_transcribed(episode.id, result.transcript_path)
    print(f"Transcript saved: {result.transcript_path}")
    print(
        f"Backend: funasr | segments: {result.segment_count} | "
        f"speakers: {result.speaker_count}"
    )
    print(f"Raw output: {result.raw_output}")
    return 0


def _run_whisper(store: Store, episode: Episode, args: argparse.Namespace) -> int:
    from .asr_whisper import WhisperOptions, dry_run_report, transcribe_episode_whisper

    kwargs: dict = {
        "model_size": args.model_size,
        "device": args.device,
        "language": args.language,
    }
    if args.work_dir:
        kwargs["work_dir"] = args.work_dir
    options = WhisperOptions(**kwargs)

    if args.dry_run:
        print("\n".join(dry_run_report(episode, options)))
        return 0
    try:
        result = transcribe_episode_whisper(episode, options)
    except RuntimeError as exc:
        print(f"Error: {exc}")
        return 2
    store.mark_transcribed(episode.id, result.transcript_path)
    print(f"Transcript saved: {result.transcript_path}")
    print(f"Backend: whisper | segments: {result.segment_count} (no diarization)")
    return 0


def _run_openai(store: Store, episode: Episode, args: argparse.Namespace) -> int:
    from .asr import (
        DEFAULT_CHUNK_SECONDS,
        AutoTranscribeOptions,
        dry_run_report,
        transcribe_episode_auto,
    )

    kwargs: dict = {
        "language": args.language,
        "chunk_seconds": args.chunk_seconds or DEFAULT_CHUNK_SECONDS,
    }
    if args.work_dir:
        kwargs["work_dir"] = args.work_dir
    options = AutoTranscribeOptions(**kwargs)

    if args.dry_run:
        print("\n".join(dry_run_report(episode, options)))
        return 0
    try:
        result = transcribe_episode_auto(episode, options)
    except RuntimeError as exc:
        print(f"Error: {exc}")
        return 2
    store.mark_transcribed(episode.id, result.transcript_path)
    print(f"Transcript saved: {result.transcript_path}")
    print(f"Backend: openai | segments: {result.segment_count}")
    print(f"Raw ASR files: {result.work_dir}")
    return 0
