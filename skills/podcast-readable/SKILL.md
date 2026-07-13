---
name: podcast-readable
description: 将播客逐字稿 Markdown 按仓库内阅读版格式规范整理为同名 -阅读版.md，保留原逐字稿不动，并配合校验闸门检查。
---

# Podcast Readable

Use this skill when converting a saved podcast transcript Markdown file into its readable version.

The single source of truth is the repository's `READABLE_FORMAT.md`. Read that file before editing and follow it directly; do not create a parallel rule set in this skill.

## Workflow

1. Read the source transcript Markdown path printed by `python -m podcast_tracker readable <episode_id>`.
2. Read `READABLE_FORMAT.md`.
3. Write the output next to the source transcript with the same base name plus `-阅读版.md`.
4. Never edit the source transcript file.
5. Preserve speaker order, timestamps, and all substantive content. Apply only the cleaning and formatting allowed by the format specification.
6. If unsure whether text is filler, keep it and mark the uncertainty with `[?]`.
7. Run the audit passes below before claiming the readable file is done.
8. After writing the readable file, run the repository verification command printed by:

```bash
python -m podcast_tracker readable <episode_id>
```

The command output includes a glossary-apply step and a strict verifier step. Run both.
If verification fails, fix only the readable output and run the glossary apply + verifier again.

## Audit Passes

These passes operationalize `READABLE_FORMAT.md`; they are not a separate rule set.

1. **Question/answer attribution pass**: review quick Q&A sections for high-confidence §4B fixes. Split only local prompt-answer mismatches, keep order, and log every split in a same-name `.qna-log.json`.
2. **Entity consistency pass**: search the whole file for aliases of the same person, company, product, model, or framework. Unify high-confidence variants, such as ASR homophones for one person or repeated product names.
3. **Speaker identity pass**: infer high-confidence speaker mappings from host/guest lists, introductions, call-outs, and self-introductions. Apply only stable one-to-one mappings such as `说话人 1 -> Host A`, `说话人 2 -> Guest B`, `说话人 3 -> Guest C`; keep `说话人 X[?]` when evidence is insufficient or conflicting.
   - When real names are applied, write a same-name sidecar next to the readable file: `<readable-stem>.speaker-map.json`, for example `{"说话人 1": "Host A", "说话人 2": "Guest B"}`. This lets the verifier distinguish deliberate speaker identity mapping from accidental speaker mismatch.
   - **Montage / orphan voices**: cold-open news collages, end-credit songs, and inserted clips show up as several `说话人 N` that each appear only once or twice. These are genuinely different audio sources, NOT the host mis-split — keep them as separate voices, do not merge them into a host, and do not invent names. Only merge into one name when the same host was truly split across tracks within a continuous exchange.
4. **Cross-context proper noun pass**: use later clear mentions to fix earlier unclear mentions. If the same tool list appears later with clearer spelling, revisit earlier `[?]` items before leaving them unresolved.
5. **Domain glossary pass**: the shared source of truth is repository `glossary.json` (`canonical` = correct spellings, `corrections` = known ASR garbles → canonical). Before finalizing, apply every `corrections` entry (e.g. `cloudcode/cloudcold → Claude Code`, `URUX/URUYX → UI/UX`, `实诗 → 史诗`) and normalize terms to their `canonical` form. When you confidently identify a new recurring garble→term mapping, add it to `glossary.json` so future episodes self-correct. Any English token the audit flags as "unknown" must be resolved one of three ways: correct it from context, add it to the glossary, or mark it `[?]` — never leave a garbled proper noun silently.
6. **Number normalization pass**: convert clear years and model/version numbers to readable numeric form, such as `二零二六年` -> `2026 年` and `四点六` -> `4.6`. Do not change non-year idioms like `略知一二`.
7. **English capitalization pass**: fix obvious random mixed-case inside ordinary English words, such as `OPEnAI`, `exPErience`, or `PErson`. Preserve intentional acronyms and podcast/domain terms such as `FDE`, `FDPM`, `PE`, `API`, `LLMs`, and `SaaS`.
8. **Filler/repetition pass**: search for residual pure fillers and mechanical repeats (`呃`, `嗯嗯`, `啊这个`, repeated starts like `我我`, repeated phrases like `第三步、第三步`). Remove only non-substantive noise. Zero-tolerance set in a clean read: no surviving `嗯 / 呃 / 唉 / 哎 / 欸`, and no `啊啊 / 哦哦 / 哎哎` reduplications. Keep single sentence-final `啊 / 哦 / 哈` (e.g. `好啊`, `对哦`). When a `这个 / 那个` is referential (`这个时间`, `那个网站`), strip only the leading filler `啊`, never the referential `这个 / 那个`. After removing a filler, fix the punctuation it left behind (`。，` → `。`, leading commas, doubled commas).
9. **No editor-notes-in-body pass**: never write explanations such as "听辨不清" in the transcript body. Use the smallest inline marker (`[?]`) or the qna log for traceability.
10. **Residual uncertainty pass**: list remaining `[?]` items mentally or in a scratch note and keep only those that truly require audio/manual confirmation.
11. **Final gate pass**: run `scripts/apply_glossary.py` first, then run `scripts/verify_readable.py --strict` with the speaker-map sidecar (it auto-loads `glossary.json`). The verifier reports two layers: hard errors (block alignment, ≥0.85 retention, speaker mismatch, English mixed-case — these fail the build) and advisory warnings — some **blocking under `--strict`** (residual fillers, known glossary ASR errors, added content, editor notes, timestamps) and some **advisory-only** (`[?]` density, garbled-ASR heuristics, orphan/montage speakers, unknown English tokens, inconsistent spelling variants). Clear every `known ASR error` before shipping. Passing retention is necessary but not sufficient — clear the warnings too (or consciously accept montage/source-quality ones). Then run targeted searches for known residual patterns and fixed glossary terms.
