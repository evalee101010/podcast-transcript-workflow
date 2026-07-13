from __future__ import annotations

import hashlib
import json
import re
import threading
import uuid
from dataclasses import dataclass
from pathlib import Path

from .models import utc_now_iso


SPEAKER_LINE_RE = re.compile(r"^\*\*(?P<speaker>[^*]+)\*\*\s*(?P<time>`[^`]+`)?\s*$")
PATCH_STATUS_VISIBLE = {"approved", "published"}

# Serializes load→append→write cycles on patch sidecar files (ThreadingHTTPServer
# handles requests concurrently; without this, concurrent saves lose patches).
_PATCH_WRITE_LOCK = threading.Lock()


class PatchBaselineMismatch(ValueError):
    """Patches were recorded against a different readable baseline.

    Subclasses ValueError so the web patch endpoint maps it to HTTP 409.
    """


@dataclass(frozen=True)
class ReadableBlock:
    index: int
    block_id: str
    speaker: str
    timestamp: str
    header_line: str
    header_line_index: int
    content_start_line: int
    content_end_line: int
    text: str
    body_hash: str


@dataclass(frozen=True)
class ParsedReadable:
    lines: list[str]
    blocks: list[ReadableBlock]
    trailing_newline: bool


def patches_path_for_readable(readable_path: Path) -> Path:
    return readable_path.with_name(readable_path.stem + ".patches.json")


def parse_readable_markdown(markdown: str) -> ParsedReadable:
    lines = markdown.splitlines()
    blocks: list[ReadableBlock] = []
    trailing_newline = markdown.endswith("\n")
    header_indices: list[int] = [
        index
        for index, line in enumerate(lines)
        if SPEAKER_LINE_RE.match(line.strip())
    ]

    for block_index, header_index in enumerate(header_indices, start=1):
        next_header = header_indices[block_index] if block_index < len(header_indices) else len(lines)
        body_start = header_index + 1
        body_end = next_header
        content_start = body_start
        while content_start < body_end and not lines[content_start].strip():
            content_start += 1
        content_end = body_end
        while content_end > content_start and not lines[content_end - 1].strip():
            content_end -= 1
        text = "\n".join(lines[content_start:content_end])
        match = SPEAKER_LINE_RE.match(lines[header_index].strip())
        speaker = match.group("speaker").strip() if match else ""
        timestamp = (match.group("time") or "").strip("`") if match else ""
        blocks.append(
            ReadableBlock(
                index=block_index,
                block_id=f"block_{block_index:04d}",
                speaker=speaker,
                timestamp=timestamp,
                header_line=lines[header_index],
                header_line_index=header_index,
                content_start_line=content_start,
                content_end_line=content_end,
                text=text,
                body_hash=hash_body_text(text),
            )
        )

    return ParsedReadable(lines=lines, blocks=blocks, trailing_newline=trailing_newline)


def hash_body_text(text: str) -> str:
    return hashlib.sha256(normalize_body_text(text).encode("utf-8")).hexdigest()


def hash_baseline_markdown(markdown: str) -> str:
    normalized = markdown.replace("\r\n", "\n").replace("\r", "\n")
    return hashlib.sha256(normalized.encode("utf-8")).hexdigest()


def normalize_body_text(text: str) -> str:
    return text.replace("\r\n", "\n").replace("\r", "\n").strip("\n")


def load_patch_file(readable_path: Path) -> tuple[list[dict], str | None]:
    """Return (events, baseline_hash). Legacy files without baseline_hash return None."""
    path = patches_path_for_readable(readable_path)
    if not path.exists():
        return [], None
    data = json.loads(path.read_text(encoding="utf-8"))
    if isinstance(data, list):
        return [event for event in data if isinstance(event, dict)], None
    if not isinstance(data, dict):
        raise ValueError(f"Patch file must be a JSON object: {path}")
    events = data.get("patches", [])
    if not isinstance(events, list):
        raise ValueError(f"Patch file patches must be a list: {path}")
    baseline_hash = data.get("baseline_hash")
    return (
        [event for event in events if isinstance(event, dict)],
        str(baseline_hash) if baseline_hash else None,
    )


def load_patch_events(readable_path: Path) -> list[dict]:
    return load_patch_file(readable_path)[0]


def _write_patch_events_locked(
    readable_path: Path,
    events: list[dict],
    baseline_hash: str | None,
) -> None:
    path = patches_path_for_readable(readable_path)
    payload = {
        "version": 1,
        "readable_path": str(readable_path),
        "baseline_hash": baseline_hash,
        "updated_at": utc_now_iso(),
        "patches": events,
    }
    tmp_path = path.with_suffix(path.suffix + ".tmp")
    tmp_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    tmp_path.replace(path)


def write_patch_events(
    readable_path: Path,
    events: list[dict],
    baseline_hash: str | None = None,
) -> None:
    with _PATCH_WRITE_LOCK:
        if baseline_hash is None:
            _, baseline_hash = load_patch_file(readable_path)
        _write_patch_events_locked(readable_path, events, baseline_hash)


