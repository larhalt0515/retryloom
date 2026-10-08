"""Read-only Git state inspection used by replay safety checks."""
from dataclasses import dataclass
from pathlib import Path
import subprocess
import hashlib
import os
import stat


def _git(root: Path, *args: str, env: dict[str, str] | None = None) -> str:
    safe_env = {key: value for key, value in (env if env is not None else os.environ).items()
                if not key.startswith("GIT_")}
    safe_env.update(GIT_CONFIG_NOSYSTEM="1", GIT_CONFIG_GLOBAL=os.devnull,
                    GIT_OPTIONAL_LOCKS="0", GIT_NO_REPLACE_OBJECTS="1")
    result = subprocess.run(
        ["git", "-c", "core.fsmonitor=false", *args], cwd=root, env=safe_env,
        check=True, capture_output=True, text=True, timeout=60
    )
    return result.stdout


@dataclass(frozen=True)
class RepositoryState:
    head: str
    index: str
    tracked: str
    untracked: str
    untracked_hashes: tuple[tuple[str, str], ...] = ()
    workspace_hashes: tuple[tuple[str, str], ...] = ()


def _untracked_hashes(root: Path, env: dict[str, str] | None = None) -> tuple[tuple[str, str], ...]:
    paths = _git(root, "ls-files", "--others", "--exclude-standard", "-z", env=env)
    hashes = []
    for name in sorted(filter(None, paths.split("\0"))):
        path = root / name
        if path.is_symlink() or not path.resolve().is_relative_to(root.resolve()):
            continue
        if not stat.S_ISREG(path.lstat().st_mode):
            continue
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
        with os.fdopen(fd, "rb") as handle:
            hashes.append((name, hashlib.file_digest(handle, "sha256").hexdigest()))
    return tuple(hashes)


def _workspace_hashes(root: Path, excluded_paths: tuple[Path, ...] = ()) -> tuple[tuple[str, str], ...]:
    """Include ignored files and Git state without following symlink targets."""
    hashes = []
    for directory, dirs, files in os.walk(root, followlinks=False):
        for name in sorted(dirs + files):
            path = Path(directory) / name
            info = path.lstat()
            if path in excluded_paths and stat.S_ISREG(info.st_mode):
                continue
            digest = hashlib.sha256(str(stat.S_IMODE(info.st_mode)).encode())
            if stat.S_ISLNK(info.st_mode):
                digest.update(os.readlink(path).encode())
            elif stat.S_ISREG(info.st_mode):
                fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
                with os.fdopen(fd, "rb") as handle:
                    digest.update(hashlib.file_digest(handle, "sha256").digest())
            else:
                digest.update(str(stat.S_IFMT(info.st_mode)).encode())
            hashes.append((str(path.relative_to(root)), digest.hexdigest()))
    return tuple(sorted(hashes))


def repository_state(root: Path, env: dict[str, str] | None = None,
                     excluded_paths: tuple[Path, ...] = ()) -> RepositoryState:
    return RepositoryState(
        head=_git(root, "rev-parse", "HEAD", env=env).strip(),
        index=_git(root, "ls-files", "--stage", "--others", "--exclude-standard", env=env),
        tracked=_git(root, "diff", "--no-ext-diff", "--no-textconv", "--binary", env=env),
        untracked=_git(root, "ls-files", "--others", "--exclude-standard", "--directory", env=env),
        untracked_hashes=_untracked_hashes(root, env),
        workspace_hashes=_workspace_hashes(root, excluded_paths),
    )


def ensure_repository(root: Path, env: dict[str, str] | None = None) -> Path:
    root = root.resolve()
    return Path(_git(root, "rev-parse", "--show-toplevel", env=env).strip()).resolve()
