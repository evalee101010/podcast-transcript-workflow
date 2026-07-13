from __future__ import annotations

import fcntl
import json
from contextlib import contextmanager
from dataclasses import replace
from pathlib import Path

from .config import DATA_DIR, EPISODES_FILE, SUBSCRIPTIONS_FILE
from .models import Episode, Subscription


class Store:
    def __init__(
        self,
        data_dir: Path = DATA_DIR,
        subscriptions_file: Path = SUBSCRIPTIONS_FILE,
        episodes_file: Path = EPISODES_FILE,
    ) -> None:
        self.data_dir = data_dir
        self.subscriptions_file = subscriptions_file
        self.episodes_file = episodes_file

    def ensure(self) -> None:
        self.data_dir.mkdir(parents=True, exist_ok=True)
        if not self.subscriptions_file.exists():
            self._write_json(self.subscriptions_file, [])
        if not self.episodes_file.exists():
            self._write_json(self.episodes_file, [])

    @contextmanager
    def _rmw_lock(self):
        """Cross-process lock for read-modify-write cycles.

        episodes.json is written by both the web server process and the
        transcribe-auto subprocess; without this, concurrent read-modify-write
        cycles silently lose updates.
        """
        self.data_dir.mkdir(parents=True, exist_ok=True)
        lock_path = self.data_dir / ".store.lock"
        with lock_path.open("w") as handle:
            fcntl.flock(handle, fcntl.LOCK_EX)
            try:
                yield
            finally:
                fcntl.flock(handle, fcntl.LOCK_UN)

    def load_subscriptions(self) -> dict[str, Subscription]:
        self.ensure()
        rows = self._read_json(self.subscriptions_file)
        return {row["id"]: Subscription.from_dict(row) for row in rows}

    def save_subscriptions(self, subscriptions: dict[str, Subscription]) -> None:
        rows = [item.to_dict() for item in subscriptions.values()]
        rows.sort(key=lambda item: item["title"].lower())
        self._write_json(self.subscriptions_file, rows)

    def upsert_subscription(self, subscription: Subscription) -> None:
        with self._rmw_lock():
            subscriptions = self.load_subscriptions()
            existing = subscriptions.get(subscription.id)
            if existing:
                subscription = replace(
                    subscription,
                    created_at=existing.created_at,
                    latest_episode_id=existing.latest_episode_id,
                    last_checked_at=existing.last_checked_at,
                    last_check_error=existing.last_check_error,
                    avatar_url=subscription.avatar_url or existing.avatar_url,
                )
            subscriptions[subscription.id] = subscription
            self.save_subscriptions(subscriptions)

    def set_latest_episode(self, subscription_id: str, episode_id: str) -> None:
        with self._rmw_lock():
            subscriptions = self.load_subscriptions()
            if subscription_id not in subscriptions:
                return
            subscriptions[subscription_id] = replace(
                subscriptions[subscription_id],
                latest_episode_id=episode_id,
            )
            self.save_subscriptions(subscriptions)

    def mark_subscription_checked(
        self,
        subscription_id: str,
        latest_episode_id: str | None,
        checked_at: str,
        error: str | None = None,
    ) -> Subscription | None:
        with self._rmw_lock():
            subscriptions = self.load_subscriptions()
            if subscription_id not in subscriptions:
                return None
            updated = replace(
                subscriptions[subscription_id],
                latest_episode_id=latest_episode_id,
                last_checked_at=checked_at,
                last_check_error=error,
            )
            subscriptions[subscription_id] = updated
            self.save_subscriptions(subscriptions)
            return updated

    def load_episodes(self) -> dict[str, Episode]:
        self.ensure()
        rows = self._read_json(self.episodes_file)
        return {row["id"]: Episode.from_dict(row) for row in rows}

    def save_episodes(self, episodes: dict[str, Episode]) -> None:
        rows = [item.to_dict() for item in episodes.values()]
        rows.sort(key=lambda item: (item["published_at"] or "", item["title"]), reverse=True)
        self._write_json(self.episodes_file, rows)

    def upsert_episode(self, episode: Episode) -> bool:
        with self._rmw_lock():
            episodes = self.load_episodes()
            existing_ids = self._matching_episode_ids(episodes, episode)
            is_new = not existing_ids
            if existing_ids:
                canonical_id = self._canonical_episode_id(episodes, existing_ids)
                merged = self._merge_episode(episodes[canonical_id], episode)
                for duplicate_id in existing_ids:
                    if duplicate_id == canonical_id:
                        continue
                    duplicate = episodes.pop(duplicate_id)
                    merged = self._merge_episode(merged, duplicate)
                episodes[canonical_id] = merged
            else:
                episodes[episode.id] = episode
            self.save_episodes(episodes)
            return is_new

    def get_episode(self, episode_id: str) -> Episode:
        episodes = self.load_episodes()
        try:
            return episodes[episode_id]
        except KeyError as exc:
            raise KeyError(f"Episode not found: {episode_id}") from exc

    def mark_transcribed(self, episode_id: str, transcript_path: Path) -> Episode:
        with self._rmw_lock():
            episodes = self.load_episodes()
            if episode_id not in episodes:
                raise KeyError(f"Episode not found: {episode_id}")
            updated = replace(
                episodes[episode_id],
                transcript_status="transcribed",
                transcript_path=str(transcript_path),
            )
            episodes[episode_id] = updated
            self.save_episodes(episodes)
            return updated

    @staticmethod
    def _matching_episode_ids(episodes: dict[str, Episode], episode: Episode) -> list[str]:
        return [
            episode_id
            for episode_id, existing in episodes.items()
            if episode_id == episode.id or existing.source_url == episode.source_url
        ]

    @staticmethod
    def _canonical_episode_id(episodes: dict[str, Episode], episode_ids: list[str]) -> str:
        def sort_key(episode_id: str) -> tuple[int, int, str, str]:
            episode = episodes[episode_id]
            has_transcript = bool(episode.transcript_path) or episode.transcript_status != "pending"
            return (
                0 if has_transcript else 1,
                0 if episode.transcript_path else 1,
                episode.created_at,
                episode_id,
            )

        return min(episode_ids, key=sort_key)

    @staticmethod
    def _merge_episode(existing: Episode, incoming: Episode) -> Episode:
        transcript_status = existing.transcript_status
        transcript_path = existing.transcript_path
        incoming_has_transcript = bool(incoming.transcript_path) or incoming.transcript_status != "pending"
        existing_has_transcript = bool(existing.transcript_path) or existing.transcript_status != "pending"
        if not existing_has_transcript and incoming_has_transcript:
            transcript_status = incoming.transcript_status
            transcript_path = incoming.transcript_path
        elif not transcript_path and incoming.transcript_path:
            transcript_path = incoming.transcript_path

        return replace(
            incoming,
            id=existing.id,
            created_at=existing.created_at,
            transcript_status=transcript_status,
            transcript_path=transcript_path,
        )

    @staticmethod
    def _read_json(path: Path) -> list[dict]:
        with path.open("r", encoding="utf-8") as file:
            return json.load(file)

    @staticmethod
    def _write_json(path: Path, data: list[dict]) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp_path = path.with_suffix(path.suffix + ".tmp")
        with tmp_path.open("w", encoding="utf-8") as file:
            json.dump(data, file, ensure_ascii=False, indent=2)
            file.write("\n")
        tmp_path.replace(path)
