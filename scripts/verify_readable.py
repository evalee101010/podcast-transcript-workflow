#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import re
import sys
import unicodedata
from dataclasses import dataclass
from difflib import SequenceMatcher
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from podcast_tracker.glossary import default_glossary_paths
from podcast_tracker.glossary import load_glossary as _load_shared_glossary


RETENTION_THRESHOLD = 0.85
# Advisory thresholds for the audit pass (warnings, not hard failures unless --strict).
REVERSE_RETENTION_WARN = 0.60  # readable chars matching back to original; low => added/rewritten
UNCERTAINTY_DENSITY_WARN = 8.0  # [?] markers per 1000 meaningful chars
ORPHAN_SPEAKER_MAX = 2  # unmapped "说话人 N" appearing this few times => flag as orphan/montage
GIBBERISH_CJK_RUN = 90  # CJK chars with no punctuation in between => likely merged ASR
PURE_BACKCHANNELS = {
    "嗯",
    "呃",
    "啊",
    "哦",
    "嗯嗯",
    "嗯嗯嗯",
    "嗯啊",
    "啊啊",
    "哦哦",
    "哎",
    "哎哎",
    "唉",
    "欸",
    "呐",
}
# High-confidence residual fillers that should not survive into a clean read.
# Single 啊/哦/哈/对 are legitimate sentence particles and are intentionally NOT listed here.
RESIDUAL_FILLER_RE = re.compile(r"嗯+啊*|呃+|唉+|欸+|啊啊+|哦哦+|哎哎+|啊这个")
# Editor annotations that must never appear in the transcript body (use [?] instead).
EDITOR_NOTE_PATTERNS = (
    "听不清",
    "听不出",
    "听不到",
    "听辨不清",
    "辨认不清",
    "无法辨认",
    "无法识别",
    "此处存疑",
    "音频不清",
    "原文不清",
)
UNCERTAINTY_RE = re.compile(r"\[\?\]")
READABLE_TIME_RE = re.compile(r"^\d{1,2}:\d{2}(?::\d{2})?$")
# Isolated lowercase latin letter surrounded by non-letters (e.g. "阿里巴 a 是 bo 国").
STRAY_LATIN_RE = re.compile(r"(?<![A-Za-z0-9])[a-z](?![A-Za-z0-9])")
# Common English words that appear in tech talk and are NOT garbled proper nouns.
# The glossary's `canonical` list supplements this at runtime — grow the glossary, not this set.
COMMON_ENGLISH = {
    "the", "and", "you", "that", "this", "are", "was", "for", "with", "what", "when",
    "how", "why", "who", "will", "can", "one", "two", "all", "out", "now", "new", "use",
    "used", "using", "like", "just", "very", "more", "most", "some", "then", "than",
    "into", "over", "from", "they", "them", "their", "our", "your", "its", "has", "have",
    "had", "not", "but", "get", "got", "make", "made", "work", "works", "working",
    "workflow", "workload", "model", "models", "agent", "agents", "agentic", "skill",
    "skills", "coding", "code", "token", "tokens", "context", "prompt", "prompts",
    "open", "source", "product", "products", "memory", "video", "chat", "inbox",
    "premium", "replay", "record", "timing", "grant", "team", "user", "users", "data",
    "task", "tasks", "tool", "tools", "app", "apps", "web", "site", "page", "click",
    "type", "text", "file", "files", "note", "notes", "email", "order", "level", "human",
    "world", "future", "system", "systems", "company", "companies", "research",
    "exciting", "terminal", "recursive", "automation", "benchmark", "alignment",
    "reasoning", "training", "inference", "feature", "features", "release", "version",
    "google", "computer", "three", "capital", "report", "access", "image", "images",
    "journey", "train", "serve", "dream", "physical", "ultra", "applied", "robotics",
    "robot", "robots", "intelligence", "improvement", "germany", "china", "audio",
    "design", "market", "number", "moment", "second", "minute", "thing", "things",
    "point", "part", "place", "story", "money", "price", "value", "growth", "scale",
    "speed", "power", "energy", "phone", "phones", "screen", "cloud", "server", "servers",
    "network", "dataset", "datasets", "paper", "papers", "chart", "graph", "table",
    "harness", "should", "would", "could", "about", "there", "these", "those", "where",
    "which", "while", "start", "first", "great", "humans",
    # common words that are edit-distance-close to each other or to garbles (kept out of
    # the near-duplicate detector so it surfaces real ASR garbles, not legit English pairs)
    "launch", "lunch", "billion", "million", "impact", "import", "action", "actions",
    "motion", "option", "options", "leader", "leaders", "leave", "space", "spark",
    "dance", "fancy", "class", "classes", "crash", "flash", "advice", "adviser", "advisor",
    "demand", "machine", "magazine", "david", "detection", "prevention", "retention",
    "optimizer", "frontier", "cursor", "coder", "andrew", "german",
    "really", "maybe", "right", "being", "doing", "going", "still", "every", "other",
    "after", "before", "again", "around", "because", "between", "different", "example",
    "important", "interesting", "actually", "basically", "probably", "something",
    "everyone", "someone", "anything", "everything", "together", "business", "industry",
    "customer", "customers", "engineer", "engineering", "developer", "developers",
    "application", "applications", "platform", "platforms", "software", "hardware",
    "internet", "mobile", "digital", "online", "content", "service", "services",
    "program", "project", "projects", "process", "question", "questions", "answer",
    "problem", "problems", "solution", "decision", "function", "structure", "standard",
}
# Warning prefixes that are advisory only — they never fail the build, even under --strict.
ADVISORY_PREFIXES = (
    "possible garbled ASR",
    "unmapped low-count speakers",
    "[?] density high",
    "unknown English tokens",
    "inconsistent spelling variants",
)
FILLER_WORDS = [
    "怎么说呢",
    "你知道吧",
    "就是说",
    "啊这个",
    "我觉得",
    "嗯嗯",
    "对吧",
    "的话",
    "然后",
    "其实",
    "就是",
    "那个",
    "这个",
    "嗯",
    "呃",
    "唉",
    "哎",
    "啊",
    "欸",
    "哦",
    "哈",
    "就",
]
ENGLISH_TOKEN_RE = re.compile(r"[A-Za-z][A-Za-z0-9]*(?:[-'][A-Za-z0-9]+)*")
ALLOWED_MIXED_CASE_TERMS = {
    "ChatGPT",
    "DeepMind",
    "GitHub",
    "LinkedIn",
    "MiniMax",
    "OpenAI",
    "OpenClaw",
    "RoBERTa",
    "SaaS",
    "YouTube",
    "iOS",
    "iPad",
    "iPhone",
    "macOS",
}


