from __future__ import annotations

import html
import json
import os
import re
import subprocess
import sys
import tempfile
import traceback
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import quote, unquote, urlparse

from .checker import render_check_report, resolve_feed, run_check
from .config import PROJECT_ROOT
from .document_patches import (
    PatchBaselineMismatch,
    append_approved_body_patch,
    hash_body_text,
    parse_readable_markdown,
    replace_rendered_block_body,
    render_readable_markdown,
)
from .feed import stable_id, to_episode, to_subscription
from .jobs import JOB_STATE_FILE, ReadableJobManager
from .lark_export import LarkExportStore, LarkExporter
from .models import Episode, Subscription, utc_now_iso
from .publishing import PostGeneratePublisher
from .readable import has_readable, readable_path_for_episode
from .store import Store


STATIC_DIR = PROJECT_ROOT / "web_static"


def run_server(host: str = "127.0.0.1", port: int = 8765) -> None:
    store = Store()
    store.ensure()
    export_store = LarkExportStore()
    lark_exporter = LarkExporter(export_store=export_store)
    completion_hook = (
        _make_lark_completion_hook(lark_exporter)
        if _lark_auto_publish_enabled()
        else None
    )
    job_manager = ReadableJobManager(
        store,
        state_path=JOB_STATE_FILE,
        completion_hook=completion_hook,
    )
    handler = _make_handler(store, job_manager, lark_exporter, export_store)
    server = ThreadingHTTPServer((host, port), handler)
    print(f"Podcast tracker web: http://{host}:{port}")
    server.serve_forever()


def _make_handler(
    store: Store,
    job_manager: ReadableJobManager | None = None,
    lark_exporter: LarkExporter | None = None,
    export_store: LarkExportStore | None = None,
):
    export_store = export_store or LarkExportStore()
    lark_exporter = lark_exporter or LarkExporter(export_store=export_store)
    completion_hook = (
        _make_lark_completion_hook(lark_exporter)
        if _lark_auto_publish_enabled()
        else None
    )
    job_manager = job_manager or ReadableJobManager(
        store,
        completion_hook=completion_hook,
    )

    class PodcastTrackerHandler(BaseHTTPRequestHandler):
        def do_HEAD(self) -> None:
            try:
                self._do_HEAD()
            except Exception as exc:
                self._send_uncaught_error(exc, body=False)

        def _do_HEAD(self) -> None:
            parsed = urlparse(self.path)
            if parsed.path in {"/", "/index.html"}:
                self._send_file(STATIC_DIR / "index.html", "text/html; charset=utf-8", body=False)
                return
            if parsed.path == "/glossary":
                payload = _render_glossary_page(store).encode("utf-8")
                self._send_bytes(payload, "text/html; charset=utf-8", body=False)
                return
            if parsed.path == "/api/state":
                self._send_json(_state_payload(store, job_manager), body=False)
                return
            if parsed.path == "/api/health":
                self._send_json(_health_payload(store), body=False)
                return
            if parsed.path.startswith("/docs/"):
                self._handle_document(parsed.path, body=False)
                return
            self.send_error(HTTPStatus.NOT_FOUND, "Not found")

        def do_GET(self) -> None:
            try:
                self._do_GET()
            except Exception as exc:
                self._send_uncaught_error(exc)

        def _do_GET(self) -> None:
            parsed = urlparse(self.path)
            if parsed.path in {"/", "/index.html"}:
                self._send_file(STATIC_DIR / "index.html", "text/html; charset=utf-8")
                return
            if parsed.path == "/glossary":
                payload = _render_glossary_page(store).encode("utf-8")
                self._send_bytes(payload, "text/html; charset=utf-8")
                return
            if parsed.path == "/api/state":
                self._send_json(_state_payload(store, job_manager))
                return
            if parsed.path == "/api/health":
                self._send_json(_health_payload(store))
                return
            if parsed.path.startswith("/docs/"):
                self._handle_document(parsed.path)
                return
            self.send_error(HTTPStatus.NOT_FOUND, "Not found")

        def do_POST(self) -> None:
            try:
                self._do_POST()
            except Exception as exc:
                self._send_uncaught_error(exc)

        def _do_POST(self) -> None:
            parsed = urlparse(self.path)
            if parsed.path == "/api/subscriptions":
                self._handle_add_subscription()
                return
            if parsed.path == "/api/check":
                report = run_check(store)
                self._send_json(
                    {
                        "report": report.to_dict(),
                        "text_report": render_check_report(report),
                        "state": _state_payload(store, job_manager),
                    }
                )
                return
            if parsed.path == "/api/jobs/readable":
                self._handle_start_readable_job()
                return
            if parsed.path == "/api/docs/lark":
                self._handle_lark_export()
                return
            if parsed.path == "/api/docs/patches":
                self._handle_document_patch()
                return
            if parsed.path == "/api/glossary/correction":
                self._handle_glossary_correction()
                return
            if parsed.path == "/api/glossary/delete":
                self._handle_glossary_delete()
                return
            self.send_error(HTTPStatus.NOT_FOUND, "Not found")

        def log_message(self, format: str, *args) -> None:
            return

        def _send_uncaught_error(self, exc: Exception, body: bool = True) -> None:
            traceback.print_exc()
            try:
                self._send_json(
                    {"error": f"服务内部错误: {exc}"},
                    status=HTTPStatus.INTERNAL_SERVER_ERROR,
                    body=body,
                )
            except Exception:
                return

        def _handle_add_subscription(self) -> None:
            payload = self._read_json()
            url = str(payload.get("url") or "").strip()
            if not url:
                self._send_json({"error": "URL is required"}, status=HTTPStatus.BAD_REQUEST)
                return
            try:
                feed = resolve_feed(url)
            except Exception as exc:
                self._send_json({"error": str(exc)}, status=HTTPStatus.BAD_REQUEST)
                return

            subscription = to_subscription(feed)
            store.upsert_subscription(subscription)
            new_count = 0
            for item in feed.episodes:
                if store.upsert_episode(to_episode(feed, item)):
                    new_count += 1

            self._send_json(
                {
                    "subscription": _subscription_item(
                        store.load_subscriptions()[feed.id],
                        store.load_episodes().values(),
                    ),
                    "indexed_episode_count": len(feed.episodes),
                    "new_episode_count": new_count,
                    "state": _state_payload(store, job_manager),
                },
                status=HTTPStatus.CREATED,
            )

        def _handle_start_readable_job(self) -> None:
            payload = self._read_json()
            episode_id = str(payload.get("episode_id") or "").strip()
            if not episode_id:
                self._send_json({"error": "episode_id is required"}, status=HTTPStatus.BAD_REQUEST)
                return
            force = bool(payload.get("force"))
            try:
                job = job_manager.start_readable_job(episode_id, force=force)
            except KeyError:
                self._send_json({"error": f"Episode not found: {episode_id}"}, status=HTTPStatus.NOT_FOUND)
                return
            except ValueError as exc:
                # e.g. force-regeneration refused because human patches exist
                self._send_json({"error": str(exc)}, status=HTTPStatus.CONFLICT)
                return
            except Exception as exc:
                self._send_json({"error": str(exc)}, status=HTTPStatus.BAD_REQUEST)
                return

            self._send_json(
                {
                    "job": job.to_dict(),
                    "state": _state_payload(store, job_manager),
                },
                status=HTTPStatus.ACCEPTED,
            )

        def _handle_lark_export(self) -> None:
            payload = self._read_json()
            episode_id = str(payload.get("episode_id") or "").strip()
            action = str(payload.get("action") or "export").strip()
            if not episode_id:
                self._send_json({"error": "episode_id is required"}, status=HTTPStatus.BAD_REQUEST)
                return
            try:
                episode = store.get_episode(episode_id)
            except KeyError:
                self._send_json({"error": f"Episode not found: {episode_id}"}, status=HTTPStatus.NOT_FOUND)
                return

            readable_path = readable_path_for_episode(episode)
            if readable_path is None:
                self._send_json({"error": "Readable document not found"}, status=HTTPStatus.NOT_FOUND)
                return

            try:
                if action == "sync":
                    export = lark_exporter.sync_readable(episode, readable_path)
                else:
                    export = lark_exporter.export_readable(episode, readable_path)
            except Exception as exc:
                self._send_json({"error": str(exc)}, status=HTTPStatus.BAD_REQUEST)
                return

            self._send_json({"export": export.to_dict()})

        def _handle_document_patch(self) -> None:
            payload = self._read_json()
            episode_id = str(payload.get("episode_id") or "").strip()
            block_id = str(payload.get("block_id") or "").strip()
            before_hash = str(payload.get("before_hash") or "").strip()
            after_text = str(payload.get("after_text") or "")
            if not episode_id:
                self._send_json({"error": "episode_id is required"}, status=HTTPStatus.BAD_REQUEST)
                return
            if not block_id:
                self._send_json({"error": "block_id is required"}, status=HTTPStatus.BAD_REQUEST)
                return
            if not before_hash:
                self._send_json({"error": "before_hash is required"}, status=HTTPStatus.BAD_REQUEST)
                return
            try:
                episode = store.get_episode(episode_id)
            except KeyError:
                self._send_json({"error": f"Episode not found: {episode_id}"}, status=HTTPStatus.NOT_FOUND)
                return
            readable_path = readable_path_for_episode(episode)
            if readable_path is None:
                self._send_json({"error": "Readable document not found"}, status=HTTPStatus.NOT_FOUND)
                return

            try:
                result = _save_document_body_patch(
                    episode,
                    readable_path,
                    block_id=block_id,
                    before_hash=before_hash,
                    after_text=after_text,
                )
            except KeyError as exc:
                self._send_json({"error": str(exc)}, status=HTTPStatus.NOT_FOUND)
                return
            except ValueError as exc:
                self._send_json({"error": str(exc)}, status=HTTPStatus.CONFLICT)
                return
            except Exception as exc:
                self._send_json({"error": str(exc)}, status=HTTPStatus.BAD_REQUEST)
                return

            self._send_json({"ok": True, **result})

        def _handle_glossary_correction(self) -> None:
            payload = self._read_json()
            episode_id = str(payload.get("episode_id") or "").strip()
            action = str(payload.get("action") or "correct").strip()
            source_text = str(payload.get("source") or "").strip()
            target = str(payload.get("target") or "").strip()
            if not episode_id:
                self._send_json(
                    {"error": "episode_id is required"},
                    status=HTTPStatus.BAD_REQUEST,
                )
                return
            sources = _parse_glossary_sources(source_text)
            try:
                episode = store.get_episode(episode_id)
            except KeyError:
                self._send_json({"error": f"Episode not found: {episode_id}"}, status=HTTPStatus.NOT_FOUND)
                return
            readable_path = readable_path_for_episode(episode)
            if readable_path is None:
                self._send_json({"error": "Readable document not found"}, status=HTTPStatus.NOT_FOUND)
                return

            try:
                if action == "correct":
                    if not sources or not target:
                        raise ValueError("修正需要填写错词和正确词。")
                    glossary_update = _upsert_glossary_entries(sources, target)
                    apply_result = _run_apply_glossary(readable_path)
                elif action == "canonical":
                    terms = _parse_glossary_sources(target or source_text)
                    if not terms:
                        raise ValueError("请填写要加入热词库的正确词。")
                    glossary_update = _upsert_global_canonical(terms)
                    apply_result = {"exit_code": 0, "output": "No correction apply needed."}
                elif action == "episode_correct":
                    if not sources or not target:
                        raise ValueError("本集修改需要填写错词和正确词。")
                    glossary_update = _upsert_episode_correction(readable_path, sources, target)
                    apply_result = _run_apply_glossary(readable_path)
                elif action == "ignore":
                    if not sources:
                        raise ValueError("本集标记需要填写候选词。")
                    glossary_update = _upsert_episode_canonical(readable_path, sources)
                    apply_result = {"exit_code": 0, "output": "Ignored for this episode only."}
                else:
                    raise ValueError(f"Unsupported glossary action: {action}")
                verify_result = _run_verify_for_readable(episode, readable_path)
                candidates = _load_glossary_candidates(readable_path)
            except Exception as exc:
                self._send_json({"error": str(exc)}, status=HTTPStatus.BAD_REQUEST)
                return

            self._send_json(
                {
                    "ok": apply_result["exit_code"] == 0 and verify_result["exit_code"] == 0,
                    "glossary": glossary_update,
                    "apply": apply_result,
                    "verify": verify_result,
                    "candidates": candidates,
                }
            )

        def _handle_glossary_delete(self) -> None:
            payload = self._read_json()
            scope = str(payload.get("scope") or "").strip()
            item_type = str(payload.get("type") or "").strip()
            source = str(payload.get("source") or "").strip()
            target = str(payload.get("target") or "").strip()
            path_text = str(payload.get("path") or "").strip()
            try:
                glossary_path = _resolve_delete_glossary_path(scope, path_text)
                result = _delete_glossary_entry(glossary_path, item_type, source, target)
            except Exception as exc:
                self._send_json({"error": str(exc)}, status=HTTPStatus.BAD_REQUEST)
                return
            self._send_json({"ok": True, "deleted": result})

        def _read_json(self) -> dict:
            length = int(self.headers.get("content-length") or 0)
            if length <= 0:
                return {}
            body = self.rfile.read(length)
            try:
                return json.loads(body.decode("utf-8"))
            except json.JSONDecodeError:
                return {}

        def _handle_document(self, request_path: str, body: bool = True) -> None:
            parts = request_path.strip("/").split("/")
            if len(parts) != 3 or parts[0] != "docs" or parts[2] not in {"readable", "readable.md"}:
                self.send_error(HTTPStatus.NOT_FOUND, "Not found")
                return

            try:
                episode = store.get_episode(unquote(parts[1]))
            except KeyError:
                self.send_error(HTTPStatus.NOT_FOUND, "Not found")
                return

            readable_path = readable_path_for_episode(episode)
            if readable_path is None:
                self.send_error(HTTPStatus.NOT_FOUND, "Readable document not found")
                return

            try:
                if parts[2] == "readable.md":
                    filename = readable_path.name
                    payload = render_readable_markdown(readable_path).encode("utf-8")
                    self._send_bytes(
                        payload,
                        "text/markdown; charset=utf-8",
                        body=body,
                        headers={"Content-Disposition": f"attachment; filename*=UTF-8''{_quote_header_filename(filename)}"},
                    )
                    return

                export = export_store.get(episode.id)
                payload = _render_document_page(episode, readable_path, lark_doc_url=export.lark_doc_url if export else None).encode("utf-8")
            except PatchBaselineMismatch as exc:
                payload = _render_document_conflict_page(str(exc)).encode("utf-8")
                self._send_bytes(
                    payload,
                    "text/html; charset=utf-8",
                    status=HTTPStatus.CONFLICT,
                    body=body,
                )
                return
            self._send_bytes(payload, "text/html; charset=utf-8", body=body)

        def _send_json(
            self,
            data: dict,
            status: HTTPStatus = HTTPStatus.OK,
            body: bool = True,
        ) -> None:
            payload = json.dumps(data, ensure_ascii=False, indent=2).encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Cache-Control", "no-store")
            self.send_header("Content-Length", str(len(payload)))
            self.end_headers()
            if body:
                self.wfile.write(payload)

        def _send_bytes(
            self,
            payload: bytes,
            content_type: str,
            status: HTTPStatus = HTTPStatus.OK,
            body: bool = True,
            headers: dict[str, str] | None = None,
        ) -> None:
            self.send_response(status)
            self.send_header("Content-Type", content_type)
            self.send_header("Cache-Control", "no-store")
            self.send_header("Content-Length", str(len(payload)))
            for key, value in (headers or {}).items():
                self.send_header(key, value)
            self.end_headers()
            if body:
                self.wfile.write(payload)

        def _send_file(self, path: Path, content_type: str, body: bool = True) -> None:
            if not path.exists():
                self.send_error(HTTPStatus.NOT_FOUND, "Not found")
                return
            payload = path.read_bytes()
            self.send_response(HTTPStatus.OK)
            self.send_header("Content-Type", content_type)
            self.send_header("Cache-Control", "no-store")
            self.send_header("Content-Length", str(len(payload)))
            self.end_headers()
            if body:
                self.wfile.write(payload)

    return PodcastTrackerHandler


