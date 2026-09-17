from __future__ import annotations

import sys
import time

STAGES = ("fetch", "render", "layout", "extract", "assemble")


class Reporter:

    def __init__(self, stream=None) -> None:
        self.stream = stream or sys.stdout
        self.stage_name = ""
        self.started = 0.0

    def _emit(self, text: str) -> None:
        prefix = f"[stage:{self.stage_name}] " if self.stage_name else ""
        self.stream.write(f"{prefix}{text}\n")
        self.stream.flush()

    def stage(self, name: str, detail: str = "") -> None:
        self.stage_name = name
        self.started = time.perf_counter()
        self._emit(detail or "starting")

    def note(self, text: str) -> None:
        self._emit(text)

    def step(self, done: int, total: int) -> None:
        self._emit(f"{done}/{max(total, 1)}")

    def finish(self, summary: str = "") -> None:
        elapsed = time.perf_counter() - self.started if self.started else 0.0
        tail = f" — {summary}" if summary else ""
        self._emit(f"done in {elapsed:.1f}s{tail}")


SILENT = Reporter(stream=type("_Null", (), {"write": lambda *_: None,
                                            "flush": lambda *_: None})())
