from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime

from .models import iso_time, parse_time


@dataclass(slots=True)
class VirtualClock:
    current: datetime

    @classmethod
    def from_iso(cls, value: str) -> "VirtualClock":
        return cls(parse_time(value))

    def advance_to(self, target: str | datetime) -> str:
        parsed = parse_time(target)
        if parsed < self.current:
            raise ValueError("VirtualClock cannot move backwards")
        self.current = parsed
        return iso_time(self.current)

    @property
    def now(self) -> str:
        return iso_time(self.current)

