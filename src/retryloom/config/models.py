"""Typed configuration models."""
from pathlib import Path
from pydantic import BaseModel, Field

class ProjectConfig(BaseModel):
    root: str = "."
    default_agent: str = "codex"

class ReplayConfig(BaseModel):
    runs: int = Field(default=3, ge=1)
    timeout_minutes: int = Field(default=30, ge=1)

class EvaluationConfig(BaseModel):
    token_reduction_threshold: float = Field(default=0.15, ge=0, le=1)

class RetryloomConfig(BaseModel):
    version: int = 1
    project: ProjectConfig = Field(default_factory=ProjectConfig)
    replay: ReplayConfig = Field(default_factory=ReplayConfig)
    evaluation: EvaluationConfig = Field(default_factory=EvaluationConfig)

    def resolved_root(self, config_path: Path) -> Path:
        root = Path(self.project.root)
        return (config_path.parent / root).resolve() if not root.is_absolute() else root

