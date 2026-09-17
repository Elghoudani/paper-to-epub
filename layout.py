from __future__ import annotations

import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Iterable, Iterator, Sequence

from PIL import Image

from cache import PageGeometry, StageCache, book_dir, read_json, write_json
from config import LABEL_POLICY, LAYOUT_SUBDIR, MIN_REGION_PX
from progress import Reporter
from render import BookRaster, RenderedPage, load_raster

LAYOUT_VERSION = 3

DEFAULT_BATCH = 8

FULL_WIDTH_FRACTION = 0.6

SIDEWAYS = (90, 270)


@dataclass(frozen=True)
class Region:

    id: str
    label: str
    policy: str
    order: int
    box_px: tuple[float, float, float, float]
    box_pt: tuple[float, float, float, float]

    @property
    def width_px(self) -> float:
        return self.box_px[2] - self.box_px[0]

    @property
    def centre_x(self) -> float:
        return (self.box_px[0] + self.box_px[2]) / 2

    @property
    def top(self) -> float:
        return self.box_px[1]

    def renumbered(self, order: int) -> "Region":
        return Region(self.id, self.label, self.policy, order, self.box_px, self.box_pt)

    def as_dict(self) -> dict:
        return {
            "region_id": self.id,
            "label": self.label,
            "policy": self.policy,
            "reading_order": self.order,
            "bbox_px": list(self.box_px),
            "bbox_pt": list(self.box_pt),
        }


@dataclass
class DetectedBox:

    label: str
    left: float
    top: float
    right: float
    bottom: float
    stated_order: int | None = None


@dataclass
class PageLayout:
    page: RenderedPage
    regions: list[Region] = field(default_factory=list)

    def as_dict(self) -> dict:
        return {
            "page_index": self.page.index,
            "page_number": self.page.number,
            "image_path": str(self.page.image_path),
            **self.page.geometry.as_dict(),
            "regions": [region.as_dict() for region in self.regions],
        }


class LayoutModel:

    def __init__(self, detect) -> None:
        self._detect = detect

    @classmethod
    def load(cls, reporter: Reporter) -> "LayoutModel":
        try:
            import surya
        except ImportError as exc:
            raise RuntimeError(
                "surya-ocr is not installed — pip install surya-ocr==0.6.0"
            ) from exc

        attempts = (
            ("split model/processor (0.6.x)", cls._load_split),
            ("bundled segformer (0.6.x)", cls._load_segformer),
            ("LayoutPredictor (0.7+)", cls._load_predictor),
        )

        problems: list[str] = []
        for description, loader in attempts:
            try:
                detect = loader()
            except Exception as exc:
                problems.append(f"{description}: {exc}")
                continue
            reporter.note(f"model ready via {description}")
            return cls(detect)

        raise RuntimeError(
            "could not load a surya layout detector:\n  " + "\n  ".join(problems)
        )

    @staticmethod
    def _checkpoint() -> str:
        from surya.settings import settings
        return settings.LAYOUT_MODEL_CHECKPOINT

    @staticmethod
    def _call_with_checkpoint(factory, checkpoint):
        for call in (lambda: factory(checkpoint=checkpoint),
                     lambda: factory(checkpoint),
                     factory):
            try:
                return call()
            except TypeError:
                continue
        return factory()

    @classmethod
    def _load_split(cls):
        from surya.model.detection.model import load_model, load_processor
        from surya.layout import batch_layout_detection

        checkpoint = cls._checkpoint()
        model = cls._call_with_checkpoint(load_model, checkpoint)
        processor = cls._call_with_checkpoint(load_processor, checkpoint)
        return lambda images: batch_layout_detection(images, model, processor)

    @classmethod
    def _load_segformer(cls):
        from surya.model.detection import segformer
        from surya.layout import batch_layout_detection

        checkpoint = cls._checkpoint()
        model = cls._call_with_checkpoint(segformer.load_model, checkpoint)
        processor = cls._call_with_checkpoint(segformer.load_processor, checkpoint)
        return lambda images: batch_layout_detection(images, model, processor)

    @staticmethod
    def _load_predictor():
        from surya.layout import LayoutPredictor

        predictor = LayoutPredictor()
        return lambda images: predictor(images)

    def __call__(self, images: Sequence[Image.Image]) -> list:
        return self._detect(images)


def read_boxes(result) -> list[DetectedBox]:
    for attribute in ("bboxes", "layout_boxes"):
        raw = getattr(result, attribute, None)
        if raw is not None:
            break
    else:
        raw = result if isinstance(result, list) else list(result or [])

    boxes: list[DetectedBox] = []
    for item in raw:
        rectangle = getattr(item, "bbox", None)
        if rectangle is None:
            corners = getattr(item, "polygon", None)
            if not corners:
                continue
            xs = [point[0] for point in corners]
            ys = [point[1] for point in corners]
            rectangle = (min(xs), min(ys), max(xs), max(ys))

        stated = getattr(item, "position", None)
        if stated is None:
            stated = getattr(item, "order", None)

        boxes.append(DetectedBox(
            label=getattr(item, "label", "Text"),
            left=rectangle[0], top=rectangle[1],
            right=rectangle[2], bottom=rectangle[3],
            stated_order=stated,
        ))

    if any(box.stated_order is not None for box in boxes):
        boxes.sort(key=lambda box: box.stated_order if box.stated_order is not None else 0)
    return boxes


