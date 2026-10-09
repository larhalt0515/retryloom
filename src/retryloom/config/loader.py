"""Load and write the local TOML configuration."""
import tomllib
from pathlib import Path
from .models import RetryloomConfig

DEFAULT_CONFIG = '''version = 1

[project]
root = "."
default_agent = "codex"

[replay]
runs = 3
timeout_minutes = 30

[evaluation]
token_reduction_threshold = 0.15
'''

def load_config(path: Path) -> RetryloomConfig:
    with path.open("rb") as handle:
        return RetryloomConfig.model_validate(tomllib.load(handle))

def write_default_config(path: Path) -> RetryloomConfig:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(DEFAULT_CONFIG, encoding="utf-8")
    return load_config(path)

