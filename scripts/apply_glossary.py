#!/usr/bin/env python3
from __future__ import annotations

import argparse
import importlib.util
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from podcast_tracker.glossary import (
    Replacement,
    apply_corrections_to_readable_text,
    default_glossary_paths,
    load_glossary,
)
from podcast_tracker.readable import speaker_map_path_for_readable


SCRIPT_DIR = Path(__file__).resolve().parent
VERIFY_SCRIPT = SCRIPT_DIR / "verify_readable.py"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="apply_glossary.py",
        description="Apply high-confidence glossary corrections to a readable transcript.",
    )
    parser.add_argument("readable", type=Path, help="Path to same-name -阅读版.md")
    parser.add_argument(
        "--glossary",
        action="append",
        type=Path,
        help="Additional glossary JSON file. Repo glossary and same-name sidecar are loaded by default.",
    )
    parser.add_argument("--dry-run", action="store_true", help="Show replacements without writing.")
    parser.add_argument(
        "--no-retention-check",
        action="store_true",
        help="Skip post-apply retention rollback check.",
    )
    args = parser.parse_args(argv)

    try:
        readable_path = args.readable
        before = readable_path.read_text(encoding="utf-8")
        glossary = load_glossary(default_glossary_paths(readable_path) + (args.glossary or []))
        after, replacements = apply_corrections_to_readable_text(before, glossary)
    except Exception as exc:
        print(f"apply_glossary.py error: {exc}", file=sys.stderr)
        return 2

    print(render_report(readable_path, replacements, dry_run=args.dry_run))
    if args.dry_run or after == before:
        return 0

    original_path = infer_original_path(readable_path)
    try:
        readable_path.write_text(after, encoding="utf-8")
        if not args.no_retention_check and original_path and original_path.exists():
            ok, reason = retention_check(original_path, readable_path)
            if not ok:
                readable_path.write_text(before, encoding="utf-8")
                print(f"retention check failed; reverted: {reason}", file=sys.stderr)
                return 1
            print("retention check: passed")
        elif not args.no_retention_check:
            print("retention check: skipped (source transcript not found)")
    except Exception as exc:
        readable_path.write_text(before, encoding="utf-8")
        print(f"apply_glossary.py error: {exc}", file=sys.stderr)
        return 2
    return 0


def render_report(path: Path, replacements: list[Replacement], dry_run: bool = False) -> str:
    lines = [
        "Glossary apply report",
        f"file: {path}",
        f"mode: {'dry-run' if dry_run else 'write'}",
        f"replacements: {sum(item.count for item in replacements)}",
    ]
    for item in replacements:
        blocks = ",".join(str(block) for block in item.blocks)
        lines.append(f"- {item.source} -> {item.target}: {item.count} hit(s), block(s): {blocks}")
    if not replacements:
        lines.append("- no known glossary errors found")
    return "\n".join(lines)


def infer_original_path(readable_path: Path) -> Path | None:
    suffix = "阅读版"
    stem = readable_path.stem
    if stem.endswith("-" + suffix):
        return readable_path.with_name(stem[: -len("-" + suffix)] + readable_path.suffix)
    if stem.endswith(suffix):
        return readable_path.with_name(stem[: -len(suffix)] + readable_path.suffix)
    return None


def retention_check(original_path: Path, readable_path: Path) -> tuple[bool, str]:
    verify_readable = _load_verify_module()
    speaker_map_path = speaker_map_path_for_readable(readable_path)
    speaker_map = (
        verify_readable.load_speaker_map_file(speaker_map_path)
        if speaker_map_path.exists()
        else {}
    )
    original_blocks = verify_readable.parse_original_transcript(original_path)
    readable_blocks = verify_readable.parse_readable_transcript(readable_path)
    ok, errors, _results = verify_readable.verify(
        original_blocks,
        readable_blocks,
        speaker_map=speaker_map,
    )
    retention_errors = [error for error in errors if "retention" in error.lower()]
    if retention_errors:
        return False, "; ".join(retention_errors[:3])
    if not ok and not errors:
        return False, "verifier failed without details"
    return True, "ok"


def _load_verify_module():
    spec = importlib.util.spec_from_file_location("verify_readable_for_apply", VERIFY_SCRIPT)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"Cannot load verifier: {VERIFY_SCRIPT}")
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


if __name__ == "__main__":
    raise SystemExit(main())