@dataclass(frozen=True)
class Block:
    index: int
    speaker: str
    timestamp: str
    text: str


@dataclass
class BlockResult:
    original_index: int
    readable_index: int | None
    speaker: str
    retention: float
    status: str
    detail: str


def parse_original_transcript(path: Path) -> list[Block]:
    lines = path.read_text(encoding="utf-8").splitlines()
    blocks: list[Block] = []
    i = 0
    while i < len(lines):
        line = lines[i]
        if not line.startswith("### "):
            i += 1
            continue
        speaker = line[4:].strip()
        i += 1
        while i < len(lines) and not lines[i].strip():
            i += 1
        timestamp = ""
        if i < len(lines) and lines[i].strip().startswith("["):
            timestamp = lines[i].strip()
            i += 1
        while i < len(lines) and not lines[i].strip():
            i += 1
        text_lines: list[str] = []
        while i < len(lines) and not lines[i].startswith("### "):
            text_lines.append(lines[i])
            i += 1
        blocks.append(
            Block(
                index=len(blocks) + 1,
                speaker=speaker,
                timestamp=timestamp,
                text="\n".join(text_lines).strip(),
            )
        )
    return blocks


READABLE_BLOCK_RE = re.compile(r"^\*\*(?P<speaker>[^*]+)\*\*\s*(?P<time>`[^`]+`)?\s*$")


def parse_readable_transcript(path: Path) -> list[Block]:
    lines = path.read_text(encoding="utf-8").splitlines()
    blocks: list[Block] = []
    current_speaker: str | None = None
    current_timestamp = ""
    current_text: list[str] = []

    def flush() -> None:
        nonlocal current_speaker, current_timestamp, current_text
        if current_speaker is None:
            return
        blocks.append(
            Block(
                index=len(blocks) + 1,
                speaker=current_speaker,
                timestamp=current_timestamp,
                text="\n".join(current_text).strip(),
            )
        )
        current_speaker = None
        current_timestamp = ""
        current_text = []

    for line in lines:
        match = READABLE_BLOCK_RE.match(line.strip())
        if match:
            flush()
            current_speaker = match.group("speaker").strip()
            current_timestamp = (match.group("time") or "").strip("`")
            continue
        if current_speaker is not None:
            current_text.append(line)
    flush()
    return blocks