def _make_lark_completion_hook(lark_exporter: LarkExporter):
    publisher = PostGeneratePublisher(lark_exporter)

    def hook(episode: Episode, readable_path: Path, _log_path: Path) -> None:
        result = publisher.publish_readable(episode, readable_path)
        if result.lark_error:
            raise RuntimeError(result.lark_error)

    return hook


def _lark_auto_publish_enabled() -> bool:
    value = os.getenv("PODCAST_TRACKER_ENABLE_LARK", "")
    return value.strip().lower() in {"1", "true", "yes", "on"}


def _health_payload(store: Store) -> dict:
    subscriptions = store.load_subscriptions()
    episodes = store.load_episodes()
    return {
        "ok": True,
        "service": "podcast-tracker-web",
        "checked_at": utc_now_iso(),
        "counts": {
            "subscriptions": len(subscriptions),
            "episodes": len(episodes),
        },
    }


def _state_payload(store: Store, job_manager: ReadableJobManager | None = None) -> dict:
    subscriptions = store.load_subscriptions()
    episodes = store.load_episodes()
    episode_values = list(episodes.values())
    sources, episode_source_ids = _source_items(subscriptions, episode_values)
    jobs_by_episode = job_manager.latest_by_episode() if job_manager else {}
    episode_items = [
        _episode_item(
            episode,
            source_id=episode_source_ids.get(episode.id),
            job=jobs_by_episode.get(episode.id),
        )
        for episode in episode_values
    ]
    return {
        "subscriptions": sources,
        "episodes": episode_items,
        "counts": {
            "subscriptions": len(sources),
            "formal_subscriptions": len(subscriptions),
            "episodes": len(episodes),
            "pending": sum(1 for episode in episodes.values() if not has_readable(episode)),
            "readable": sum(1 for episode in episodes.values() if has_readable(episode)),
        },
    }


def _source_items(
    subscriptions: dict[str, Subscription],
    episodes: list[Episode],
) -> tuple[list[dict], dict[str, str]]:
    sources: list[dict] = []
    episode_source_ids: dict[str, str] = {}
    subscriptions_by_title = {subscription.title: subscription for subscription in subscriptions.values()}

    inferred: dict[str, list[Episode]] = {}
    for episode in episodes:
        if episode.subscription_id in subscriptions:
            episode_source_ids[episode.id] = episode.subscription_id
            continue
        title_match = subscriptions_by_title.get(episode.program_title)
        if title_match:
            episode_source_ids[episode.id] = title_match.id
            continue
        inferred_id = f"inferred:{stable_id('program', episode.program_title)}"
        episode_source_ids[episode.id] = inferred_id
        inferred.setdefault(inferred_id, []).append(episode)

    for subscription in subscriptions.values():
        related = [
            episode
            for episode in episodes
            if episode_source_ids.get(episode.id) == subscription.id
        ]
        sources.append(_subscription_item_from_related(subscription, related, inferred=False))

    for inferred_id, related in sorted(
        inferred.items(),
        key=lambda item: item[1][0].program_title.lower(),
    ):
        sources.append(_inferred_subscription_item(inferred_id, related))

    return sources, episode_source_ids


