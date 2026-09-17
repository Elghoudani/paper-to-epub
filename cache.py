from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from config import DATA_DIR


def book_dir(book: str) -> Path:
    return DATA_DIR / book


def read_json(path: Path) -> dict | None:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None


def write_json(path: Path, payload: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, indent=2), encoding="utf-8")


@dataclass(frozen=True)
class PageGeometry:

    width_px: int
    height_px: int
    width_pt: float
    height_pt: float

    @property
    def points_per_pixel(self) -> float:
        if self.width_px <= 0:
            return 0.0
        return self.width_pt / self.width_px

    def matches(self, record: dict[str, Any]) -> bool:
        return (record.get("width_px") == self.width_px
                and record.get("height_px") == self.height_px)

    def as_dict(self) -> dict:
        return {
            "width_px": self.width_px,
            "height_px": self.height_px,
            "width_pt": self.width_pt,
            "height_pt": self.height_pt,
        }

    @classmethod
    def from_dict(cls, record: dict) -> "PageGeometry":
        return cls(
            width_px=record["width_px"],
            height_px=record["height_px"],
            width_pt=record["width_pt"],
            height_pt=record["height_pt"],
        )


class StageCache:

    def __init__(self, book: str, subdir: str, **stamps: Any) -> None:
        self.dir = book_dir(book) / subdir
        self.stamps = stamps

    def path_for(self, page_number: int) -> Path:
        return self.dir / f"page_{page_number:04d}.json"

    def load(self, page_number: int, geometry: PageGeometry) -> dict | None:
        record = read_json(self.path_for(page_number))
        if record is None:
            return None
        if any(record.get(key) != value for key, value in self.stamps.items()):
            return None
        if not geometry.matches(record):
            return None
        return record

    def save(self, page_number: int, record: dict) -> dict:
        stamped = {**self.stamps, **record}
        write_json(self.path_for(page_number), stamped)
        return stamped