def parse_speaker_map(raw: str | None) -> dict[str, str]:
    if not raw:
        return {}
    mapping: dict[str, str] = {}
    for part in raw.split(","):
        part = part.strip()
        if not part:
            continue
        if "=" not in part:
            raise ValueError(f"Invalid speaker map entry: {part!r}")
        source, target = part.split("=", 1)
        mapping[source.strip()] = target.strip()
    return mapping


def load_speaker_map_file(path: Path | None) -> dict[str, str]:
    if path is None:
        return {}
    data = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(data, dict):
        raise ValueError(f"Speaker map file must be a JSON object: {path}")
    mapping: dict[str, str] = {}
    for source, target in data.items():
        if not isinstance(source, str) or not isinstance(target, str):
            raise ValueError(f"Speaker map file keys and values must be strings: {path}")
        source = source.strip()
        target = target.strip()
        if source and target:
            mapping[source] = target
    return mapping


def load_glossary(paths: list[Path | None]) -> tuple[set[str], dict[str, str]]:
    """Merge glossary files. Returns (canonical_terms, corrections{garble: canonical})."""
    glossary = _load_shared_glossary(paths)
    return glossary.canonical, glossary.corrections


QnaSplits = dict[int, tuple[list[int], list[int]]]


def load_qna_splits(path: Path | None) -> QnaSplits:
    if path is None:
        return {}
    text = path.read_text(encoding="utf-8").strip()
    if not text:
        return {}

    splits: QnaSplits = {}
    try:
        data = json.loads(text)
    except json.JSONDecodeError:
        data = None
    if isinstance(data, dict):
        data = [data]
    if isinstance(data, list):
        for item in data:
            if not isinstance(item, dict):
                continue
            original_indices = item.get("original_indices") or item.get("originals")
            if not isinstance(original_indices, list):
                original = item.get("original") or item.get("original_index")
                original_indices = [original] if isinstance(original, int) else []
            readable_indices = item.get("readable_indices") or item.get("readable")
            if isinstance(readable_indices, int):
                readable_indices = [readable_indices]
            if isinstance(original_indices, list) and isinstance(readable_indices, list):
                originals = [idx for idx in original_indices if isinstance(idx, int)]
                readable = [idx for idx in readable_indices if isinstance(idx, int)]
                if originals and readable:
                    splits[originals[0]] = (originals, readable)
        return splits

    pattern = re.compile(
        r"original(?:_index)?=(?P<original>\d+).*?"
        r"readable(?:_indices)?=(?P<readable>[0-9,\s]+)"
    )
    for line in text.splitlines():
        match = pattern.search(line)
        if not match:
            continue
        original = int(match.group("original"))
        readable = [int(part) for part in re.split(r"[\s,]+", match.group("readable").strip()) if part]
        if readable:
            splits[original] = ([original], readable)
    return splits


def normalize_qna_splits(
    qna_splits: QnaSplits | dict[int, list[int]] | None,
) -> QnaSplits:
    if not qna_splits:
        return {}
    normalized: QnaSplits = {}
    for original, value in qna_splits.items():
        if (
            isinstance(value, tuple)
            and len(value) == 2
            and isinstance(value[0], list)
            and isinstance(value[1], list)
        ):
            originals = [idx for idx in value[0] if isinstance(idx, int)]
            readable = [idx for idx in value[1] if isinstance(idx, int)]
        else:
            originals = [original]
            readable = [idx for idx in value if isinstance(idx, int)]
        if originals and readable:
            normalized[originals[0]] = (originals, readable)
    return normalized


def strip_punctuation_and_space(text: str) -> str:
    chars: list[str] = []
    for char in text:
        if char.isspace():
            continue
        if unicodedata.category(char).startswith("P"):
            continue
        chars.append(char)
    return "".join(chars)


def compact_text(text: str) -> str:
    return strip_punctuation_and_space(text)


def meaningful_text(text: str) -> str:
    normalized = compact_text(text)
    for word in FILLER_WORDS:
        normalized = normalized.replace(word, "")
    normalized = normalize_chinese_number_terms(normalized)
    return "".join(char for char in normalized if is_cjk(char) or char.isdigit())


def normalize_chinese_number_terms(text: str) -> str:
    replacements = {
        "零": "0",
        "一": "1",
        "二": "2",
        "三": "3",
        "四": "4",
        "五": "5",
        "六": "6",
        "七": "7",
        "八": "8",
        "九": "9",
        "点": "",
    }
    for source, target in replacements.items():
        text = text.replace(source, target)
    return text