def _subscription_item(
    subscription: Subscription,
    episodes: list[Episode] | object,
    inferred: bool = False,
) -> dict:
    related = [
        episode
        for episode in episodes
        if isinstance(episode, Episode) and episode.subscription_id == subscription.id
    ]
    return _subscription_item_from_related(subscription, related, inferred=inferred)


def _subscription_item_from_related(
    subscription: Subscription,
    related: list[Episode],
    inferred: bool = False,
) -> dict:
    return {
        "id": subscription.id,
        "title": subscription.title,
        "feed_url": subscription.feed_url,
        "source_url": subscription.source_url,
        "created_at": subscription.created_at,
        "latest_episode_id": subscription.latest_episode_id,
        "last_checked_at": subscription.last_checked_at,
        "last_check_error": subscription.last_check_error,
        "avatar_url": subscription.avatar_url,
        "inferred": inferred,
        "episode_count": len(related),
        "pending_count": sum(1 for episode in related if not has_readable(episode)),
        "readable_count": sum(1 for episode in related if has_readable(episode)),
    }


def _inferred_subscription_item(source_id: str, episodes: list[Episode]) -> dict:
    title = episodes[0].program_title or "未识别播客"
    latest = max(episodes, key=lambda episode: episode.published_at or "")
    return {
        "id": source_id,
        "title": title,
        "feed_url": "",
        "source_url": latest.source_url,
        "created_at": min(episode.created_at for episode in episodes),
        "latest_episode_id": latest.id,
        "last_checked_at": None,
        "last_check_error": None,
        "avatar_url": None,
        "inferred": True,
        "episode_count": len(episodes),
        "pending_count": sum(1 for episode in episodes if not has_readable(episode)),
        "readable_count": sum(1 for episode in episodes if has_readable(episode)),
    }


def _episode_item(episode: Episode, source_id: str | None = None, job=None) -> dict:
    readable_path = readable_path_for_episode(episode)
    has_readable = readable_path is not None
    job_id = None
    job_status = None
    job_error = None
    job_stage = None
    job_stage_label = None
    job_progress = None
    job_stage_started_at = None
    if job and not has_readable:
        job_id = job.id
        job_status = job.status
        job_error = job.error
        job_stage = job.stage
        job_stage_label = job.stage_label
        job_progress = job.progress
        job_stage_started_at = job.stage_started_at

    display_status = "readable" if has_readable else "pending"
    if job_status in {"queued", "running"}:
        display_status = "generating"
    elif job_status == "failed":
        display_status = "failed"

    item = {
        "id": episode.id,
        "subscription_id": episode.subscription_id,
        "program_title": episode.program_title,
        "title": episode.title,
        "source_url": episode.source_url,
        "published_at": episode.published_at,
        "created_at": episode.created_at,
    }
    item["source_id"] = source_id or episode.subscription_id
    item["has_readable"] = has_readable
    item["display_status"] = display_status
    item["document_url"] = f"/docs/{episode.id}/readable" if has_readable else None
    item["job_id"] = job_id
    item["job_status"] = job_status
    item["job_error"] = job_error
    item["job_stage"] = job_stage
    item["job_stage_label"] = job_stage_label
    item["job_progress"] = job_progress
    item["job_stage_started_at"] = job_stage_started_at
    return item


def _parse_glossary_sources(source_text: str) -> list[str]:
    raw_parts = re.split(r"[,，;；\n]+", source_text)
    sources: list[str] = []
    seen: set[str] = set()
    for part in raw_parts:
        source = part.strip()
        if source and source not in seen:
            sources.append(source)
            seen.add(source)
    return sources


def _upsert_glossary_entries(
    sources: list[str],
    target: str,
    glossary_path: Path | None = None,
) -> dict:
    glossary_path = glossary_path or _global_glossary_path()
    data, canonical, corrections = _load_glossary_parts(glossary_path)

    target = target.strip()
    added_canonical = False
    if target and target not in canonical:
        canonical.append(target)
        added_canonical = True

    changed_corrections: dict[str, str] = {}
    for source in sources:
        source = source.strip()
        if not source or source == target:
            continue
        if corrections.get(source) != target:
            corrections[source] = target
            changed_corrections[source] = target

    _write_glossary_parts(glossary_path, data, canonical, corrections)
    return {
        "path": str(glossary_path),
        "scope": "global",
        "canonical": target,
        "added_canonical": added_canonical,
        "corrections": changed_corrections,
    }


def _upsert_global_canonical(
    terms: list[str],
    glossary_path: Path | None = None,
) -> dict:
    glossary_path = glossary_path or _global_glossary_path()
    data, canonical, corrections = _load_glossary_parts(glossary_path)
    added: list[str] = []
    for term in terms:
        term = term.strip()
        if term and term not in canonical:
            canonical.append(term)
            added.append(term)
    _write_glossary_parts(glossary_path, data, canonical, corrections)
    return {
        "path": str(glossary_path),
        "scope": "global",
        "canonical": added,
        "added_canonical": bool(added),
        "corrections": {},
    }


def _upsert_episode_canonical(readable_path: Path, terms: list[str]) -> dict:
    glossary_path = readable_path.with_name(readable_path.stem + ".glossary.json")
    data, canonical, corrections = _load_glossary_parts(glossary_path)
    added: list[str] = []
    for term in terms:
        term = term.strip()
        if term and term not in canonical:
            canonical.append(term)
            added.append(term)
    _write_glossary_parts(glossary_path, data, canonical, corrections)
    return {
        "path": str(glossary_path),
        "scope": "episode",
        "canonical": added,
        "added_canonical": bool(added),
        "corrections": {},
    }


def _upsert_episode_correction(readable_path: Path, sources: list[str], target: str) -> dict:
    glossary_path = readable_path.with_name(readable_path.stem + ".glossary.json")
    data, canonical, corrections = _load_glossary_parts(glossary_path)
    target = target.strip()
    if target and target not in canonical:
        canonical.append(target)
    changed_corrections: dict[str, str] = {}
    for source in sources:
        source = source.strip()
        if not source or source == target:
            continue
        if corrections.get(source) != target:
            corrections[source] = target
            changed_corrections[source] = target
    _write_glossary_parts(glossary_path, data, canonical, corrections)
    return {
        "path": str(glossary_path),
        "scope": "episode",
        "canonical": target,
        "added_canonical": bool(target),
        "corrections": changed_corrections,
    }


def _load_glossary_parts(path: Path) -> tuple[dict, list[str], dict[str, str]]:
    data = json.loads(path.read_text(encoding="utf-8")) if path.exists() else {}
    if not isinstance(data, dict):
        raise ValueError(f"Glossary must be a JSON object: {path}")
    canonical = data.get("canonical")
    corrections = data.get("corrections")
    if not isinstance(canonical, list):
        canonical = []
    if not isinstance(corrections, dict):
        corrections = {}
    normalized_corrections = {
        str(source): str(target)
        for source, target in corrections.items()
        if str(source).strip() and str(target).strip()
    }
    return data, [str(term) for term in canonical if str(term).strip()], normalized_corrections


def _write_glossary_parts(
    path: Path,
    data: dict,
    canonical: list[str],
    corrections: dict[str, str],
) -> None:
    data["canonical"] = canonical
    data["corrections"] = dict(sorted(corrections.items(), key=lambda item: item[0].lower()))
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def _global_glossary_path() -> Path:
    return PROJECT_ROOT / "glossary.json"


def _resolve_delete_glossary_path(scope: str, path_text: str) -> Path:
    if scope == "global":
        return _global_glossary_path()
    if scope != "episode":
        raise ValueError("scope must be global or episode")
    if not path_text:
        raise ValueError("path is required for episode glossary deletion")
    path = Path(path_text).expanduser()
    resolved = path.resolve(strict=False)
    allowed_root = PROJECT_ROOT.parent.resolve()
    if allowed_root not in (resolved, *resolved.parents):
        raise ValueError("glossary path is outside the project workspace")
    if not resolved.name.endswith(".glossary.json"):
        raise ValueError("episode glossary path must end with .glossary.json")
    return resolved


def _delete_glossary_entry(
    glossary_path: Path,
    item_type: str,
    source: str,
    target: str = "",
) -> dict:
    source = source.strip()
    target = target.strip()
    if not source:
        raise ValueError("source is required")
    data, canonical, corrections = _load_glossary_parts(glossary_path)

    if item_type == "canonical":
        before = len(canonical)
        canonical = [term for term in canonical if term != source]
        if len(canonical) == before:
            raise ValueError(f"canonical term not found: {source}")
        deleted = {"type": "canonical", "source": source}
    elif item_type == "correction":
        current = corrections.get(source)
        if current is None:
            raise ValueError(f"correction not found: {source}")
        if target and current != target:
            raise ValueError(f"correction target changed: {source} -> {current}")
        del corrections[source]
        deleted = {"type": "correction", "source": source, "target": current}
    else:
        raise ValueError("type must be canonical or correction")

    if glossary_path != _global_glossary_path() and not canonical and not corrections:
        if glossary_path.exists():
            glossary_path.unlink()
        deleted["removed_file"] = True
    else:
        _write_glossary_parts(glossary_path, data, canonical, corrections)
        deleted["removed_file"] = False
    deleted["path"] = str(glossary_path)
    return deleted


def _run_apply_glossary(readable_path: Path) -> dict:
    command = [_python_bin(), str(PROJECT_ROOT / "scripts" / "apply_glossary.py"), str(readable_path)]
    completed = subprocess.run(
        command,
        cwd=str(PROJECT_ROOT),
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        check=False,
    )
    return {
        "exit_code": completed.returncode,
        "output": completed.stdout[-6000:],
    }


