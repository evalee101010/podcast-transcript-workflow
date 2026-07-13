from __future__ import annotations

import os
from pathlib import Path


PRIVATE_DIR_ENV = "PODCAST_TRACKER_PRIVATE_DIR"


def private_runtime_dir() -> Path:
    configured = os.getenv(PRIVATE_DIR_ENV)
    if configured:
        return Path(configured).expanduser()
    return Path.home() / ".podcast_tracker_private"