def is_cjk(char: str) -> bool:
    return (
        "\u3400" <= char <= "\u4dbf"
        or "\u4e00" <= char <= "\u9fff"
        or "\uf900" <= char <= "\ufaff"
        or "\U00020000" <= char <= "\U0002a6df"
        or "\U0002a700" <= char <= "\U0002b73f"
        or "\U0002b740" <= char <= "\U0002b81f"
        or "\U0002b820" <= char <= "\U0002ceaf"
    )


def is_pure_backchannel(block: Block) -> bool:
    compacted = compact_text(block.text)
    return bool(compacted) and compacted in PURE_BACKCHANNELS


def suspicious_english_capitalization_tokens(text: str) -> list[str]:
    tokens: list[str] = []
    for match in ENGLISH_TOKEN_RE.finditer(text):
        token = match.group(0)
        if is_suspicious_english_capitalization(token):
            tokens.append(token)
    return tokens


def is_suspicious_english_capitalization(token: str) -> bool:
    if len(token) < 4 or token in ALLOWED_MIXED_CASE_TERMS:
        return False
    if token.islower() or token.isupper():
        return False

    for part in re.split(r"[-']", token):
        if len(part) < 4 or part in ALLOWED_MIXED_CASE_TERMS:
            continue
        if part.islower() or part.isupper():
            continue
        if len(part) > 1 and part[:-1].isupper() and part[-1] == "s":
            continue
        if re.search(r"(?:^|[a-z])[A-Z]{2,}[a-z]", part):
            return True
    return False


def retention_ratio(original_text: str, readable_text: str) -> float:
    original = meaningful_text(original_text)
    readable = meaningful_text(readable_text)
    if not original:
        return 1.0
    if not readable:
        return 0.0
    matcher = SequenceMatcher(None, original, readable, autojunk=False)
    matched = sum(match.size for match in matcher.get_matching_blocks())
    return matched / len(original)


def normalize_speaker(speaker: str, speaker_map: dict[str, str]) -> str:
    return speaker_map.get(speaker, speaker)


def reverse_retention(original_text: str, readable_text: str) -> float:
    """How much of the readable maps back to the original (catches added/rewritten content)."""
    original = meaningful_text(original_text)
    readable = meaningful_text(readable_text)
    if not readable:
        return 1.0
    if not original:
        return 0.0
    matcher = SequenceMatcher(None, original, readable, autojunk=False)
    matched = sum(match.size for match in matcher.get_matching_blocks())
    return matched / len(readable)


def residual_filler_hits(text: str) -> list[str]:
    """High-confidence口水词 that should have been removed from a clean read."""
    return RESIDUAL_FILLER_RE.findall(text)


def editor_note_hits(text: str) -> list[str]:
    return [pattern for pattern in EDITOR_NOTE_PATTERNS if pattern in text]


def count_uncertainty_markers(text: str) -> int:
    return len(UNCERTAINTY_RE.findall(text))


def gibberish_signals(text: str) -> list[str]:
    """Conservative heuristics for garbled ASR. Advisory only (precision over recall)."""
    signals: list[str] = []
    # Legit emphatic reduplications (对对对 / 哈哈哈) are not gibberish.
    triples = {match.group(1) for match in re.finditer(r"([一-鿿])\1\1", text)}
    garbled_triples = triples - set("对是好哈嗯啊哦呃笑哎行")
    if garbled_triples:
        signals.append("char×3 " + "/".join(repr(char) for char in sorted(garbled_triples)))
    longest = run = 0
    for char in text:
        if is_cjk(char):
            run += 1
            longest = max(longest, run)
        elif char.isspace():
            continue
        else:
            run = 0
    if longest >= GIBBERISH_CJK_RUN:
        signals.append(f"no-punct CJK run {longest}")
    strays = STRAY_LATIN_RE.findall(text)
    if len(strays) >= 3:
        signals.append(f"stray latin ×{len(strays)}")
    return signals


def english_tokens(text: str) -> list[str]:
    return [match.group(0) for match in ENGLISH_TOKEN_RE.finditer(text)]


def known_error_hits(text: str, corrections: dict[str, str]) -> list[tuple[str, str]]:
    """Glossary garbles found still-uncorrected in the readable text."""
    hits: list[tuple[str, str]] = []
    for garble, canon in corrections.items():
        if re.search(r"[一-鿿]", garble):
            if garble in text:
                hits.append((garble, canon))
        elif re.search(rf"(?<![A-Za-z0-9]){re.escape(garble)}(?![A-Za-z0-9])", text, re.IGNORECASE):
            hits.append((garble, canon))
    return hits


