from __future__ import annotations

import re
from dataclasses import dataclass, field

VISUAL_LABELS = ("Figure", "Picture", "FigureGroup")

FIGURE_CAPTION = re.compile(r"^\s*fig(?:ure)?s?\.?\s*(\d+[a-z]?)", re.IGNORECASE)
TABLE_CAPTION = re.compile(r"^\s*tab(?:le)?s?\.?\s*(\d+|[ivxlcdm]+)", re.IGNORECASE)

SECTION_NUMBER = re.compile(r"^\s*(\d+(?:\.\d+)*)")

DEFINITION_START = re.compile(r"^\s*(?:\[\d+\]|\d+\.)\s")
DEFINITION_SPLIT = re.compile(r"(?=(?:\[\d+\]|\b\d+\.)\s)")
DEFINITION_ENTRY = re.compile(r"^(?:\[(\d+)\]|(\d+)\.)\s*(.+)", re.DOTALL)


@dataclass
class Target:

    anchor: str
    chapter: str = ""


class LinkIndex:

    def __init__(self) -> None:
        self.targets: dict[str, Target] = {}
        self.sections: dict[str, str] = {}

    def scan(self, regions: list[dict]) -> None:
        claimed_figures: set[str] = set()
        claimed_tables: set[str] = set()

        for position, region in enumerate(regions):
            if region.get("label") != "Caption":
                continue
            text = (region.get("content", {}).get("text") or "").strip()

            figure = FIGURE_CAPTION.match(text)
            if figure:
                number = figure.group(1).lower()
                claimed_figures.add(number)
                self._attach(regions, position, f"fig-{number}", VISUAL_LABELS, backwards=True)
                continue

            table = TABLE_CAPTION.match(text)
            if table:
                number = table.group(1).lower()
                claimed_tables.add(number)
                self._attach(regions, position, f"tbl-{number}", ("Table",), backwards=False)

        self._number_the_rest(regions, claimed_figures, claimed_tables)

    def _attach(self, regions: list[dict], caption_at: int, anchor: str,
                labels: tuple[str, ...], backwards: bool) -> None:
        before = range(caption_at - 1, -1, -1)
        after = range(caption_at + 1, len(regions))
        for search in ((before, after) if backwards else (after, before)):
            for index in search:
                candidate = regions[index]
                if candidate.get("xref_id"):
                    continue
                if candidate.get("policy") == "image" or candidate.get("label") in labels:
                    candidate["xref_id"] = anchor
                    self.targets[anchor] = Target(anchor)
                    return

    def _number_the_rest(self, regions: list[dict], figures: set[str], tables: set[str]) -> None:
        counters = {"fig": 1, "tbl": 1, "eq": 1}
        taken = {"fig": figures, "tbl": tables, "eq": set()}

        for region in regions:
            if region.get("policy") != "image" or region.get("xref_id"):
                continue
            label = region.get("label", "")
            if label in VISUAL_LABELS:
                kind = "fig"
            elif label == "Table":
                kind = "tbl"
            elif label == "Formula":
                kind = "eq"
            else:
                continue

            while str(counters[kind]) in taken[kind]:
                counters[kind] += 1
            anchor = f"{kind}-{counters[kind]}"
            counters[kind] += 1
            region["xref_id"] = anchor
            self.targets[anchor] = Target(anchor)

    def place(self, region: dict, chapter_file: str) -> None:
        anchor = region.get("xref_id")
        if anchor and anchor in self.targets:
            self.targets[anchor].chapter = chapter_file

    def place_section(self, title: str, chapter_file: str) -> None:
        match = SECTION_NUMBER.match(title)
        if match:
            self.sections[match.group(1)] = chapter_file

    def href(self, anchor: str, from_chapter: str) -> str | None:
        target = self.targets.get(anchor)
        if target is None or not target.chapter:
            return None
        if target.chapter == from_chapter:
            return f"#{target.anchor}"
        return f"{target.chapter}#{target.anchor}"

    def section_href(self, number: str, from_chapter: str) -> str | None:
        chapter = self.sections.get(number)
        if chapter is None or chapter == from_chapter:
            return None
        return chapter


@dataclass
class Note:
    number: int
    text: str
    first_seen_in: str = ""


class NoteIndex:

    def __init__(self) -> None:
        self.notes: dict[int, Note] = {}
        self.definition_regions: set[str] = set()

    def scan(self, regions: list[dict]) -> None:
        for region in regions:
            content = region.get("content", {})
            if content.get("type") != "text":
                continue
            text = (content.get("text") or "").strip()
            if not text or not DEFINITION_START.match(text):
                continue
            entries = self._split(text)
            if not entries:
                continue
            self.definition_regions.add(region["region_id"])
            for number, body in entries:
                self.notes.setdefault(number, Note(number, body))

    @staticmethod
    def _split(text: str) -> list[tuple[int, str]]:
        found: list[tuple[int, str]] = []
        for piece in DEFINITION_SPLIT.split(text.strip()):
            entry = DEFINITION_ENTRY.match(piece.strip())
            if entry:
                number = int(entry.group(1) or entry.group(2))
                found.append((number, entry.group(3).strip()))
        return found

    def __contains__(self, number: int) -> bool:
        return number in self.notes

    def cite(self, number: int, chapter_file: str) -> tuple[str, bool]:
        note = self.notes[number]
        if not note.first_seen_in:
            note.first_seen_in = chapter_file
            return f"fnref-{number}", True
        return f"fnref-{number}", False

    def is_definition(self, region: dict) -> bool:
        return region.get("region_id") in self.definition_regions

    @property
    def any(self) -> bool:
        return bool(self.notes)
