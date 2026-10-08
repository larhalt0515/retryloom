"""Independent per-run Git clone lifecycle."""
from dataclasses import dataclass
from pathlib import Path
import shutil
import subprocess

from .repository import ensure_repository


class CloneError(RuntimeError):
    """Raised when an isolated clone cannot be created."""


@dataclass(frozen=True)
class CleanupResult:
    path: Path
    removed: bool
    error: str | None = None


class CloneManager:
    """Create independent clones and clean them up best-effort."""

    def __init__(self, source: Path, replay_root: Path):
        self.source = ensure_repository(source)
        self.replay_root = replay_root.resolve()

    def create(self, run_name: str, base_commit: str) -> Path:
        if not run_name or Path(run_name).name != run_name or run_name in {".", ".."}:
            raise CloneError(f"invalid run name: {run_name!r}")
        destination = self.replay_root / run_name
        if destination.exists():
            raise CloneError(f"clone destination already exists: {destination}")
        self.replay_root.mkdir(parents=True, exist_ok=True)
        try:
            subprocess.run(
                ["git", "clone", "--no-hardlinks", str(self.source), str(destination)],
                check=True,
                capture_output=True,
                text=True,
            )
            subprocess.run(
                ["git", "checkout", "--detach", base_commit],
                cwd=destination,
                check=True,
                capture_output=True,
                text=True,
            )
        except subprocess.CalledProcessError as exc:
            self.cleanup(destination)
            detail = (exc.stderr or str(exc)).strip()
            raise CloneError(f"failed to create clone {destination}: {detail}") from exc
        return destination

    def cleanup(self, path: Path) -> CleanupResult:
        candidate = path.resolve()
        try:
            relative = candidate.relative_to(self.replay_root)
        except ValueError:
            return CleanupResult(
                path=path, removed=False, error="cleanup target is outside replay root"
            )
        if len(relative.parts) != 1 or not candidate.is_dir() or candidate.is_symlink():
            return CleanupResult(
                path=path, removed=False, error="cleanup target is not a run clone"
            )
        try:
            shutil.rmtree(candidate)
        except OSError as exc:
            return CleanupResult(path=candidate, removed=False, error=str(exc))
        return CleanupResult(path=candidate, removed=True)
