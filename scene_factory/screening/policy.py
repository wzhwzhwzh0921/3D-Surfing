"""Shared evidence layout and versioned prompts; no network or rendering."""
from pathlib import Path

DEFAULT_VERSION = "v5"
DEFAULT_MODEL = "gpt-6.1-sol"
DEFAULT_URL = "http://127.0.0.1:18773/v1/chat/completions"
VERSIONS = ("v1", "v2", "v3", "v4", "v5")
VIEWS = [f"az_{azimuth:03d}_{zoom}" for zoom in ("overview", "closer")
         for azimuth in (45, 135, 225, 315)]


def prompt_path(version: str = DEFAULT_VERSION) -> Path:
    if version not in VERSIONS:
        raise ValueError(f"Unknown screening prompt version: {version}")
    return Path(__file__).with_name("prompts") / f"{version}.txt"


def prompt_text(version: str = DEFAULT_VERSION) -> str:
    # Historical inline prompts did not include the file's final newline.
    # Keep this distinction so direct image-screening caches remain reusable.
    return prompt_path(version).read_text(encoding="utf-8").rstrip("\n")