def _run_verify_for_readable(episode: Episode, readable_path: Path) -> dict:
    candidate_path = readable_path.with_name(readable_path.stem + ".glossary-candidates.json")
    command = [
        _python_bin(),
        str(PROJECT_ROOT / "scripts" / "verify_readable.py"),
        str(episode.transcript_path or ""),
        str(readable_path),
        "--strict",
        "--emit-glossary-candidates",
        str(candidate_path),
    ]
    from .readable import speaker_map_path_for_readable

    speaker_map_path = speaker_map_path_for_readable(readable_path)
    if speaker_map_path.exists():
        command.extend(["--speaker-map-file", str(speaker_map_path)])
    completed = subprocess.run(
        command,
        cwd=str(PROJECT_ROOT),
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        check=False,
    )
    return {
        "exit_code": completed.returncode,
        "output": completed.stdout[-10000:],
        "candidate_path": str(candidate_path),
    }


def _load_glossary_candidates(readable_path: Path) -> dict:
    path = readable_path.with_name(readable_path.stem + ".glossary-candidates.json")
    if not path.exists():
        return {"unknown_tokens": [], "variant_clusters": []}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {"unknown_tokens": [], "variant_clusters": []}
    return data if isinstance(data, dict) else {"unknown_tokens": [], "variant_clusters": []}


def _python_bin() -> str:
    python = PROJECT_ROOT / ".venv" / "bin" / "python"
    return str(python if python.exists() else Path(sys.executable))


def _save_document_body_patch(
    episode: Episode,
    readable_path: Path,
    *,
    block_id: str,
    before_hash: str,
    after_text: str,
) -> dict:
    rendered_markdown = render_readable_markdown(readable_path)
    parsed = parse_readable_markdown(rendered_markdown)
    block = next((item for item in parsed.blocks if item.block_id == block_id), None)
    if block is None:
        raise KeyError(f"Readable block not found: {block_id}")
    if block.body_hash != before_hash:
        raise ValueError("当前段落已有更新，请刷新页面后再编辑。")

    normalized_after = after_text.replace("\r\n", "\n").replace("\r", "\n").strip("\n")
    if hash_body_text(normalized_after) == before_hash:
        return {
            "changed": False,
            "block": {"block_id": block.block_id, "body_hash": block.body_hash},
            "verify": {"exit_code": 0, "output": "No text changes."},
        }

    candidate_markdown = replace_rendered_block_body(readable_path, block_id, normalized_after)
    verify_result = _run_verify_for_rendered_readable(
        episode,
        readable_path,
        candidate_markdown,
        strict=False,
    )
    if verify_result["exit_code"] != 0:
        raise ValueError("校验未通过，未保存修改。\n" + verify_result["output"])

    patch = append_approved_body_patch(
        readable_path,
        episode_id=episode.id,
        block_id=block.block_id,
        before_hash=before_hash,
        before_text=block.text,
        after_text=normalized_after,
    )
    return {
        "changed": True,
        "patch": patch,
        "block": {
            "block_id": block.block_id,
            "body_hash": hash_body_text(normalized_after),
        },
        "verify": verify_result,
    }


def _run_verify_for_rendered_readable(
    episode: Episode,
    readable_path: Path,
    markdown: str,
    *,
    strict: bool = True,
) -> dict:
    if not episode.transcript_path:
        return {
            "exit_code": 0,
            "output": "No transcript path; skipped verification.",
            "candidate_path": "",
        }
    candidate_path = readable_path.with_name(readable_path.stem + ".glossary-candidates.json")
    with tempfile.TemporaryDirectory(prefix="podcast-readable-edit-") as tmpdir:
        temp_readable = Path(tmpdir) / readable_path.name
        temp_readable.write_text(markdown, encoding="utf-8")
        command = [
            _python_bin(),
            str(PROJECT_ROOT / "scripts" / "verify_readable.py"),
            str(episode.transcript_path),
            str(temp_readable),
            "--emit-glossary-candidates",
            str(candidate_path),
        ]
        if strict:
            command.insert(4, "--strict")
        sidecar_glossary_path = readable_path.with_name(readable_path.stem + ".glossary.json")
        if sidecar_glossary_path.exists():
            command.extend(["--glossary", str(sidecar_glossary_path)])
        from .readable import speaker_map_path_for_readable

        speaker_map_path = speaker_map_path_for_readable(readable_path)
        if speaker_map_path.exists():
            command.extend(["--speaker-map-file", str(speaker_map_path)])
        completed = subprocess.run(
            command,
            cwd=str(PROJECT_ROOT),
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            check=False,
        )
    return {
        "exit_code": completed.returncode,
        "output": completed.stdout[-10000:],
        "candidate_path": str(candidate_path),
    }


