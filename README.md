# Podcast Transcript Workflow

Local-first workflow for tracking podcast feeds, transcribing episodes, turning verbatim transcripts into readable Markdown, and optionally publishing the result to Feishu/Lark.

It was built for Chinese podcasts and 小宇宙 links, with FunASR as the default local backend.

## What It Does

```text
Podcast/RSS/Xiaoyuzhou URL
-> local episode index
-> audio download
-> ffmpeg 16 kHz mono wav
-> ASR backend
-> speaker-segmented verbatim Markdown
-> Codex-readable transcript pass
-> strict verifier
-> optional Feishu/Lark doc + group message
```

## Features

- Subscribe to RSS/Atom feeds, 小宇宙 program pages, or 小宇宙 episode pages.
- Generate verbatim transcripts with timestamps and speaker labels.
- Use FunASR locally by default; Whisper and OpenAI are optional backends.
- Run a Codex skill (`skills/podcast-readable`) to produce a polished readable transcript without changing the original.
- Verify readable transcripts with retention, speaker, glossary, filler, and English-token checks.
- Run as a local web app or CLI.
- Optionally publish readable transcripts to Feishu/Lark via `lark-cli`.

## Install

```bash
git clone <repo-url>
cd podcast-transcript-workflow
python3 -m venv .venv
.venv/bin/python -m pip install -U pip
.venv/bin/python -m pip install -U setuptools wheel
.venv/bin/python -m pip install -e .
```

If you are in an offline or restricted-network environment with an older pip, editable install can be forced through the legacy local path:

```bash
.venv/bin/python -m pip install --no-use-pep517 -e .
```

For the default local ASR backend:

```bash
brew install ffmpeg
bash scripts/setup_funasr.sh
```

The first FunASR run downloads models from ModelScope, usually around 1-2 GB.

## Configure

Start from the example file:

```bash
cp config.example.env .env
set -a
source .env
set +a
```

Or export only the values you need in your shell:

```bash
export PODCAST_TRACKER_DATA_DIR="$PWD/data"
export PODCAST_TRACKER_DOCS_DIR="$PWD/podcast-docs"
export PODCAST_TRACKER_PRIVATE_DIR="$HOME/.podcast_tracker_private"
```

Generated local state is intentionally ignored by git:

- `data/`: subscriptions, episode index, downloaded audio
- `podcast-docs/`: generated transcripts
- `~/.podcast_tracker_private/`: job state, logs, Feishu/Lark export cache

## CLI Usage

```bash
# Add a podcast/RSS/Xiaoyuzhou URL
podcast-tracker add <url>

# Check all subscriptions for new episodes
podcast-tracker check

# List pending episodes
podcast-tracker episodes --pending

# Transcribe one episode
podcast-tracker transcribe-auto <episode_id>

# Print readable transcript target and verifier commands
podcast-tracker readable <episode_id>

# Check subscriptions, transcribe new episodes, generate readable docs
podcast-tracker scheduled-update
```

You can also run with Python directly:

```bash
.venv/bin/python -m podcast_tracker check
```

## Local Web UI

```bash
scripts/run_web.sh
```

Open:

```text
http://127.0.0.1:8765/
```

macOS launchd helper:

```bash
scripts/install_launchd_web.sh
scripts/healthcheck_web.sh
scripts/uninstall_launchd_web.sh
```

## Codex Skill

The readable-transcript skill lives at:

```text
skills/podcast-readable/SKILL.md
```

The job runner calls it through `codex exec`, so readable generation requires the Codex CLI to be installed and authenticated, or `OPENAI_API_KEY` to be available for non-interactive Codex CLI use. Smoke test it before running scheduled jobs:

```bash
codex exec --help
```

You can override the binary with:

```bash
export PODCAST_TRACKER_CODEX_BIN=/path/to/codex
```

The format spec is `READABLE_FORMAT.md`; the shared terminology file is `glossary.json`.

## Feishu/Lark Publishing

Feishu/Lark publishing is optional and disabled by default for scheduled and web-generated readable transcripts. Manual web UI buttons can still publish explicitly when `lark-cli` is configured.

To enable it:

```bash
export PODCAST_TRACKER_ENABLE_LARK=1
export PODCAST_TRACKER_LARK_CLI_BIN=/path/to/lark-cli
export PODCAST_TRACKER_LARK_FOLDER_NAME="Podcast Transcripts"
export PODCAST_TRACKER_LARK_CHAT_NAME="Your Group Chat Name"
```

`lark-cli` must already be authenticated with the required Drive, Docs, and IM permissions.

## Backends

```bash
export PODCAST_TRACKER_ASR_BACKEND=funasr   # default
export PODCAST_TRACKER_ASR_BACKEND=whisper
export PODCAST_TRACKER_ASR_BACKEND=openai
```

Optional dependencies:

```bash
.venv/bin/python -m pip install -e ".[funasr]"
.venv/bin/python -m pip install -e ".[whisper]"
.venv/bin/python -m pip install -e ".[openai]"
```

The OpenAI backend also expects `OPENAI_API_KEY` and either the official transcription skill/CLI used by `podcast_tracker.asr`, or a compatible executable exposed through `TRANSCRIBE_CLI`.

## Tests

```bash
.venv/bin/python -m pip install pytest
.venv/bin/python -m pytest
```

Or with the standard library only:

```bash
.venv/bin/python -m unittest discover -s tests
```

CI runs the standard-library and mocked tests without downloading ASR models.

## Privacy

Do not commit:

- `data/`
- downloaded audio
- generated transcript Markdown
- `.env`
- Feishu/Lark export caches
- private job logs

The repository includes only code, tests, examples, the Codex skill, and public workflow documentation.
