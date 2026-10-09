"""Owned temporary state; no host configuration or credentials are copied.

CODEX_HOME redirects supported Codex state. Live CLI authentication may therefore
be unavailable unless the caller explicitly supplies a supported auth environment.
This isolates state paths; it is not an OS sandbox against hostile commands.
"""
from dataclasses import dataclass
import os
from pathlib import Path
import shutil
import stat
import tempfile
from types import MappingProxyType

from .subprocess import execute_process


def baseline_environment(env: dict[str, str]) -> MappingProxyType:
    return MappingProxyType({key: value for key, value in env.items()
                             if not key.startswith("GIT_")})


def git_environment(env: dict[str, str]) -> dict[str, str]:
    result = {key: value for key, value in env.items() if not key.startswith("GIT_")}
    result.update(GIT_CONFIG_NOSYSTEM="1", GIT_CONFIG_GLOBAL=os.devnull,
                  GIT_OPTIONAL_LOCKS="0", GIT_NO_REPLACE_OBJECTS="1")
    return result


def git_command(cwd: Path, env: dict[str, str], *args: str) -> str:
    command = ["git", "-c", "core.fsmonitor=false", "-c", "core.hooksPath=/dev/null", *args]
    result = execute_process(command, cwd, git_environment(env), 60)
    if not result.success or result.output_truncated or result.cleanup_error:
        raise RuntimeError(result.error or result.cleanup_error or result.stderr or "Git command failed or timed out")
    return result.stdout.strip()


@dataclass(frozen=True)
class OwnedDirectory:
    path: Path
    parent_device: int
    parent_inode: int
    device: int
    inode: int

    @classmethod
    def create(cls, parent: Path | None = None) -> "OwnedDirectory":
        path = Path(tempfile.mkdtemp(prefix="retryloom-replay-", dir=parent)).absolute()
        parent_info, info = path.parent.stat(), path.lstat()
        return cls(path, parent_info.st_dev, parent_info.st_ino, info.st_dev, info.st_ino)

    def cleanup(self) -> str | None:
        parent_fd = None
        try:
            parent_fd = os.open(self.path.parent, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
            parent = os.fstat(parent_fd)
            if (parent.st_dev, parent.st_ino) != (self.parent_device, self.parent_inode):
                return "cleanup refused: temporary parent identity changed"
            try:
                info = os.stat(self.path.name, dir_fd=parent_fd, follow_symlinks=False)
            except FileNotFoundError:
                return None
            if not stat.S_ISDIR(info.st_mode) or (info.st_dev, info.st_ino) != (self.device, self.inode):
                return "cleanup refused: owned temporary directory was replaced"
            # Modern Python rmtree uses fd-relative traversal to reject symlink swaps.
            if not getattr(shutil.rmtree, "avoids_symlink_attacks", False):
                return "cleanup refused: platform lacks symlink-safe rmtree"
            shutil.rmtree(self.path.name, dir_fd=parent_fd)
        except OSError as error:
            return str(error)
        finally:
            if parent_fd is not None:
                os.close(parent_fd)
        return None


def run_environment(root: Path, baseline: dict[str, str], seed: int) -> dict[str, str]:
    result = dict(baseline)
    paths = {"HOME": root / "home", "XDG_CONFIG_HOME": root / "config",
             "XDG_CACHE_HOME": root / "cache", "XDG_STATE_HOME": root / "state",
             "XDG_DATA_HOME": root / "data", "CODEX_HOME": root / "codex",
             "TMPDIR": root / "tmp", "PYTHONPYCACHEPREFIX": root / "pycache"}
    for name, path in paths.items():
        path.mkdir()
        result[name] = str(path)
    result.update(TMP=result["TMPDIR"], TEMP=result["TMPDIR"],
                  PYTHONHASHSEED=str(seed), RETRYLOOM_RUN_SEED=str(seed))
    return git_environment(result)


def create_clone(source: Path, root: Path, commit: str, env: dict[str, str]) -> Path:
    # The source may contain the historical solution. Only the exact base commit
    # may cross into the run, preserving its OID and excluding parent history.
    seed = root / "seed.git"
    git_command(root, env, "init", "--bare", "--template=", str(seed))
    git_command(seed, env, "fetch", "--depth=1", "--no-tags", "--no-write-fetch-head", "--",
                str(source), f"{commit}:refs/heads/retryloom-base")
    git_command(seed, env, "symbolic-ref", "HEAD", "refs/heads/retryloom-base")
    destination = root / "repository"
    git_command(root, env, "clone", "--no-local", "--no-hardlinks", "--no-checkout",
                "--no-tags", "--template=", "--", str(seed), str(destination))
    git_command(destination, env, "checkout", "--detach", commit)
    if git_command(destination, env, "rev-parse", "HEAD") != commit:
        raise RuntimeError("clone HEAD differs from the resolved base commit")
    git_command(destination, env, "remote", "remove", "origin")
    if git_command(destination, env, "rev-list", "--all") != commit:
        raise RuntimeError("clone history contains commits outside the permitted base")
    if (destination / ".git" / "objects" / "info" / "alternates").exists():
        raise RuntimeError("clone has shared object alternates")
    # The seed is also per-run and contains no source history beyond the base.
    return destination