def _render_document_page(
    episode: Episode,
    readable_path: Path,
    lark_doc_url: str | None = None,
) -> str:
    markdown = render_readable_markdown(readable_path)
    body = _markdown_to_editable_html(markdown)
    title = html.escape(episode.title)
    program = html.escape(episode.program_title)
    source_url = html.escape(episode.source_url, quote=True)
    lark_url = html.escape(lark_doc_url or "", quote=True)
    lark_export_label = "同步飞书" if lark_doc_url else "发到飞书"
    published_at = html.escape(episode.published_at or "")
    candidates_panel = _render_glossary_candidates_panel(episode, readable_path)
    glossary_dock = _render_glossary_correction_dock(episode)
    return f"""<!doctype html>
<html lang="zh-CN">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>{title} - 阅读版</title>
  <style>
    :root {{
      --bg: #f6f7f5;
      --panel: #ffffff;
      --panel-muted: #f0f2ef;
      --line: #dcdfda;
      --muted: #697168;
      --text: #171a18;
      --accent: #1c2420;
      --accent-hover: #111713;
      --accent-soft: #e9eee9;
      --green: #2f6f4e;
    }}
    * {{ box-sizing: border-box; }}
    body {{
      margin: 0;
      background: var(--bg);
      color: var(--text);
      font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", "PingFang SC",
        "Microsoft YaHei", sans-serif;
      line-height: 1.72;
    }}
    .topbar {{
      height: 58px;
      display: flex;
      align-items: center;
      justify-content: space-between;
      gap: 16px;
      padding: 0 28px;
      background: var(--panel);
      border-bottom: 1px solid var(--line);
      position: sticky;
      top: 0;
      z-index: 2;
    }}
    .nav {{
      display: flex;
      align-items: center;
      gap: 10px;
      min-width: 0;
    }}
    .actions {{
      display: flex;
      align-items: center;
      gap: 8px;
      flex: 0 0 auto;
    }}
    .back,
    .source,
    .doc-action {{
      height: 34px;
      border-radius: 7px;
      display: inline-flex;
      align-items: center;
      padding: 0 10px;
      text-decoration: none;
      font-weight: 650;
      white-space: nowrap;
    }}
    .back {{
      color: var(--text);
      background: var(--panel-muted);
    }}
    .source {{
      color: #fff;
      background: var(--accent);
    }}
    button.doc-action {{
      border: 0;
      cursor: pointer;
      font: inherit;
    }}
    .doc-action {{
      color: var(--text);
      background: var(--panel-muted);
    }}
    .back:hover {{
      background: var(--accent-soft);
    }}
    .doc-action:hover {{
      background: var(--accent-soft);
    }}
    .doc-action:disabled {{
      cursor: wait;
      opacity: 0.72;
    }}
    .source:hover {{
      background: var(--accent-hover);
    }}
    .settings-menu {{
      position: relative;
    }}
    .settings-button {{
      height: 34px;
      border-radius: 7px;
      display: inline-flex;
      align-items: center;
      gap: 6px;
      padding: 0 10px;
      color: var(--text);
      background: var(--panel-muted);
      font-weight: 650;
    }}
    .settings-button:hover,
    .settings-button[aria-expanded="true"] {{
      background: var(--accent-soft);
    }}
    .settings-caret {{
      color: var(--muted);
      font-size: 11px;
    }}
    .settings-dropdown {{
      position: absolute;
      top: calc(100% + 8px);
      right: 0;
      min-width: 148px;
      padding: 6px;
      border: 1px solid var(--line);
      border-radius: 8px;
      background: var(--panel);
      box-shadow: 0 6px 18px rgba(23, 26, 24, 0.08);
      display: none;
      z-index: 5;
    }}
    .settings-menu.is-open .settings-dropdown {{
      display: block;
    }}
    .settings-item {{
      min-height: 34px;
      border-radius: 6px;
      display: flex;
      align-items: center;
      padding: 0 10px;
      color: var(--text);
      text-decoration: none;
      font-weight: 650;
      white-space: nowrap;
    }}
    .settings-item:hover {{
      background: var(--panel-muted);
    }}
    .doc-title {{
      min-width: 0;
      overflow: hidden;
      text-overflow: ellipsis;
      white-space: nowrap;
      color: var(--muted);
      font-size: 13px;
    }}
    .lark-status {{
      min-height: 18px;
      color: var(--muted);
      font-size: 12px;
      line-height: 1.45;
      margin: 0 0 10px;
    }}
    .lark-status.error {{
      color: #9f3a2d;
    }}
    .audit-panel {{
      margin: 0 0 16px;
      padding: 14px 16px;
      background: #fffaf0;
      border: 1px solid #eadcc0;
      color: #312c22;
    }}
    .audit-head {{
      display: flex;
      align-items: baseline;
      justify-content: space-between;
      gap: 12px;
      margin-bottom: 10px;
    }}
    .audit-title {{
      font-size: 15px;
      font-weight: 750;
    }}
    .audit-note {{
      color: #776d5c;
      font-size: 12px;
      white-space: nowrap;
    }}
    .audit-section {{
      margin-top: 10px;
    }}
    .audit-section-title {{
      color: #776d5c;
      font-size: 12px;
      font-weight: 700;
      margin-bottom: 6px;
    }}
    .audit-tokens {{
      display: flex;
      flex-wrap: wrap;
      gap: 6px;
      margin: 0;
      padding: 0;
      list-style: none;
    }}
    .audit-token {{
      display: inline-flex;
      align-items: center;
      gap: 4px;
      max-width: 100%;
      padding: 4px 7px;
      border-radius: 6px;
      background: rgba(255, 255, 255, 0.72);
      border: 1px solid #efe4ce;
      font-size: 12px;
      line-height: 1.35;
    }}
    .audit-token code {{
      padding: 0;
      background: transparent;
      color: #2a261f;
      font-size: 12px;
    }}
    .audit-count {{
      color: #776d5c;
    }}
    .audit-cluster {{
      color: #2a261f;
    }}
    .audit-fill {{
      border: 0;
      border-radius: 5px;
      background: #efe4ce;
      color: #423828;
      cursor: pointer;
      font-size: 11px;
      font-weight: 700;
      padding: 2px 5px;
    }}
    .audit-editor {{
      display: grid;
      grid-template-columns: minmax(0, 1fr) minmax(0, 1fr) auto;
      gap: 8px;
    }}
    .audit-buttons {{
      display: flex;
      align-items: center;
      gap: 6px;
      flex-wrap: wrap;
    }}
    .audit-input {{
      width: 100%;
      min-width: 0;
      height: 34px;
      border: 1px solid #e6d7bc;
      border-radius: 7px;
      background: rgba(255, 255, 255, 0.82);
      color: var(--text);
      font: inherit;
      font-size: 13px;
      padding: 0 9px;
    }}
    .audit-save {{
      height: 34px;
      border: 0;
      border-radius: 7px;
      background: var(--accent);
      color: #fff;
      cursor: pointer;
      font: inherit;
      font-size: 13px;
      font-weight: 750;
      padding: 0 11px;
      white-space: nowrap;
    }}
    .audit-save.secondary {{
      background: #efe4ce;
      color: #423828;
    }}
    .audit-save:disabled {{
      cursor: wait;
      opacity: 0.72;
    }}
    .audit-dock {{
      position: fixed;
      left: 50%;
      bottom: 14px;
      transform: translateX(-50%);
      width: min(920px, calc(100vw - 28px));
      padding: 10px;
      border: 1px solid #eadcc0;
      border-radius: 8px;
      background: #fffaf0;
      box-shadow: 0 10px 28px rgba(23, 26, 24, 0.12);
      z-index: 4;
    }}
    .audit-dock-head {{
      display: flex;
      align-items: center;
      justify-content: space-between;
      gap: 10px;
      margin-bottom: 8px;
      color: #776d5c;
      font-size: 12px;
      line-height: 1.35;
    }}
    .audit-dock-title {{
      color: #423828;
      font-weight: 750;
    }}
    .audit-status {{
      min-height: 17px;
      margin-top: 8px;
      color: #776d5c;
      font-size: 12px;
      line-height: 1.4;
    }}
    .audit-status.error {{
      color: #9f3a2d;
    }}
    .transcript-block {{
      position: relative;
      margin: 0 0 18px;
      padding: 0 78px 0 0;
    }}
    .transcript-block p:last-child {{
      margin-bottom: 0;
    }}
    .block-tools {{
      position: absolute;
      top: 0;
      right: 0;
      display: flex;
      gap: 6px;
      opacity: 0;
      transition: opacity 0.15s ease;
    }}
    .transcript-block:hover .block-tools,
    .transcript-block.is-editing .block-tools,
    .block-tools:focus-within {{
      opacity: 1;
    }}
    .block-edit {{
      height: 28px;
      border: 1px solid var(--line);
      border-radius: 6px;
      background: var(--panel-muted);
      color: var(--text);
      cursor: pointer;
      font: inherit;
      font-size: 12px;
      font-weight: 700;
      padding: 0 9px;
      white-space: nowrap;
    }}
    .block-edit:hover {{
      background: var(--accent-soft);
    }}
    .block-editor {{
      display: none;
      margin: 8px 0 4px;
      padding: 10px;
      border: 1px solid var(--line);
      border-radius: 7px;
      background: #fbfcfa;
    }}
    .transcript-block.is-editing .block-rendered {{
      display: none;
    }}
    .transcript-block.is-editing .block-editor {{
      display: block;
    }}
    .block-textarea {{
      width: 100%;
      min-height: 150px;
      resize: vertical;
      border: 1px solid var(--line);
      border-radius: 6px;
      background: #fff;
      color: var(--text);
      font: inherit;
      font-size: 15px;
      line-height: 1.65;
      padding: 10px 12px;
    }}
    .block-editor-actions {{
      display: flex;
      align-items: center;
      gap: 8px;
      margin-top: 8px;
    }}
    .block-save,
    .block-cancel {{
      height: 30px;
      border: 0;
      border-radius: 6px;
      cursor: pointer;
      font: inherit;
      font-size: 12px;
      font-weight: 750;
      padding: 0 10px;
    }}
    .block-save {{
      background: var(--accent);
      color: #fff;
    }}
    .block-cancel {{
      background: var(--panel-muted);
      color: var(--text);
    }}
    .block-save:disabled {{
      cursor: wait;
      opacity: 0.72;
    }}
    .block-status {{
      min-height: 16px;
      color: var(--muted);
      font-size: 12px;
      line-height: 1.35;
    }}
    .block-status.error {{
      color: #9f3a2d;
    }}
    main {{
      max-width: 920px;
      margin: 0 auto;
      padding: 34px 28px 132px;
    }}
    article {{
      background: var(--panel);
      border: 1px solid var(--line);
      padding: 34px 42px;
      box-shadow: 0 1px 2px rgba(23, 26, 24, 0.05);
    }}
    h1, h2, h3 {{
      line-height: 1.35;
      letter-spacing: 0;
    }}
    h1 {{
      margin: 0 0 18px;
      font-size: 30px;
    }}
    h2 {{ margin: 34px 0 14px; font-size: 22px; }}
    h3 {{ margin: 26px 0 10px; font-size: 18px; }}
    p {{ margin: 0 0 18px; }}
    blockquote {{
      margin: 0 0 18px;
      padding: 12px 16px;
      border-left: 3px solid var(--green);
      color: #3f4641;
      background: var(--panel-muted);
    }}
    blockquote p {{ margin: 0 0 8px; }}
    blockquote p:last-child {{ margin-bottom: 0; }}
    code {{
      padding: 2px 5px;
      border-radius: 5px;
      background: var(--panel-muted);
      color: #3f4641;
      font-family: ui-monospace, SFMono-Regular, Menlo, Consolas, monospace;
      font-size: 0.9em;
    }}
    strong {{ font-weight: 750; }}
    hr {{
      border: 0;
      border-top: 1px solid var(--line);
      margin: 28px 0;
    }}
    .meta {{
      color: var(--muted);
      font-size: 13px;
      margin-bottom: 18px;
    }}
    .muted {{
      color: var(--muted);
    }}
    @media print {{
      body {{ background: #fff; }}
      .topbar {{ display: none; }}
      .audit-panel, .audit-dock, .lark-status {{ display: none; }}
      .block-tools, .block-editor {{ display: none !important; }}
      .transcript-block {{ padding-right: 0; }}
      main {{ max-width: none; padding: 0; }}
      article {{ border: 0; box-shadow: none; padding: 0; }}
    }}
    @media (max-width: 720px) {{
      .topbar {{ padding: 0 14px; }}
      .actions {{ gap: 6px; }}
      .doc-action, .source {{ padding: 0 8px; }}
      main {{ padding: 18px 14px 226px; }}
      article {{ padding: 24px 18px; }}
      h1 {{ font-size: 24px; }}
      .doc-title {{ display: none; }}
      .audit-editor {{ grid-template-columns: 1fr; }}
      .audit-dock {{
        bottom: 0;
        width: 100%;
        border-right: 0;
        border-bottom: 0;
        border-left: 0;
        border-radius: 8px 8px 0 0;
      }}
      .audit-dock-head {{
        align-items: flex-start;
      }}
      .transcript-block {{ padding-right: 0; }}
      .block-tools {{
        position: static;
        opacity: 1;
        margin-bottom: 8px;
      }}
    }}
  </style>
</head>
<body>
  <header class="topbar">
    <div class="nav">
      <a class="back" href="/">返回</a>
      <span class="doc-title">{program} · {published_at}</span>
    </div>
    <div class="actions">
      <a class="doc-action" href="/docs/{html.escape(episode.id, quote=True)}/readable.md">下载 MD</a>
      <button class="doc-action" type="button" onclick="window.print()">保存 PDF</button>
      <button class="doc-action" id="larkButton" data-episode="{html.escape(episode.id, quote=True)}" data-lark-url="{lark_url}" type="button">{lark_export_label}</button>
      {f'<button class="doc-action secondary" id="openLarkButton" data-lark-url="{lark_url}" type="button">打开飞书</button>' if lark_doc_url else ''}
      <div class="settings-menu" id="settingsMenu">
        <button class="settings-button" id="settingsButton" type="button" aria-haspopup="menu" aria-expanded="false">
          设置 <span class="settings-caret">▾</span>
        </button>
        <div class="settings-dropdown" id="settingsDropdown" role="menu" aria-labelledby="settingsButton">
          <a class="settings-item" href="/glossary" role="menuitem">热词库</a>
        </div>
      </div>
      <a class="source" href="{source_url}" target="_blank" rel="noreferrer">原文</a>
    </div>
  </header>
  <main>
    <div class="lark-status" id="larkStatus" aria-live="polite"></div>
    {candidates_panel}
    {glossary_dock}
    <article>
      {body}
    </article>
  </main>
  <script>
    const larkButton = document.getElementById("larkButton");
    const openLarkButton = document.getElementById("openLarkButton");
    const larkStatus = document.getElementById("larkStatus");
    const scrollKey = "podcast-readable-scroll:{html.escape(episode.id, quote=True)}";
    const savedScrollY = sessionStorage.getItem(scrollKey);
    if (savedScrollY !== null) {{
      sessionStorage.removeItem(scrollKey);
      requestAnimationFrame(() => window.scrollTo(0, Number(savedScrollY) || 0));
    }}
    function reloadKeepingScroll(delay = 450) {{
      sessionStorage.setItem(scrollKey, String(window.scrollY));
      setTimeout(() => window.location.reload(), delay);
    }}
    const settingsMenu = document.getElementById("settingsMenu");
    const settingsButton = document.getElementById("settingsButton");
    function setSettingsOpen(open) {{
      if (!settingsMenu || !settingsButton) return;
      settingsMenu.classList.toggle("is-open", open);
      settingsButton.setAttribute("aria-expanded", open ? "true" : "false");
    }}
    if (settingsButton && settingsMenu) {{
      settingsButton.addEventListener("click", event => {{
        event.stopPropagation();
        setSettingsOpen(!settingsMenu.classList.contains("is-open"));
      }});
      document.addEventListener("click", event => {{
        if (settingsMenu.contains(event.target)) return;
        setSettingsOpen(false);
      }});
      document.addEventListener("keydown", event => {{
        if (event.key === "Escape") setSettingsOpen(false);
      }});
    }}
    function setLarkStatus(text, isError = false) {{
      if (!larkStatus) return;
      larkStatus.textContent = text || "";
      larkStatus.classList.toggle("error", isError);
    }}
    if (larkButton) {{
      larkButton.addEventListener("click", async () => {{
        const existingUrl = larkButton.dataset.larkUrl;
        const action = existingUrl ? "sync" : "export";
        larkButton.disabled = true;
        larkButton.textContent = action === "sync" ? "同步中" : "发送中";
        setLarkStatus(action === "sync" ? "正在同步当前版本到飞书文档..." : "正在导入飞书文档并发送到群...");
        try {{
          const response = await fetch("/api/docs/lark", {{
            method: "POST",
            headers: {{ "Content-Type": "application/json" }},
            body: JSON.stringify({{ episode_id: larkButton.dataset.episode, action }})
          }});
          const payload = await response.json();
          if (!response.ok) throw new Error(payload.error || "发送失败");
          const url = payload.export && payload.export.lark_doc_url;
          if (!url) throw new Error("飞书文档链接为空");
          larkButton.dataset.larkUrl = url;
          larkButton.textContent = "同步飞书";
          setLarkStatus(action === "sync" ? "已同步到飞书。" : "已发送到飞书。");
          if (openLarkButton) openLarkButton.dataset.larkUrl = url;
          if (action !== "sync") window.open(url, "_blank");
        }} catch (error) {{
          larkButton.textContent = existingUrl ? "重试同步" : "重试飞书";
          larkButton.title = error.message;
          setLarkStatus(error.message || "发送失败", true);
        }} finally {{
          larkButton.disabled = false;
        }}
      }});
    }}
    if (openLarkButton) {{
      openLarkButton.addEventListener("click", () => {{
        const url = openLarkButton.dataset.larkUrl;
        if (url) window.open(url, "_blank");
      }});
    }}
    const glossaryDock = document.getElementById("glossaryDock");
    if (glossaryDock) {{
      const sourceInput = document.getElementById("glossarySource");
      const targetInput = document.getElementById("glossaryTarget");
      const correctButton = document.getElementById("glossaryCorrect");
      const canonicalButton = document.getElementById("glossaryCanonical");
      const episodeButton = document.getElementById("glossaryEpisode");
      const actionButtons = [correctButton, canonicalButton, episodeButton].filter(Boolean);
      const glossaryStatus = document.getElementById("glossaryStatus");
      function setGlossaryStatus(text, isError = false) {{
        if (!glossaryStatus) return;
        glossaryStatus.textContent = text || "";
        glossaryStatus.classList.toggle("error", isError);
      }}
      function setActionButtons(disabled) {{
        actionButtons.forEach((button) => button.disabled = disabled);
      }}
      async function submitGlossaryAction(action, source, target = "") {{
        if (action === "correct" && (!source || !target)) {{
          setGlossaryStatus("修正需要填写错词和正确词。", true);
          return;
        }}
        if (action === "canonical" && !(target || source)) {{
          setGlossaryStatus("请填写正确词。", true);
          return;
        }}
        if (action === "episode_correct" && (!source || !target)) {{
          setGlossaryStatus("本集修改需要填写错词和正确词。", true);
          return;
        }}
        const episodeId = glossaryDock.dataset.episode;
        setActionButtons(true);
        setGlossaryStatus("正在写入词典，并重新检查当前阅读版...");
        try {{
          const response = await fetch("/api/glossary/correction", {{
            method: "POST",
            headers: {{ "Content-Type": "application/json" }},
            body: JSON.stringify({{ episode_id: episodeId, action, source, target }})
          }});
          const payload = await response.json();
          if (!response.ok) throw new Error(payload.error || "保存失败");
          let message = "已保存，正在刷新...";
          if (action === "correct") {{
            const changed = payload.glossary && payload.glossary.corrections
              ? Object.keys(payload.glossary.corrections).length
              : 0;
            message = `已保存 ${{changed}} 条错词映射，正在刷新...`;
          }} else if (action === "canonical") {{
            message = "已加入全局热词库，正在刷新...";
          }} else if (action === "episode_correct") {{
            message = "已保存为本集修改，正在刷新...";
          }}
          setGlossaryStatus(message);
          reloadKeepingScroll(650);
        }} catch (error) {{
          setActionButtons(false);
          setGlossaryStatus(error.message || "保存失败", true);
        }}
      }}
      document.querySelectorAll("[data-fill-source]").forEach((button) => {{
        button.addEventListener("click", () => {{
          if (sourceInput) sourceInput.value = button.dataset.fillSource || "";
          if (targetInput && Object.prototype.hasOwnProperty.call(button.dataset, "fillTarget")) {{
            targetInput.value = button.dataset.fillTarget || "";
          }}
          if (targetInput) targetInput.focus();
        }});
      }});
      document.querySelectorAll("[data-review-action]").forEach((button) => {{
        button.addEventListener("click", () => {{
          const action = button.dataset.reviewAction || "";
          const source = button.dataset.source || "";
          if (action === "correct" || action === "episode_correct") {{
            if (sourceInput) sourceInput.value = source;
            if (targetInput) targetInput.focus();
            setGlossaryStatus(action === "correct" ? "填写正确词后点“修正为”。" : "填写正确词后点“本集修改”。");
            return;
          }}
          submitGlossaryAction(action, source, "");
        }});
      }});
      document.addEventListener("mouseup", () => {{
        const active = document.activeElement;
        if (active === sourceInput || active === targetInput) return;
        const selection = window.getSelection();
        if (!selection || selection.isCollapsed) return;
        const selectedText = selection.toString().replace(/\\s+/g, " ").trim();
        if (!selectedText || selectedText.length > 80) return;
        const article = document.querySelector("article");
        if (!article || !article.contains(selection.anchorNode)) return;
        if (sourceInput) sourceInput.value = selectedText;
        setGlossaryStatus("已填入选中文本。填写正确词后保存。");
      }});
      if (correctButton) correctButton.addEventListener("click", () => {{
        submitGlossaryAction(
          "correct",
          sourceInput ? sourceInput.value.trim() : "",
          targetInput ? targetInput.value.trim() : ""
        );
      }});
      if (canonicalButton) canonicalButton.addEventListener("click", () => {{
        submitGlossaryAction(
          "canonical",
          sourceInput ? sourceInput.value.trim() : "",
          targetInput ? targetInput.value.trim() : ""
        );
      }});
      if (episodeButton) episodeButton.addEventListener("click", () => {{
        submitGlossaryAction(
          "episode_correct",
          sourceInput ? sourceInput.value.trim() : "",
          targetInput ? targetInput.value.trim() : ""
        );
      }});
    }}
    document.querySelectorAll(".transcript-block").forEach((block) => {{
      const editButton = block.querySelector("[data-block-edit]");
      const saveButton = block.querySelector("[data-block-save]");
      const cancelButton = block.querySelector("[data-block-cancel]");
      const textarea = block.querySelector("textarea");
      const status = block.querySelector(".block-status");
      const initialValue = textarea ? textarea.value : "";
      function setBlockStatus(text, isError = false) {{
        if (!status) return;
        status.textContent = text || "";
        status.classList.toggle("error", isError);
      }}
      if (editButton) editButton.addEventListener("click", () => {{
        block.classList.add("is-editing");
        if (textarea) {{
          textarea.focus();
          textarea.setSelectionRange(textarea.value.length, textarea.value.length);
        }}
        setBlockStatus("");
      }});
      if (cancelButton) cancelButton.addEventListener("click", () => {{
        if (textarea) textarea.value = initialValue;
        block.classList.remove("is-editing");
        setBlockStatus("");
      }});
      if (saveButton) saveButton.addEventListener("click", async () => {{
        if (!textarea) return;
        saveButton.disabled = true;
        setBlockStatus("正在保存并校验...");
        try {{
          const response = await fetch("/api/docs/patches", {{
            method: "POST",
            headers: {{ "Content-Type": "application/json" }},
            body: JSON.stringify({{
              episode_id: "{html.escape(episode.id, quote=True)}",
              block_id: block.dataset.blockId,
              before_hash: block.dataset.blockHash,
              after_text: textarea.value
            }})
          }});
          const payload = await response.json();
          if (!response.ok) throw new Error(payload.error || "保存失败");
          setBlockStatus("已保存，正在刷新...");
          reloadKeepingScroll(450);
        }} catch (error) {{
          saveButton.disabled = false;
          setBlockStatus(error.message || "保存失败", true);
        }}
      }});
    }});
  </script>
</body>
</html>
"""


