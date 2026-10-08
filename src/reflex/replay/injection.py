"""Inject only immutable lesson text, with a deterministic integrity check."""
import hashlib
from dataclasses import dataclass
from typing import Literal


@dataclass(frozen=True)
class FrozenLesson:
    text: str
    lesson_hash: str

    @classmethod
    def freeze(cls, text: str) -> "FrozenLesson":
        return cls(text, hashlib.sha256(text.encode("utf-8")).hexdigest())

    def verify(self) -> None:
        if hashlib.sha256(self.text.encode("utf-8")).hexdigest() != self.lesson_hash:
            raise ValueError("frozen lesson hash mismatch")


def instructions_for(condition: Literal["CONTROL", "REFLEX"], lesson: FrozenLesson) -> list[str]:
    lesson.verify()
    if condition == "CONTROL":
        return []
    if condition == "REFLEX":
        return [lesson.text]
    raise ValueError("unknown replay condition")