def _levenshtein(a: str, b: str, cap: int = 2) -> int:
    if abs(len(a) - len(b)) > cap:
        return cap + 1
    prev = list(range(len(b) + 1))
    for i, ca in enumerate(a, 1):
        cur = [i]
        for j, cb in enumerate(b, 1):
            cur.append(min(prev[j] + 1, cur[j - 1] + 1, prev[j - 1] + (ca != cb)))
        prev = cur
    return prev[-1]


def near_duplicate_clusters(tokens: list[str]) -> list[list[str]]:
    """Low-frequency English tokens within edit distance 2 — likely one garbled term."""
    from collections import Counter

    counts = Counter(token.lower() for token in tokens if len(token) >= 6)
    lowfreq = [token for token, n in counts.items() if n <= 3]
    clusters: list[list[str]] = []
    used: set[str] = set()
    for i, a in enumerate(lowfreq):
        if a in used:
            continue
        group = [a]
        for b in lowfreq[i + 1:]:
            if b not in used and 0 < _levenshtein(a, b, 2) <= 2:
                group.append(b)
                used.add(b)
        if len(group) >= 2:
            used.add(a)
            clusters.append(group)
    return clusters


def glossary_candidates(
    readable_blocks: list[Block],
    glossary: tuple[set[str], dict[str, str]],
) -> dict[str, list]:
    from collections import Counter, defaultdict

    canonical, corrections = glossary
    allowed = allowed_english_terms(canonical, corrections)
    token_counts: Counter[str] = Counter()
    token_blocks: dict[str, set[int]] = defaultdict(set)
    all_tokens: list[str] = []
    for block in readable_blocks:
        for token in english_tokens(block.text):
            all_tokens.append(token)
            low = token.lower()
            if len(low) < 5 or token.isupper() or low in allowed or token in ALLOWED_MIXED_CASE_TERMS:
                continue
            token_counts[low] += 1
            token_blocks[low].add(block.index)

    unknown_tokens = [
        {
            "token": token,
            "count": count,
            "blocks": sorted(token_blocks[token]),
        }
        for token, count in token_counts.most_common(100)
    ]
    clusters = [
        sorted(cluster)
        for cluster in near_duplicate_clusters([token for token in all_tokens if token.lower() not in allowed])
    ]
    return {
        "unknown_tokens": unknown_tokens,
        "variant_clusters": clusters,
    }


def allowed_english_terms(canonical: set[str], corrections: dict[str, str]) -> set[str]:
    allowed = set(COMMON_ENGLISH)
    for term in list(canonical) + list(corrections.values()):
        allowed.add(term.lower())  # keep hyphenated whole tokens like "swe-bench"
        for part in re.split(r"[^A-Za-z0-9]+", term):
            if part:
                allowed.add(part.lower())
    for garble in corrections:  # correction keys are reported by known_error, not as "unknown"
        if not re.search(r"[\u4e00-\u9fff]", garble):
            allowed.add(garble.lower())
    return allowed


