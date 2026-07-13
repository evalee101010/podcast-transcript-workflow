from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable

from .config import PROJECT_ROOT
from .document_patches import render_readable_markdown
from .models import Episode, utc_now_iso
from .private_runtime import private_runtime_dir


DEFAULT_FOLDER_NAME = os.getenv("PODCAST_TRACKER_LARK_FOLDER_NAME", "Podcast Transcripts")
DEFAULT_CHAT_NAME = os.getenv("PODCAST_TRACKER_LARK_CHAT_NAME", "")
DEFAULT_MESSAGE_SEND_AS = os.getenv("PODCAST_TRACKER_LARK_MESSAGE_SEND_AS", "bot")
DEFAULT_CHAT_DOC_PERM = os.getenv("PODCAST_TRACKER_LARK_CHAT_DOC_PERM", "edit")
LARK_CLI_TIMEOUT_SECONDS = 120

LarkRunner = Callable[[list[str], Path], dict[str, Any]]


@dataclass(frozen=True)
class LarkExport:
    episode_id: str
    readable_path: str
    folder_name: str
    folder_token: str
    chat_name: str
    chat_id: str
    lark_doc_url: str
    lark_doc_token: str | None
    message_id: str | None
    exported_at: str

    def to_dict(self) -> dict:
        return {
            "episode_id": self.episode_id,
            "readable_path": self.readable_path,
            "folder_name": self.folder_name,
            "folder_token": self.folder_token,
            "chat_name": self.chat_name,
            "chat_id": self.chat_id,
            "lark_doc_url": self.lark_doc_url,
            "lark_doc_token": self.lark_doc_token,
            "message_id": self.message_id,
            "exported_at": self.exported_at,
        }

    @classmethod
    def from_dict(cls, row: dict) -> "LarkExport":
        return cls(
            episode_id=str(row["episode_id"]),
            readable_path=str(row["readable_path"]),
            folder_name=str(row["folder_name"]),
            folder_token=str(row["folder_token"]),
            chat_name=str(row["chat_name"]),
            chat_id=str(row["chat_id"]),
            lark_doc_url=str(row["lark_doc_url"]),
            lark_doc_token=row.get("lark_doc_token"),
            message_id=row.get("message_id"),
            exported_at=str(row["exported_at"]),
        )