def _render_document_conflict_page(message: str) -> str:
    escaped_message = html.escape(message)
    return f"""<!doctype html>
<html lang="zh-CN">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>阅读版需要处理</title>
  <style>
    body {{
      margin: 0;
      min-height: 100vh;
      display: grid;
      place-items: center;
      background: #f6f7f5;
      color: #171a18;
      font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", "PingFang SC",
        "Microsoft YaHei", sans-serif;
    }}
    main {{
      width: min(620px, calc(100vw - 32px));
      padding: 28px;
      background: #fff;
      border: 1px solid #dcdfda;
      border-radius: 8px;
    }}
    h1 {{
      margin: 0 0 12px;
      font-size: 22px;
    }}
    p {{
      margin: 0 0 18px;
      color: #697168;
      line-height: 1.7;
    }}
    .actions {{
      display: flex;
      gap: 10px;
      flex-wrap: wrap;
    }}
    a, button {{
      height: 36px;
      border: 0;
      border-radius: 7px;
      display: inline-flex;
      align-items: center;
      padding: 0 12px;
      color: #171a18;
      background: #f0f2ef;
      text-decoration: none;
      font: inherit;
      font-weight: 650;
      cursor: pointer;
    }}
    a.primary {{
      color: #fff;
      background: #1c2420;
    }}
  </style>
</head>
<body>
  <main>
    <h1>阅读版暂时不能打开</h1>
    <p>{escaped_message}</p>
    <div class="actions">
      <a class="primary" href="/">返回列表</a>
      <button type="button" onclick="window.location.reload()">刷新重试</button>
    </div>
  </main>
</body>
</html>
"""