def collect_warnings(
    original_blocks: list[Block],
    readable_blocks: list[Block],
    results: list[BlockResult],
    speaker_map: dict[str, str] | None = None,
    glossary: tuple[set[str], dict[str, str]] | None = None,
) -> list[str]:
    """Advisory audit pass. Operationalizes the spec's readability/quality rules.

    These are signals for human review, not fidelity failures; they do not change the
    exit code unless --strict is passed.
    """
    warnings: list[str] = []

    # 1) residual fillers / backchannel left inside readable text (under-cleaning)
    filler_blocks: list[tuple[int, list[str]]] = []
    total_fillers = 0
    for block in readable_blocks:
        hits = residual_filler_hits(block.text)
        if hits:
            total_fillers += len(hits)
            filler_blocks.append((block.index, hits))
    if total_fillers:
        sample = "; ".join(f"#{idx}:{'/'.join(hits)}" for idx, hits in filler_blocks[:10])
        more = "" if len(filler_blocks) <= 10 else f" (+{len(filler_blocks) - 10} more blocks)"
        warnings.append(
            f"residual filler/backchannel ×{total_fillers} in {len(filler_blocks)} blocks: {sample}{more}"
        )

    # 2) editor notes in body (forbidden — use [?] instead)
    for block in readable_blocks:
        notes = editor_note_hits(block.text)
        if notes:
            warnings.append(f"editor note in body at block {block.index}: {', '.join(notes)}")

    # 3) [?] uncertainty density (high => source ASR likely poor, needs re-listen)
    total_uncertain = sum(count_uncertainty_markers(block.text) for block in readable_blocks)
    meaningful_chars = sum(len(meaningful_text(block.text)) for block in readable_blocks) or 1
    density = total_uncertain / meaningful_chars * 1000
    if density >= UNCERTAINTY_DENSITY_WARN:
        warnings.append(
            f"[?] density high: {total_uncertain} markers ({density:.1f}/1000 chars) — source ASR likely poor"
        )

    # 4) garbled ASR signals
    gibberish_blocks: list[tuple[int, list[str]]] = []
    for block in readable_blocks:
        signals = gibberish_signals(block.text)
        if signals:
            gibberish_blocks.append((block.index, signals))
    if gibberish_blocks:
        sample = "; ".join(f"#{idx}:{','.join(sig)}" for idx, sig in gibberish_blocks[:10])
        more = "" if len(gibberish_blocks) <= 10 else f" (+{len(gibberish_blocks) - 10} more)"
        warnings.append(f"possible garbled ASR in {len(gibberish_blocks)} blocks: {sample}{more}")

    # 5) orphan / montage speakers: unmapped "说话人 N" appearing very few times
    counts: dict[str, int] = {}
    for block in readable_blocks:
        if is_pure_backchannel(block):
            continue
        counts[block.speaker] = counts.get(block.speaker, 0) + 1
    orphans = [
        (speaker, count)
        for speaker, count in counts.items()
        if re.fullmatch(r"说话人\s*\d+(?:\[\?\])?", speaker) and count <= ORPHAN_SPEAKER_MAX
    ]
    if orphans:
        sample = ", ".join(f"{speaker}×{count}" for speaker, count in sorted(orphans))
        warnings.append(
            f"unmapped low-count speakers (diarization orphan or montage voice?): {sample}"
        )

    # 6) reverse retention: readable that does not map back to original => added/rewritten
    original_by_index = {block.index: block for block in original_blocks}
    readable_by_index = {block.index: block for block in readable_blocks}
    for result in results:
        if result.readable_index is None or result.detail.startswith("qna_split"):
            continue
        original = original_by_index.get(result.original_index)
        readable = readable_by_index.get(result.readable_index)
        if original is None or readable is None:
            continue
        if len(meaningful_text(readable.text)) < 20:
            continue
        reverse = reverse_retention(original.text, readable.text)
        if reverse < REVERSE_RETENTION_WARN:
            warnings.append(
                f"block {result.original_index}->{result.readable_index} reverse retention "
                f"{reverse:.2f} — possible added/rewritten content"
            )

    # 7) timestamp lint
    for block in readable_blocks:
        if is_pure_backchannel(block):
            continue
        if not block.timestamp:
            warnings.append(f"block {block.index} ({block.speaker}) missing timestamp")
        elif not READABLE_TIME_RE.match(block.timestamp):
            warnings.append(f"block {block.index} bad timestamp format: {block.timestamp!r}")

    # 8) glossary-driven 错字 audit (only when a glossary is loaded)
    if glossary is not None:
        canonical, corrections = glossary
        for block in readable_blocks:
            for garble, canon in known_error_hits(block.text, corrections):
                warnings.append(
                    f"known ASR error at block {block.index}: {garble!r} -> {canon!r} (not corrected)"
                )
        candidates = glossary_candidates(readable_blocks, glossary)
        unknown_tokens = candidates["unknown_tokens"]
        if unknown_tokens:
            sample = ", ".join(
                f"{item['token']}×{item['count']}" for item in unknown_tokens[:25]
            )
            warnings.append(
                "unknown English tokens (not in glossary; likely garbled proper nouns or new terms): "
                + sample
            )
        clusters = candidates["variant_clusters"]
        for cluster in clusters[:15]:
            warnings.append(
                "inconsistent spelling variants (likely one garbled term): " + "/".join(sorted(cluster))
            )
        if len(clusters) > 15:
            warnings.append(f"inconsistent spelling variants: +{len(clusters) - 15} more clusters")

    return warnings