class LarkExportStore:
    def __init__(self, exports_file: Path | None = None) -> None:
        self.exports_file = exports_file or _default_exports_file()

    def load(self) -> dict[str, LarkExport]:
        if not self.exports_file.exists():
            return {}
        rows = json.loads(self.exports_file.read_text(encoding="utf-8"))
        return {row["episode_id"]: LarkExport.from_dict(row) for row in rows}

    def get(self, episode_id: str) -> LarkExport | None:
        return self.load().get(episode_id)

    def save(self, exports: dict[str, LarkExport]) -> None:
        rows = [item.to_dict() for item in exports.values()]
        rows.sort(key=lambda item: item["exported_at"], reverse=True)
        self.exports_file.parent.mkdir(parents=True, exist_ok=True)
        tmp_path = self.exports_file.with_suffix(self.exports_file.suffix + ".tmp")
        tmp_path.write_text(json.dumps(rows, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        tmp_path.replace(self.exports_file)

    def upsert(self, export: LarkExport) -> LarkExport:
        exports = self.load()
        exports[export.episode_id] = export
        self.save(exports)
        return export


class LarkFolderStore:
    def __init__(self, folders_file: Path | None = None) -> None:
        self.folders_file = folders_file or _default_folders_file()

    def load(self) -> dict[str, str]:
        if not self.folders_file.exists():
            return {}
        rows = json.loads(self.folders_file.read_text(encoding="utf-8"))
        return {str(row["folder_name"]): str(row["folder_token"]) for row in rows}

    def get(self, folder_name: str) -> str | None:
        return self.load().get(folder_name)

    def upsert(self, folder_name: str, folder_token: str) -> str:
        folders = self.load()
        folders[folder_name] = folder_token
        rows = [
            {"folder_name": name, "folder_token": token}
            for name, token in sorted(folders.items())
        ]
        self.folders_file.parent.mkdir(parents=True, exist_ok=True)
        tmp_path = self.folders_file.with_suffix(self.folders_file.suffix + ".tmp")
        tmp_path.write_text(json.dumps(rows, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        tmp_path.replace(self.folders_file)
        return folder_token


class LarkExporter:
    def __init__(
        self,
        export_store: LarkExportStore | None = None,
        folder_store: LarkFolderStore | None = None,
        runner: LarkRunner | None = None,
        folder_name: str = DEFAULT_FOLDER_NAME,
        chat_name: str = DEFAULT_CHAT_NAME,
        message_send_as: str = DEFAULT_MESSAGE_SEND_AS,
        chat_doc_perm: str = DEFAULT_CHAT_DOC_PERM,
        tmp_dir: Path | None = None,
    ) -> None:
        self.export_store = export_store or LarkExportStore()
        self.folder_store = folder_store or LarkFolderStore()
        self.runner = runner or _run_lark
        self.folder_name = folder_name
        self.chat_name = chat_name
        self.message_send_as = message_send_as
        self.chat_doc_perm = chat_doc_perm
        self.tmp_dir = tmp_dir or (private_runtime_dir() / "tmp")

    def export_readable(self, episode: Episode, readable_path: Path) -> LarkExport:
        existing = self.export_store.get(episode.id)
        if existing and Path(existing.readable_path) == readable_path and existing.message_id:
            return existing

        folder_token = existing.folder_token if existing else self._ensure_folder()
        chat_id = existing.chat_id if existing else self._find_chat()

        if existing and existing.lark_doc_url:
            lark_doc_url = existing.lark_doc_url
            lark_doc_token = existing.lark_doc_token
        else:
            lark_doc_url, lark_doc_token = self._import_markdown(episode, readable_path, folder_token)

        message_id = existing.message_id if existing else None
        if not message_id:
            if lark_doc_token:
                self._grant_chat_permission(lark_doc_token, chat_id)
            message_id = self._send_group_message(episode, lark_doc_url, chat_id)

        export = LarkExport(
            episode_id=episode.id,
            readable_path=str(readable_path),
            folder_name=self.folder_name,
            folder_token=folder_token,
            chat_name=self.chat_name,
            chat_id=chat_id,
            lark_doc_url=lark_doc_url,
            lark_doc_token=lark_doc_token,
            message_id=message_id,
            exported_at=utc_now_iso(),
        )
        return self.export_store.upsert(export)

    def sync_readable(self, episode: Episode, readable_path: Path) -> LarkExport:
        existing = self.export_store.get(episode.id)
        if not existing or not existing.lark_doc_url:
            return self.export_readable(episode, readable_path)

        self._overwrite_markdown(existing.lark_doc_url, episode, readable_path)
        export = LarkExport(
            episode_id=episode.id,
            readable_path=str(readable_path),
            folder_name=existing.folder_name,
            folder_token=existing.folder_token,
            chat_name=existing.chat_name,
            chat_id=existing.chat_id,
            lark_doc_url=existing.lark_doc_url,
            lark_doc_token=existing.lark_doc_token,
            message_id=existing.message_id,
            exported_at=utc_now_iso(),
        )
        return self.export_store.upsert(export)

    def _ensure_folder(self) -> str:
        cached_token = self._cached_folder_token()
        if cached_token:
            return cached_token

        try:
            result = self.runner(
                [
                    "lark-cli",
                    "drive",
                    "+search",
                    "--as",
                    "user",
                    "--query",
                    self.folder_name,
                    "--doc-types",
                    "folder",
                    "--only-title",
                    "--json",
                ],
                PROJECT_ROOT,
            )
        except RuntimeError as exc:
            if "search:docs:read" not in str(exc):
                raise
        else:
            folder = _find_named_item(result, self.folder_name)
            if folder:
                token = _extract_token(folder, ("folder_token", "token", "file_token", "obj_token"))
                if token:
                    return self._cache_folder_token(token)

        created = self.runner(
            [
                "lark-cli",
                "drive",
                "+create-folder",
                "--as",
                "user",
                "--name",
                self.folder_name,
                "--json",
            ],
            PROJECT_ROOT,
        )
        token = _extract_token(created, ("folder_token", "token", "file_token", "obj_token"))
        if not token:
            raise RuntimeError(f"Created folder but could not read folder token: {created}")
        return self._cache_folder_token(token)

    def _cached_folder_token(self) -> str | None:
        token = self.folder_store.get(self.folder_name)
        if token:
            return token
        for export in self.export_store.load().values():
            if export.folder_name == self.folder_name and export.folder_token:
                return self._cache_folder_token(export.folder_token)
        return None

    def _cache_folder_token(self, folder_token: str) -> str:
        return self.folder_store.upsert(self.folder_name, folder_token)

    def _find_chat(self) -> str:
        if not self.chat_name.strip():
            raise RuntimeError(
                "PODCAST_TRACKER_LARK_CHAT_NAME is required when Feishu/Lark export is enabled."
            )
        result = self.runner(
            [
                "lark-cli",
                "im",
                "+chat-search",
                "--as",
                "user",
                "--query",
                self.chat_name,
                "--chat-modes",
                "group",
                "--page-size",
                "10",
                "--json",
            ],
            PROJECT_ROOT,
        )
        chat = _find_named_item(result, self.chat_name)
        if not chat:
            raise RuntimeError(f"Could not find Feishu group chat: {self.chat_name}")
        chat_id = _extract_token(chat, ("chat_id", "open_chat_id", "id"))
        if not chat_id:
            raise RuntimeError(f"Found chat but could not read chat_id: {chat}")
        return chat_id

    def _import_markdown(
        self,
        episode: Episode,
        readable_path: Path,
        folder_token: str,
    ) -> tuple[str, str | None]:
        safe_file = _copy_to_safe_import_path(episode, readable_path, self.tmp_dir)
        result = self.runner(
            [
                "lark-cli",
                "drive",
                "+import",
                "--as",
                "user",
                "--type",
                "docx",
                "--folder-token",
                folder_token,
                "--file",
                safe_file.name,
                "--name",
                _document_name(episode),
                "--json",
            ],
            safe_file.parent,
        )
        url = _extract_url(result)
        token = _extract_token(result, ("doc_token", "document_id", "file_token", "token", "obj_token"))
        if not url:
            raise RuntimeError(f"Imported readable file but could not read document URL: {result}")
        return url, token

    def _overwrite_markdown(self, lark_doc_url: str, episode: Episode, readable_path: Path) -> None:
        safe_file = _copy_to_safe_import_path(episode, readable_path, self.tmp_dir)
        self.runner(
            [
                "lark-cli",
                "docs",
                "+update",
                "--api-version",
                "v2",
                "--doc",
                lark_doc_url,
                "--command",
                "overwrite",
                "--doc-format",
                "markdown",
                "--content",
                f"@{safe_file.name}",
                "--as",
                "user",
                "--json",
            ],
            safe_file.parent,
        )

    def _grant_chat_permission(self, lark_doc_token: str, chat_id: str) -> None:
        payload = {
            "member_type": "openchat",
            "member_id": chat_id,
            "perm": self.chat_doc_perm,
            "type": "chat",
        }
        self.runner(
            [
                "lark-cli",
                "drive",
                "permission.members",
                "create",
                "--token",
                lark_doc_token,
                "--type",
                "docx",
                "--data",
                json.dumps(payload, ensure_ascii=False, separators=(",", ":")),
                "--as",
                "user",
                "--yes",
                "--format",
                "json",
            ],
            PROJECT_ROOT,
        )

    def _send_group_message(self, episode: Episode, lark_doc_url: str, chat_id: str) -> str | None:
        markdown = (
            f"**播客文稿已生成**\n\n"
            f"[{episode.title}]({lark_doc_url})\n\n"
            f"节目：{episode.program_title}\n"
            f"原始链接：{episode.source_url}"
        )
        result = self.runner(
            [
                "lark-cli",
                "im",
                "+messages-send",
                "--as",
                self.message_send_as,
                "--chat-id",
                chat_id,
                "--markdown",
                markdown,
                "--idempotency-key",
                f"podcast-readable-{episode.id}",
                "--json",
            ],
            PROJECT_ROOT,
        )
        return _extract_token(result, ("message_id", "msg_id", "id"))


def _run_lark(command: list[str], cwd: Path) -> dict[str, Any]:
    resolved_command = _with_resolved_lark_cli(command)
    try:
        completed = subprocess.run(
            resolved_command,
            cwd=str(cwd),
            capture_output=True,
            text=True,
            check=False,
            timeout=LARK_CLI_TIMEOUT_SECONDS,
        )
    except subprocess.TimeoutExpired as exc:
        action = " ".join(command[1:3]) if len(command) >= 3 else command[0]
        raise RuntimeError(f"lark-cli {action} timed out after {exc.timeout} seconds") from exc
    except OSError as exc:
        if command and command[0] == "lark-cli":
            raise RuntimeError(
                "找不到 lark-cli。请确认已安装，或设置 PODCAST_TRACKER_LARK_CLI_BIN "
                "为 lark-cli 的绝对路径。"
            ) from exc
        raise
    if completed.returncode != 0:
        detail = (completed.stderr or completed.stdout or "").strip()
        raise RuntimeError(detail or f"lark-cli exited with status {completed.returncode}")
    text = (completed.stdout or "").strip()
    if not text:
        return {}
    try:
        return json.loads(text)
    except json.JSONDecodeError as exc:
        raise RuntimeError(f"lark-cli returned non-JSON output: {text}") from exc


def _with_resolved_lark_cli(command: list[str]) -> list[str]:
    if not command or command[0] != "lark-cli":
        return command
    return [_resolve_lark_cli_bin(), *command[1:]]


def _resolve_lark_cli_bin() -> str:
    override = os.getenv("PODCAST_TRACKER_LARK_CLI_BIN")
    if override:
        return override

    candidates: list[Path] = []
    found = shutil.which("lark-cli")
    if found:
        candidates.append(Path(found))

    nvm_root = Path.home() / ".nvm" / "versions" / "node"
    if nvm_root.exists():
        candidates.extend(sorted(nvm_root.glob("*/bin/lark-cli"), reverse=True))

    candidates.extend([Path("/opt/homebrew/bin/lark-cli"), Path("/usr/local/bin/lark-cli")])
    for candidate in candidates:
        if candidate.exists() and os.access(candidate, os.X_OK):
            return str(candidate)
    return "lark-cli"


def _private_runtime_dir() -> Path:
    return private_runtime_dir()


def _default_exports_file() -> Path:
    return _private_runtime_dir() / "lark_exports.json"


def _default_folders_file() -> Path:
    return _private_runtime_dir() / "lark_folders.json"


def _copy_to_safe_import_path(episode: Episode, readable_path: Path, tmp_dir: Path) -> Path:
    tmp_dir.mkdir(parents=True, exist_ok=True)
    safe_path = tmp_dir / f"{episode.id}-{_safe_name(episode.title)}.md"
    safe_path.write_text(render_readable_markdown(readable_path), encoding="utf-8")
    return safe_path


def _document_name(episode: Episode) -> str:
    return _safe_name(f"{episode.program_title}-{episode.title}", max_length=90)


def _safe_name(value: str, max_length: int = 120) -> str:
    cleaned = re.sub(r"[\\/:*?\"<>|\n\r\t]+", "-", value)
    cleaned = re.sub(r"\s+", " ", cleaned).strip(" .-")
    return (cleaned or "podcast-readable")[:max_length].rstrip(" .-")


def _find_named_item(payload: Any, name: str) -> dict[str, Any] | None:
    for item in _walk_dicts(payload):
        values = [
            str(item.get(key) or "").strip()
            for key in ("name", "title", "chat_name", "file_name")
        ]
        if name in values:
            return item
    for item in _walk_dicts(payload):
        values = [
            str(item.get(key) or "").strip()
            for key in ("name", "title", "chat_name", "file_name")
        ]
        if any(name in value for value in values):
            return item
    return None


def _extract_token(payload: Any, keys: tuple[str, ...]) -> str | None:
    for item in _walk_dicts(payload):
        for key in keys:
            value = item.get(key)
            if isinstance(value, str) and value.strip():
                return value.strip()
    return None


def _extract_url(payload: Any) -> str | None:
    for item in _walk_dicts(payload):
        for key in ("url", "doc_url", "document_url", "lark_doc_url", "web_url"):
            value = item.get(key)
            if isinstance(value, str) and value.startswith(("http://", "https://")):
                return value
    return None


def _walk_dicts(value: Any):
    if isinstance(value, dict):
        yield value
        for child in value.values():
            yield from _walk_dicts(child)
    elif isinstance(value, list):
        for item in value:
            yield from _walk_dicts(item)
