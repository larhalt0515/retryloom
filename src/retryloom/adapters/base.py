"""Shared adapter protocol for normalizing agent session sources."""
from pathlib import Path
from typing import Protocol

from retryloom.models import Session


class SessionAdapter(Protocol):
    """Discover source files and parse them into canonical sessions."""

    def discover(self) -> list[Path]:
        """Return available session files for this adapter."""
        ...

    def parse(self, path: Path) -> Session:
        """Parse one source file into a canonical session."""
        ...