def _render_glossary_page(store: Store) -> str:
    global_parts = _glossary_display_parts(_global_glossary_path())
    episode_sections = _episode_glossary_sections(store)
    global_section = (
        _render_glossary_page_scope(
            "全局热词库",
            "后续 ASR hotwords 与全局自动纠错会使用这些条目。",
            _global_glossary_path(),
            global_parts,
            scope="global",
        )
        if global_parts
        else '<section class="glossary-card"><h2>全局热词库</h2><p class="muted">还没有全局热词。</p></section>'
    )
    episode_html = (
        "".join(episode_sections)
        if episode_sections
        else '<section class="glossary-card"><h2>本集词库</h2><p class="muted">还没有任何本集词库。只有点击“本集修改”后才会生成。</p></section>'
    )
    return f"""<!doctype html>
<html lang="zh-CN">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>热词库 - 播客追踪</title>
  <style>
    :root {{
      --bg: #f6f7f5;
      --panel: #ffffff;
      --panel-muted: #f0f2ef;
      --line: #dcdfda;
      --muted: #697168;
      --text: #171a18;
      --accent: #1c2420;
      --accent-soft: #e9eee9;
      --green: #2f6f4e;
    }}
    * {{ box-sizing: border-box; }}
    body {{
      margin: 0;
      background: var(--bg);
      color: var(--text);
      font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", "PingFang SC", "Microsoft YaHei", sans-serif;
      font-size: 15px;
      line-height: 1.62;
    }}
    .topbar {{
      height: 58px;
      display: flex;
      align-items: center;
      justify-content: space-between;
      gap: 16px;
      padding: 0 28px;
      background: var(--panel);
      border-bottom: 1px solid var(--line);
      position: sticky;
      top: 0;
      z-index: 2;
    }}
    .back {{
      height: 34px;
      border-radius: 7px;
      display: inline-flex;
      align-items: center;
      padding: 0 10px;
      color: var(--text);
      background: var(--panel-muted);
      text-decoration: none;
      font-weight: 650;
    }}
    .back:hover {{ background: var(--accent-soft); }}
    .title {{
      min-width: 0;
      font-weight: 760;
      font-size: 17px;
    }}
    main {{
      max-width: 1120px;
      margin: 0 auto;
      padding: 24px 22px 56px;
    }}
    .intro {{
      color: var(--muted);
      margin: 0 0 18px;
    }}
    .glossary-card {{
      background: var(--panel);
      border: 1px solid var(--line);
      padding: 18px;
      margin-bottom: 16px;
    }}
    h1, h2, h3 {{ margin: 0; line-height: 1.25; }}
    h1 {{ font-size: 22px; }}
    h2 {{ font-size: 18px; }}
    h3 {{
      font-size: 13px;
      color: var(--muted);
      margin: 16px 0 8px;
    }}
    .scope-head {{
      display: flex;
      justify-content: space-between;
      align-items: flex-start;
      gap: 12px;
      margin-bottom: 10px;
    }}
    .scope-note {{
      color: var(--muted);
      font-size: 13px;
      margin-top: 5px;
    }}
    .file-path {{
      color: var(--muted);
      font-size: 12px;
      word-break: break-all;
      text-align: right;
    }}
    .tokens {{
      display: flex;
      flex-wrap: wrap;
      gap: 7px;
      padding: 0;
      margin: 0;
      list-style: none;
    }}
    .token {{
      position: relative;
      display: inline-flex;
      align-items: center;
      gap: 5px;
      max-width: 100%;
      padding: 5px 25px 5px 8px;
      border: 1px solid #dce4da;
      border-radius: 7px;
      background: #f9fbf8;
      font-size: 12px;
    }}
    .delete-token {{
      position: absolute;
      top: -7px;
      right: -7px;
      width: 19px;
      height: 19px;
      border: 1px solid #d3dbd1;
      border-radius: 50%;
      display: grid;
      place-items: center;
      padding: 0;
      background: #fff;
      color: #697168;
      cursor: pointer;
      font-size: 14px;
      line-height: 1;
    }}
    .delete-token:hover {{
      background: #fff1ee;
      border-color: #dba99f;
      color: #9f3a2d;
    }}
    code {{
      font-family: ui-monospace, SFMono-Regular, Menlo, Consolas, monospace;
      font-size: 12px;
      color: #202620;
      overflow-wrap: anywhere;
    }}
    .arrow {{ color: var(--muted); }}
    .muted {{ color: var(--muted); }}
    .count {{
      color: var(--muted);
      font-size: 12px;
      white-space: nowrap;
    }}
    @media (max-width: 720px) {{
      .topbar {{ padding: 0 14px; }}
      main {{ padding: 18px 14px 42px; }}
      .scope-head {{ display: block; }}
      .file-path {{ text-align: left; margin-top: 8px; }}
    }}
  </style>
</head>
<body>
  <header class="topbar">
    <a class="back" href="/">返回</a>
    <div class="title">热词库</div>
  </header>
  <main>
    <p class="intro">这里展示当前保存的全局热词和本集词库。全局条目会影响后续转写；本集条目只影响对应文稿。</p>
    {global_section}
    {episode_html}
  </main>
  <script>
    document.querySelectorAll("[data-glossary-delete]").forEach((button) => {{
      button.addEventListener("click", async () => {{
        const source = button.dataset.source || "";
        const target = button.dataset.target || "";
        const label = target ? `${{source}} → ${{target}}` : source;
        if (!window.confirm(`确认删除“${{label}}”吗？`)) return;
        button.disabled = true;
        try {{
          const response = await fetch("/api/glossary/delete", {{
            method: "POST",
            headers: {{ "Content-Type": "application/json" }},
            body: JSON.stringify({{
              scope: button.dataset.scope,
              type: button.dataset.type,
              path: button.dataset.path,
              source,
              target
            }})
          }});
          const payload = await response.json();
          if (!response.ok) throw new Error(payload.error || "删除失败");
          window.location.reload();
        }} catch (error) {{
          button.disabled = false;
          window.alert(error.message || "删除失败");
        }}
      }});
    }});
  </script>
</body>
</html>
"""


def _episode_glossary_sections(store: Store) -> list[str]:
    sections: list[str] = []
    seen: set[Path] = set()
    episodes = sorted(
        store.load_episodes().values(),
        key=lambda episode: episode.published_at or episode.created_at,
        reverse=True,
    )
    for episode in episodes:
        readable_path = readable_path_for_episode(episode)
        if readable_path is None:
            continue
        glossary_path = readable_path.with_name(readable_path.stem + ".glossary.json")
        if glossary_path in seen or not glossary_path.exists():
            continue
        seen.add(glossary_path)
        parts = _glossary_display_parts(glossary_path)
        if not parts:
            continue
        title = f"{episode.program_title} - {episode.title}"
        sections.append(
            _render_glossary_page_scope(
                title,
                "仅当前文稿使用。",
                glossary_path,
                parts,
                scope="episode",
            )
        )
    return sections


def _render_glossary_page_scope(
    title: str,
    note: str,
    path: Path,
    parts: dict[str, list[tuple[str, str | None]]],
    *,
    scope: str,
) -> str:
    term_count = len(parts.get("terms", []))
    mapping_count = len(parts.get("mappings", []))
    term_items = _render_page_glossary_items(
        parts.get("terms", []),
        scope=scope,
        glossary_path=path,
        item_type="canonical",
    )
    mapping_items = _render_page_glossary_items(
        parts.get("mappings", []),
        scope=scope,
        glossary_path=path,
        item_type="correction",
    )
    term_section = (
        f'<h3>规范写法 <span class="count">{term_count} 条</span></h3><ul class="tokens">{"".join(term_items)}</ul>'
        if term_items
        else ""
    )
    mapping_section = (
        f'<h3>错词映射 <span class="count">{mapping_count} 条</span></h3><ul class="tokens">{"".join(mapping_items)}</ul>'
        if mapping_items
        else ""
    )
    return (
        '<section class="glossary-card">'
        '<div class="scope-head">'
        '<div>'
        f'<h2>{html.escape(title)}</h2>'
        f'<div class="scope-note">{html.escape(note)}</div>'
        '</div>'
        f'<div class="file-path">{html.escape(str(path))}</div>'
        '</div>'
        f'{term_section}{mapping_section}'
        '</section>'
    )