def build_regions(boxes: Iterable[DetectedBox], page: RenderedPage) -> list[Region]:
    geometry = page.geometry
    scale = geometry.points_per_pixel
    regions: list[Region] = []

    for box in boxes:
        policy = LABEL_POLICY.get(box.label, "skip")
        if policy == "skip":
            continue

        left = min(max(box.left, 0), geometry.width_px)
        right = min(max(box.right, 0), geometry.width_px)
        top = min(max(box.top, 0), geometry.height_px)
        bottom = min(max(box.bottom, 0), geometry.height_px)

        if right - left < MIN_REGION_PX or bottom - top < MIN_REGION_PX:
            continue

        index = len(regions)
        regions.append(Region(
            id=f"p{page.number}_r{index}",
            label=box.label,
            policy=policy,
            order=index,
            box_px=(left, top, right, bottom),
            box_pt=tuple(round(value * scale, 3) for value in (left, top, right, bottom)),
        ))

    return regions


def whole_page_region(page: RenderedPage) -> list[Region]:
    geometry = page.geometry
    scale = geometry.points_per_pixel
    box_px = (0.0, 0.0, float(geometry.width_px), float(geometry.height_px))
    return [Region(
        id=f"p{page.number}_r0",
        label="Figure",
        policy="image",
        order=0,
        box_px=box_px,
        box_pt=tuple(round(value * scale, 3) for value in box_px),
    )]


def reading_order(regions: Sequence[Region], page_width_px: int) -> list[Region]:
    if not regions:
        return []

    middle = page_width_px / 2
    full_width_at = page_width_px * FULL_WIDTH_FRACTION

    ranked: list[tuple[tuple[int, int, float, float], Region]] = []
    band = 0

    for region in sorted(regions, key=lambda item: (item.top, item.box_px[0])):
        if region.width_px > full_width_at:
            band += 1
            ranked.append(((band, 0, region.top, region.box_px[0]), region))
            band += 1
            continue

        column = 0 if region.centre_x < middle else 1
        ranked.append(((band, column, region.top, region.box_px[0]), region))

    ranked.sort(key=lambda pair: pair[0])
    return [region.renumbered(position) for position, (_key, region) in enumerate(ranked)]


def manifest_path(book: str) -> Path:
    return book_dir(book) / "layout_manifest.json"


def _batches(pages: Sequence[RenderedPage], size: int) -> Iterator[list[RenderedPage]]:
    for start in range(0, len(pages), size):
        yield list(pages[start:start + size])


def detect_layout(book: str, batch_size: int = DEFAULT_BATCH,
                  reporter: Reporter | None = None) -> dict:
    raster: BookRaster = load_raster(book)
    reporter = reporter or Reporter()
    reporter.stage("layout", f"{len(raster.pages)} pages")

    store = StageCache(book, LAYOUT_SUBDIR, layout_version=LAYOUT_VERSION)
    store.dir.mkdir(parents=True, exist_ok=True)

    cached: dict[int, dict] = {}
    outstanding: list[RenderedPage] = []
    for page in raster.pages:
        record = store.load(page.number, page.geometry)
        if record is None:
            outstanding.append(page)
        else:
            cached[page.number] = record

    if cached:
        reporter.note(f"{len(cached)} page(s) already detected")

    if outstanding:
        model = LayoutModel.load(reporter)
        finished = len(cached)
        for batch in _batches(outstanding, max(1, batch_size)):
            images = [Image.open(page.image_path).convert("RGB") for page in batch]
            try:
                results = model(images)
                for page, result in zip(batch, results):
                    if page.rotation in SIDEWAYS:
                        regions = whole_page_region(page)
                    else:
                        regions = reading_order(
                            build_regions(read_boxes(result), page),
                            page.geometry.width_px,
                        )
                    cached[page.number] = store.save(
                        page.number, PageLayout(page, regions).as_dict()
                    )
                    finished += 1
                    reporter.step(finished, len(raster.pages))
            finally:
                for image in images:
                    image.close()

    ordered = [cached[page.number] for page in raster.pages]
    manifest = {
        "book_name": book,
        "page_count": len(ordered),
        "total_regions": sum(len(page["regions"]) for page in ordered),
        "pages": ordered,
    }
    write_json(manifest_path(book), manifest)

    reporter.finish(f"{manifest['total_regions']} regions")
    _report_labels(ordered, reporter)
    return manifest


def _report_labels(pages: Sequence[dict], reporter: Reporter) -> None:
    counts: dict[str, int] = {}
    for page in pages:
        for region in page["regions"]:
            counts[region["label"]] = counts.get(region["label"], 0) + 1
    if not counts:
        reporter.note("no regions detected — is this a scanned PDF?")
        return
    summary = ", ".join(
        f"{label} {count}" for label, count in sorted(counts.items(), key=lambda kv: -kv[1])
    )
    reporter.note(summary)


def load_layout(book: str) -> dict:
    record = read_json(manifest_path(book))
    if record is None:
        raise FileNotFoundError(
            f"'{book}' has no layout yet — run the layout stage first."
        )
    return record


if __name__ == "__main__":
    if len(sys.argv) < 2:
        print("usage: python layout.py <book> [batch-size]")
        sys.exit(1)
    size = int(sys.argv[2]) if len(sys.argv) > 2 else DEFAULT_BATCH
    detect_layout(sys.argv[1], batch_size=size)