def verify(
    original_blocks: list[Block],
    readable_blocks: list[Block],
    speaker_map: dict[str, str] | None = None,
    qna_splits: QnaSplits | dict[int, list[int]] | None = None,
) -> tuple[bool, list[str], list[BlockResult]]:
    speaker_map = speaker_map or {}
    qna_splits = normalize_qna_splits(qna_splits)
    errors: list[str] = []
    results: list[BlockResult] = []

    original_active = [block for block in original_blocks if not is_pure_backchannel(block)]
    readable_active = [block for block in readable_blocks if not is_pure_backchannel(block)]
    readable_by_index = {block.index: block for block in readable_active}

    original_pos = 0
    readable_pos = 0
    while original_pos < len(original_active):
        original = original_active[original_pos]
        mapped_speaker = normalize_speaker(original.speaker, speaker_map)

        if original.index in qna_splits:
            original_indices, split_indices = qna_splits[original.index]
            grouped_original = original_active[original_pos : original_pos + len(original_indices)]
            actual_original_indices = [block.index for block in grouped_original]
            split_blocks = [readable_by_index[idx] for idx in split_indices if idx in readable_by_index]
            expected_indices = [block.index for block in readable_active[readable_pos : readable_pos + len(split_blocks)]]
            if actual_original_indices != original_indices:
                errors.append(
                    f"Q/A split original order mismatch at block {original.index}: "
                    f"expected original blocks {original_indices}, got {actual_original_indices}"
                )
            if len(split_blocks) != len(split_indices):
                errors.append(
                    f"Q/A split log for original block {original.index} references missing readable blocks: {split_indices}"
                )
            elif expected_indices != split_indices:
                errors.append(
                    f"Q/A split order mismatch for original block {original.index}: expected next readable blocks {split_indices}, got {expected_indices}"
                )
            combined_original_text = "\n".join(block.text for block in grouped_original)
            combined_text = "\n".join(block.text for block in split_blocks)
            retention = retention_ratio(combined_original_text, combined_text)
            status = "ok" if retention >= RETENTION_THRESHOLD else "suspicious"
            if status != "ok":
                errors.append(
                    f"Block {original.index} Q/A split retention {retention:.2f} below {RETENTION_THRESHOLD:.2f}"
                )
            results.append(
                BlockResult(
                    original_index=original.index,
                    readable_index=split_indices[0] if split_indices else None,
                    speaker=mapped_speaker,
                    retention=retention,
                    status=status,
                    detail=f"qna_split original={original_indices} readable={split_indices}",
                )
            )
            readable_pos += len(split_blocks)
            original_pos += len(grouped_original)
            continue

        if readable_pos >= len(readable_active):
            errors.append(f"Missing readable block for original block {original.index} ({mapped_speaker})")
            results.append(
                BlockResult(
                    original_index=original.index,
                    readable_index=None,
                    speaker=mapped_speaker,
                    retention=0.0,
                    status="missing",
                    detail="no readable block",
                )
            )
            original_pos += 1
            continue

        readable = readable_active[readable_pos]
        if mapped_speaker != readable.speaker:
            errors.append(
                f"Speaker mismatch at original block {original.index}/readable block {readable.index}: "
                f"{mapped_speaker!r} != {readable.speaker!r}"
            )

        retention = retention_ratio(original.text, readable.text)
        status = "ok" if retention >= RETENTION_THRESHOLD else "suspicious"
        if status != "ok":
            errors.append(
                f"Block {original.index} retention {retention:.2f} below {RETENTION_THRESHOLD:.2f}"
            )
        results.append(
            BlockResult(
                original_index=original.index,
                readable_index=readable.index,
                speaker=mapped_speaker,
                retention=retention,
                status=status,
                detail="",
            )
        )
        original_pos += 1
        readable_pos += 1

    if readable_pos < len(readable_active):
        extra = readable_active[readable_pos:]
        errors.append(
            "Extra readable blocks after alignment: "
            + ", ".join(f"{block.index}:{block.speaker}" for block in extra)
        )

    for block in readable_blocks:
        for token in suspicious_english_capitalization_tokens(block.text):
            errors.append(
                f"Readable block {block.index} suspicious English capitalization: {token!r}"
            )

    return not errors, errors, results