def visible_body_patches(events: list[dict]) -> dict[str, dict]:
    active: dict[str, dict] = {}
    for event in events:
        if event.get("field") != "body":
            continue
        if event.get("status") not in PATCH_STATUS_VISIBLE:
            continue
        block_id = str(event.get("block_id") or "").strip()
        after_text = event.get("after_text")
        if not block_id or not isinstance(after_text, str):
            continue
        active[block_id] = event
    return active


def patch_events_can_apply_safely(markdown: str, events: list[dict]) -> bool:
    """Return true when body patch history still lines up with this baseline.

    The readable file can change outside the locally approved edits, for example
    when glossary normalization touches the title or metadata. In that case the
    whole-file baseline hash changes, but the existing block patches are still
    safe as long as each block's patch chain matches its current body hash.
    """
    parsed = parse_readable_markdown(markdown)
    block_hashes = {block.block_id: block.body_hash for block in parsed.blocks}
    for event in events:
        if event.get("field") != "body":
            continue
        if event.get("status") not in PATCH_STATUS_VISIBLE:
            continue
        block_id = str(event.get("block_id") or "").strip()
        before_hash = str(event.get("before_hash") or "").strip()
        after_hash = str(event.get("after_hash") or "").strip()
        if not block_id or not before_hash or not after_hash:
            return False
        current_hash = block_hashes.get(block_id)
        if current_hash is None:
            return False
        if before_hash == current_hash:
            block_hashes[block_id] = after_hash
            continue
        if after_hash == current_hash:
            block_hashes[block_id] = after_hash
            continue
        return False
    return True


def apply_patch_events_to_markdown(markdown: str, events: list[dict]) -> str:
    parsed = parse_readable_markdown(markdown)
    active = visible_body_patches(events)
    if not active:
        return markdown
    lines = list(parsed.lines)
    for block in reversed(parsed.blocks):
        event = active.get(block.block_id)
        if not event:
            continue
        replacement = normalize_body_text(str(event.get("after_text") or "")).split("\n")
        if replacement == [""]:
            replacement = []
        lines[block.content_start_line : block.content_end_line] = replacement
    rendered = "\n".join(lines)
    if parsed.trailing_newline:
        rendered += "\n"
    return rendered


def render_readable_markdown(readable_path: Path) -> str:
    markdown = readable_path.read_text(encoding="utf-8")
    events, baseline_hash = load_patch_file(readable_path)
    current_hash = hash_baseline_markdown(markdown)
    if (
        events
        and baseline_hash
        and baseline_hash != current_hash
        and not patch_events_can_apply_safely(markdown, events)
    ):
        raise PatchBaselineMismatch(
            f"阅读版基线已变更，历史修订暂不套用以免错位：{readable_path.name}。"
            "请先核对基线文件或迁移同名 .patches.json。"
        )
    return apply_patch_events_to_markdown(markdown, events)


def rendered_blocks_for_readable(readable_path: Path) -> list[ReadableBlock]:
    return parse_readable_markdown(render_readable_markdown(readable_path)).blocks


def replace_rendered_block_body(readable_path: Path, block_id: str, after_text: str) -> str:
    markdown = render_readable_markdown(readable_path)
    parsed = parse_readable_markdown(markdown)
    lines = list(parsed.lines)
    for block in parsed.blocks:
        if block.block_id == block_id:
            replacement = normalize_body_text(after_text).split("\n")
            if replacement == [""]:
                replacement = []
            lines[block.content_start_line : block.content_end_line] = replacement
            rendered = "\n".join(lines)
            if parsed.trailing_newline:
                rendered += "\n"
            return rendered
    raise KeyError(f"Readable block not found: {block_id}")


def append_approved_body_patch(
    readable_path: Path,
    *,
    episode_id: str | None = None,
    block_id: str,
    before_hash: str,
    before_text: str,
    after_text: str,
    author_id: str = "local-owner",
    author_name: str = "Owner",
) -> dict:
    after_text = normalize_body_text(after_text)
    event = {
        "patch_id": str(uuid.uuid4()),
        "episode_id": episode_id,
        "readable_path": str(readable_path),
        "block_id": block_id,
        "field": "body",
        "before_hash": before_hash,
        "before_text": before_text,
        "after_hash": hash_body_text(after_text),
        "after_text": after_text,
        "author": {"id": author_id, "display_name": author_name},
        "status": "approved",
        "created_at": utc_now_iso(),
        "reviewed_at": utc_now_iso(),
        "reviewed_by": author_id,
        "published_at": None,
    }
    with _PATCH_WRITE_LOCK:
        current_markdown = readable_path.read_text(encoding="utf-8")
        current_hash = hash_baseline_markdown(current_markdown)
        events, stored_hash = load_patch_file(readable_path)
        if (
            events
            and stored_hash
            and stored_hash != current_hash
            and not patch_events_can_apply_safely(current_markdown, events)
        ):
            raise PatchBaselineMismatch(
                "阅读版基线已变更，历史修订与当前基线不匹配，已拒绝保存以免错位。"
            )
        events.append(event)
        _write_patch_events_locked(readable_path, events, current_hash)
    return event