def _render_page_glossary_items(
    items: list[tuple[str, str | None]],
    *,
    scope: str,
    glossary_path: Path,
    item_type: str,
) -> list[str]:
    rendered = []
    scope_attr = html.escape(scope, quote=True)
    type_attr = html.escape(item_type, quote=True)
    path_attr = html.escape(str(glossary_path), quote=True)
    for source, target in items:
        source_text = html.escape(source)
        source_attr = html.escape(source, quote=True)
        target_attr = html.escape(target or "", quote=True)
        delete_button = (
            '<button class="delete-token" type="button" title="删除" aria-label="删除" '
            'data-glossary-delete '
            f'data-scope="{scope_attr}" data-type="{type_attr}" data-path="{path_attr}" '
            f'data-source="{source_attr}" data-target="{target_attr}">×</button>'
        )
        if target is None:
            rendered.append(f'<li class="token"><code>{source_text}</code>{delete_button}</li>')
        else:
            rendered.append(
                f'<li class="token"><code>{source_text}</code><span class="arrow">→</span><code>{html.escape(target)}</code>{delete_button}</li>'
            )
    return rendered


def _render_glossary_candidates_panel(episode: Episode, readable_path: Path) -> str:
    candidates_path = readable_path.with_name(readable_path.stem + ".glossary-candidates.json")
    if not candidates_path.exists():
        return ""
    try:
        data = json.loads(candidates_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return ""
    unknown_tokens = data.get("unknown_tokens")
    variant_clusters = data.get("variant_clusters")
    if not isinstance(unknown_tokens, list):
        unknown_tokens = []
    if not isinstance(variant_clusters, list):
        variant_clusters = []

    unknown_items = [
        item
        for item in unknown_tokens
        if isinstance(item, dict) and isinstance(item.get("token"), str)
    ][:24]
    cluster_items = [
        [str(token) for token in cluster if str(token).strip()]
        for cluster in variant_clusters
        if isinstance(cluster, list)
    ][:12]
    sections: list[str] = []
    if unknown_items:
        tokens = []
        for item in unknown_items:
            raw_token = str(item.get("token") or "")
            token = html.escape(raw_token)
            fill_value = html.escape(raw_token, quote=True)
            count = html.escape(str(item.get("count") or 1))
            blocks = item.get("blocks")
            title = ""
            if isinstance(blocks, list) and blocks:
                title = f' title="块: {html.escape(", ".join(str(block) for block in blocks[:20]), quote=True)}"'
            tokens.append(
                f'<li class="audit-token"{title}><code>{token}</code><span class="audit-count">×{count}</span>'
                f'<button class="audit-fill" type="button" data-review-action="correct" data-source="{fill_value}">修正</button>'
                f'<button class="audit-fill" type="button" data-review-action="canonical" data-source="{fill_value}">正确</button>'
                f'<button class="audit-fill" type="button" data-review-action="episode_correct" data-source="{fill_value}">本集修改</button></li>'
            )
        sections.append(
            '<div class="audit-section"><div class="audit-section-title">词典外英文</div>'
            f'<ul class="audit-tokens">{"".join(tokens)}</ul></div>'
        )
    if cluster_items:
        clusters = []
        for cluster in cluster_items:
            text = html.escape(" / ".join(cluster))
            fill_value = html.escape(", ".join(cluster), quote=True)
            clusters.append(
                f'<li class="audit-token audit-cluster">{text}'
                f'<button class="audit-fill" type="button" data-review-action="correct" data-source="{fill_value}">批量修正</button>'
                f'<button class="audit-fill" type="button" data-review-action="episode_correct" data-source="{fill_value}">本集修改</button></li>'
            )
        sections.append(
            '<div class="audit-section"><div class="audit-section-title">疑似同词多拼</div>'
            f'<ul class="audit-tokens">{"".join(clusters)}</ul></div>'
        )

    return (
        '<section class="audit-panel" id="glossaryPanel" aria-label="疑似错词">'
        '<div class="audit-head">'
        '<div class="audit-title">疑似错词</div>'
        '<div class="audit-note">仅本地显示，不进入飞书文档</div>'
        '</div>'
        f'{"".join(sections)}'
        '</section>'
    )


def _render_glossary_correction_dock(episode: Episode) -> str:
    episode_id = html.escape(episode.id, quote=True)
    return (
        f'<section class="audit-dock" id="glossaryDock" data-episode="{episode_id}" aria-label="错词修正工具条">'
        '<div class="audit-dock-head">'
        '<div><span class="audit-dock-title">错词修正</span> 选中正文里的词会自动填入</div>'
        '<div>保存后停留在当前位置</div>'
        '</div>'
        '<div class="audit-editor">'
        '<input class="audit-input" id="glossarySource" placeholder="错词，可逗号分隔，如 a 证" autocomplete="off">'
        '<input class="audit-input" id="glossaryTarget" placeholder="正确词，如 agent" autocomplete="off">'
        '<div class="audit-buttons">'
        '<button class="audit-save" id="glossaryCorrect" type="button">修正为</button>'
        '<button class="audit-save secondary" id="glossaryCanonical" type="button">这是正确词</button>'
        '<button class="audit-save secondary" id="glossaryEpisode" type="button">本集修改</button>'
        '</div>'
        '</div>'
        '<div class="audit-status" id="glossaryStatus" aria-live="polite"></div>'
        '</section>'
    )


def _glossary_display_parts(path: Path) -> dict[str, list[tuple[str, str | None]]] | None:
    try:
        _data, canonical, corrections = _load_glossary_parts(path)
    except (OSError, json.JSONDecodeError, ValueError):
        return None
    terms = [(term, None) for term in canonical if term.strip()]
    mappings = [
        (source, target)
        for source, target in sorted(corrections.items(), key=lambda item: item[0].lower())
        if source.strip() and target.strip()
    ]
    if not terms and not mappings:
        return None
    return {"terms": terms, "mappings": mappings}


def _markdown_to_editable_html(markdown: str) -> str:
    parsed = parse_readable_markdown(markdown)
    if not parsed.blocks:
        return _markdown_to_html(markdown)

    html_blocks: list[str] = []
    cursor = 0
    for block in parsed.blocks:
        if block.header_line_index > cursor:
            prefix = "\n".join(parsed.lines[cursor : block.header_line_index])
            if prefix.strip():
                html_blocks.append(_markdown_to_html(prefix))

        header_html = _markdown_to_html(block.header_line)
        body_html = _markdown_to_html(block.text)
        if not body_html:
            body_html = '<p class="muted">（空）</p>'
        textarea_value = html.escape(block.text, quote=False)
        block_id = html.escape(block.block_id, quote=True)
        block_hash = html.escape(block.body_hash, quote=True)
        html_blocks.append(
            f'<section class="transcript-block" data-block-id="{block_id}" data-block-hash="{block_hash}">'
            '<div class="block-tools">'
            '<button class="block-edit" type="button" data-block-edit>编辑</button>'
            '</div>'
            '<div class="block-rendered">'
            f'{header_html}'
            f'<div class="block-body">{body_html}</div>'
            '</div>'
            '<div class="block-editor">'
            f'<textarea class="block-textarea" spellcheck="false">{textarea_value}</textarea>'
            '<div class="block-editor-actions">'
            '<button class="block-save" type="button" data-block-save>保存</button>'
            '<button class="block-cancel" type="button" data-block-cancel>取消</button>'
            '<span class="block-status" aria-live="polite"></span>'
            '</div>'
            '</div>'
            '</section>'
        )
        cursor = block.content_end_line

    if cursor < len(parsed.lines):
        suffix = "\n".join(parsed.lines[cursor:])
        if suffix.strip():
            html_blocks.append(_markdown_to_html(suffix))
    return "\n".join(html_blocks)


def _quote_header_filename(filename: str) -> str:
    return quote(filename, safe="")


def _markdown_to_html(markdown: str) -> str:
    blocks: list[str] = []
    paragraph: list[str] = []
    quote: list[str] = []

    def flush_paragraph() -> None:
        if paragraph:
            blocks.append(f"<p>{'<br>'.join(_inline_markdown(line) for line in paragraph)}</p>")
            paragraph.clear()

    def flush_quote() -> None:
        if quote:
            inner = "".join(f"<p>{_inline_markdown(line)}</p>" for line in quote)
            blocks.append(f"<blockquote>{inner}</blockquote>")
            quote.clear()

    for raw_line in markdown.splitlines():
        line = raw_line.strip()
        if not line:
            flush_paragraph()
            flush_quote()
            continue
        if line == "---":
            flush_paragraph()
            flush_quote()
            blocks.append("<hr>")
            continue
        if line.startswith(">"):
            flush_paragraph()
            quote.append(line[1:].strip())
            continue

        flush_quote()
        if line.startswith("#"):
            flush_paragraph()
            level = min(len(line) - len(line.lstrip("#")), 3)
            text = line[level:].strip()
            if text:
                blocks.append(f"<h{level}>{_inline_markdown(text)}</h{level}>")
            continue
        paragraph.append(line)

    flush_paragraph()
    flush_quote()
    return "\n".join(blocks)


def _inline_markdown(text: str) -> str:
    escaped = html.escape(text)
    escaped = re.sub(r"`([^`]+)`", r"<code>\1</code>", escaped)
    escaped = re.sub(r"\*\*([^*]+)\*\*", r"<strong>\1</strong>", escaped)
    return escaped
