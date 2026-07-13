from __future__ import annotations

import json
import re
from dataclasses import dataclass
from pathlib import Path

from .config import PROJECT_ROOT


GLOSSARY_FILE = PROJECT_ROOT / "glossary.json"
ENGLISH_BOUNDARY_RE = r"(?<![A-Za-z0-9]){}(?![A-Za-z0-9])"


@dataclass(frozen=True)
class Glossary:
    canonical: set[str]
    corrections: dict[str, str]


@dataclass(frozen=True)
class Replacement:
    source: str
    target: str
    count: int
    blocks: tuple[int, ...]


def default_glossary_paths(readable_path: Path | None = None) -> list[Path | None]:
    paths: list[Path | None] = [GLOSSARY_FILE]
    if readable_path is not None:
        paths.append(readable_path.with_name(readable_path.stem + ".glossary.json"))
    return paths


def load_glossary(paths: list[Path | None]) -> Glossary:
    canonical: set[str] = set()
    corrections: dict[str, str] = {}
    for path in paths:
        if path is None or not path.exists():
            continue
        data = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(data, dict):
            raise ValueError(f"Glossary must be a JSON object: {path}")
        for term in data.get("canonical", []) or []:
            if isinstance(term, str) and term.strip():
                canonical.add(term.strip())
        for garble, canon in (data.get("corrections", {}) or {}).items():
            if (
                isinstance(garble, str)
                and isinstance(canon, str)
                and garble.strip()
                and canon.strip()
            ):
                corrections[garble.strip()] = canon.strip()
    return Glossary(canonical=canonical, corrections=corrections)


def hotword_terms(glossary: Glossary, limit: int = 300) -> list[str]:
    """Return Latin-bearing canonical terms suitable for FunASR hotwords."""
    terms: set[str] = set()
    for term in glossary.canonical | set(glossary.corrections.values()):
        if _is_hotword_term(term):
            terms.add(term.strip())
            for part in re.split(r"[^A-Za-z0-9.+#-]+", term):
                if _is_hotword_term(part):
                    terms.add(part.strip())
    return sorted(terms, key=lambda item: (item.lower(), item))[:limit]


def write_hotword_file(path: Path, terms: list[str]) -> Path | None:
    if not terms:
        return None
    path.write_text("\n".join(terms) + "\n", encoding="utf-8")
    return path


def apply_corrections_to_readable_text(
    text: str,
    glossary: Glossary,
) -> tuple[str, list[Replacement]]:
    """Apply known ASR corrections to readable body lines only.

    Speaker/time lines and headings stay untouched so the verifier can still map
    the document structure back to the verbatim transcript.
    """
    lines = text.splitlines(keepends=True)
    current_block = 0
    totals: dict[tuple[str, str], int] = {}
    blocks: dict[tuple[str, str], set[int]] = {}
    out: list[str] = []
    for line in lines:
        if _is_speaker_line(line):
            current_block += 1
            out.append(line)
            continue
        if _skip_replacement_line(line):
            out.append(line)
            continue
        replaced = line
        for source, target in sorted(
            glossary.corrections.items(),
            key=lambda item: len(item[0]),
            reverse=True,
        ):
            replaced, count = replace_known_error(replaced, source, target)
            if count:
                key = (source, target)
                totals[key] = totals.get(key, 0) + count
                blocks.setdefault(key, set()).add(current_block or 1)
        out.append(replaced)
    replacements = [
        Replacement(source, target, count, tuple(sorted(blocks.get((source, target), set()))))
        for (source, target), count in sorted(totals.items(), key=lambda item: item[0][0].lower())
    ]
    return "".join(out), replacements


def replace_known_error(text: str, source: str, target: str) -> tuple[str, int]:
    if not source:
        return text, 0
    if re.search(r"[\u4e00-\u9fff]", source):
        count = text.count(source)
        return text.replace(source, target), count
    pattern = re.compile(ENGLISH_BOUNDARY_RE.format(re.escape(source)), re.IGNORECASE)
    return pattern.subn(target, text)


def _is_hotword_term(term: str) -> bool:
    compact = term.strip()
    return len(compact) >= 2 and bool(re.search(r"[A-Za-z]", compact))


def _is_speaker_line(line: str) -> bool:
    return bool(re.match(r"^\*\*.+?\*\*\s+`?\d{1,2}:\d{2}", line.strip()))


def _skip_replacement_line(line: str) -> bool:
    stripped = line.strip()
    return (
        not stripped
        or stripped.startswith("#")
        or stripped.startswith("```")
        or stripped.startswith("---")
        or stripped.startswith("|")
    )