def build_report(
    original_path: Path,
    readable_path: Path,
    original_blocks: list[Block],
    readable_blocks: list[Block],
    errors: list[str],
    results: list[BlockResult],
    warnings: list[str] | None = None,
) -> str:
    ignored_original = sum(1 for block in original_blocks if is_pure_backchannel(block))
    ignored_readable = sum(1 for block in readable_blocks if is_pure_backchannel(block))
    active_original = len(original_blocks) - ignored_original
    active_readable = len(readable_blocks) - ignored_readable
    lines = [
        "verify_readable.py report",
        f"original: {original_path}",
        f"readable: {readable_path}",
        f"blocks: original={len(original_blocks)} readable={len(readable_blocks)}",
        f"active_blocks: original={active_original} readable={active_readable}",
        f"ignored_pure_backchannel: original={ignored_original} readable={ignored_readable}",
    ]
    if results:
        lowest = min(result.retention for result in results)
        average = sum(result.retention for result in results) / len(results)
        lines.append(f"retention: min={lowest:.3f} average={average:.3f} threshold={RETENTION_THRESHOLD:.2f}")
    if errors:
        lines.append("status: FAIL")
        lines.append("errors:")
        lines.extend(f"- {error}" for error in errors)
        suspicious = [result for result in results if result.status != "ok"]
        if suspicious:
            lines.append("suspicious_blocks:")
            lines.extend(
                "- original={0} readable={1} speaker={2} retention={3:.3f} {4}".format(
                    result.original_index,
                    result.readable_index if result.readable_index is not None else "-",
                    result.speaker,
                    result.retention,
                    result.detail,
                ).rstrip()
                for result in suspicious[:50]
            )
            if len(suspicious) > 50:
                lines.append(f"- ... {len(suspicious) - 50} more")
    else:
        lines.append("status: PASS")

    if warnings:
        blocking = sum(1 for warning in warnings if not warning.startswith(ADVISORY_PREFIXES))
        lines.append(
            f"warnings: {len(warnings)} ({blocking} blocking under --strict, "
            f"{len(warnings) - blocking} advisory)"
        )
        for warning in warnings:
            mark = "!" if not warning.startswith(ADVISORY_PREFIXES) else "~"
            lines.append(f"{mark} {warning}")
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Verify a podcast readable transcript against its source transcript."
    )
    parser.add_argument("original", type=Path)
    parser.add_argument("readable", type=Path)
    parser.add_argument(
        "--speaker-map",
        help="Comma-separated mapping from original speaker labels to readable labels, e.g. '说话人 4=课代表'",
    )
    parser.add_argument(
        "--speaker-map-file",
        type=Path,
        help="Optional JSON object mapping original speaker labels to readable labels.",
    )
    parser.add_argument(
        "--qna-log",
        type=Path,
        help="Optional JSON/line log for high-confidence Q/A split exceptions.",
    )
    parser.add_argument(
        "--glossary",
        type=Path,
        action="append",
        help="Extra glossary JSON (canonical + corrections). Repeatable. The repo glossary.json "
        "and a same-name <readable-stem>.glossary.json are auto-loaded.",
    )
    parser.add_argument(
        "--strict",
        action="store_true",
        help="Treat blocking advisory warnings (under-cleaning, known ASR errors, added content, "
        "timestamps, editor notes) as failures. Pure source-quality signals stay advisory.",
    )
    parser.add_argument(
        "--emit-glossary-candidates",
        type=Path,
        help="Write unknown English tokens and near-duplicate spelling clusters to this JSON file.",
    )
    args = parser.parse_args(argv)

    try:
        speaker_map = load_speaker_map_file(args.speaker_map_file)
        speaker_map.update(parse_speaker_map(args.speaker_map))
        qna_splits = load_qna_splits(args.qna_log)
        glossary_paths = default_glossary_paths(args.readable)
        glossary_paths.extend(args.glossary or [])
        glossary = load_glossary(glossary_paths)
        original_blocks = parse_original_transcript(args.original)
        readable_blocks = parse_readable_transcript(args.readable)
        ok, errors, results = verify(
            original_blocks,
            readable_blocks,
            speaker_map=speaker_map,
            qna_splits=qna_splits,
        )
        warnings = collect_warnings(
            original_blocks,
            readable_blocks,
            results,
            speaker_map=speaker_map,
            glossary=glossary,
        )
        if args.emit_glossary_candidates:
            args.emit_glossary_candidates.write_text(
                json.dumps(
                    glossary_candidates(readable_blocks, glossary),
                    ensure_ascii=False,
                    indent=2,
                )
                + "\n",
                encoding="utf-8",
            )
    except Exception as exc:
        print(f"verify_readable.py error: {exc}", file=sys.stderr)
        return 2

    print(
        build_report(
            args.original,
            args.readable,
            original_blocks,
            readable_blocks,
            errors,
            results,
            warnings,
        )
    )
    if not ok:
        return 1
    blocking = [w for w in warnings if not w.startswith(ADVISORY_PREFIXES)]
    if args.strict and blocking:
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
