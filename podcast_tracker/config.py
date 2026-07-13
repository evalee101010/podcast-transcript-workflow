import os
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parents[1]
WORKSPACE_ROOT = PROJECT_ROOT.parent


def _path_from_env(name: str, default: Path) -> Path:
    configured = os.getenv(name)
    if configured:
        return Path(configured).expanduser()
    return default


DATA_DIR = _path_from_env("PODCAST_TRACKER_DATA_DIR", PROJECT_ROOT / "data")
DOCS_DIR = _path_from_env("PODCAST_TRACKER_DOCS_DIR", PROJECT_ROOT / "podcast-docs")
READABLE_SKILL_DIR = _path_from_env(
    "PODCAST_TRACKER_READABLE_SKILL_DIR",
    PROJECT_ROOT / "skills" / "podcast-readable",
)
READABLE_FORMAT_FILE = _path_from_env(
    "PODCAST_TRACKER_READABLE_FORMAT_FILE",
    PROJECT_ROOT / "READABLE_FORMAT.md",
)
SUBSCRIPTIONS_FILE = DATA_DIR / "subscriptions.json"
EPISODES_FILE = DATA_DIR / "episodes.json"
