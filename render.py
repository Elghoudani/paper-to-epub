from __future__ import annotations

import sys
from dataclasses import dataclass
from pathlib import Path

import fitz
from PIL import Image

from cache import PageGeometry, book_dir, read_json, write_json
from config import PAGES_SUBDIR, RENDER_DPI
from progress import Reporter


@dataclass(frozen=True)
class RenderedPage:

    index: int
    number: int
    image_path: Path
    geometry: PageGeometry
    rotation: int

    def as_dict(self) -> dict:
        return {
            "page_index": self.index,
            "page_number": self.number,
            "image_path": str(self.image_path),
            "rotation": self.rotation,
            **self.geometry.as_dict(),
        }

    @classmethod
    def from_dict(cls, record: dict) -> "RenderedPage":
        return cls(
            index=record["page_index"],
            number=record["page_number"],
            image_path=Path(record["image_path"]),
            geometry=PageGeometry.from_dict(record),
            rotation=record.get("rotation", 0),
        )


@dataclass(frozen=True)
class BookRaster:

    book: str
    pdf_path: Path
    dpi: int
    pages: list[RenderedPage]

    def as_dict(self) -> dict:
        return {
            "book_name": self.book,
            "pdf_path": str(self.pdf_path),
            "dpi": self.dpi,
            "page_count": len(self.pages),
            "pages": [page.as_dict() for page in self.pages],
        }

    @classmethod
    def from_dict(cls, record: dict) -> "BookRaster":
        return cls(
            book=record["book_name"],
            pdf_path=Path(record["pdf_path"]),
            dpi=record.get("dpi", RENDER_DPI),
            pages=[RenderedPage.from_dict(page) for page in record["pages"]],
        )


def manifest_path(book: str) -> Path:
    return book_dir(book) / "manifest.json"


def expected_size(page: fitz.Page, dpi: int) -> tuple[int, int]:
    scale = dpi / 72.0
    return round(page.rect.width * scale), round(page.rect.height * scale)


def _reusable(path: Path, wanted: tuple[int, int]) -> tuple[int, int] | None:
    if not path.is_file():
        return None
    try:
        with Image.open(path) as existing:
            size = existing.size
    except OSError:
        return None
    if abs(size[0] - wanted[0]) > 2 or abs(size[1] - wanted[1]) > 2:
        return None
    return size


def render_book(pdf_path: Path, reporter: Reporter | None = None) -> BookRaster:
    pdf_path = Path(pdf_path).resolve()
    if not pdf_path.is_file():
        raise FileNotFoundError(f"no such PDF: {pdf_path}")

    book = pdf_path.stem
    target_dir = book_dir(book) / PAGES_SUBDIR
    target_dir.mkdir(parents=True, exist_ok=True)

    reporter = reporter or Reporter()
    reporter.stage("render", f"{RENDER_DPI} dpi → {target_dir}")

    document = fitz.open(pdf_path)
    zoom = fitz.Matrix(RENDER_DPI / 72.0, RENDER_DPI / 72.0)
    pages: list[RenderedPage] = []
    redrawn = 0

    try:
        for index, page in enumerate(document):
            number = index + 1
            image_path = target_dir / f"page_{number:04d}.png"
            wanted = expected_size(page, RENDER_DPI)

            size = _reusable(image_path, wanted)
            if size is None:
                pixmap = page.get_pixmap(matrix=zoom, colorspace=fitz.csRGB)
                pixmap.save(str(image_path))
                size = (pixmap.width, pixmap.height)
                redrawn += 1

            pages.append(RenderedPage(
                index=index,
                number=number,
                image_path=image_path,
                geometry=PageGeometry(
                    width_px=size[0],
                    height_px=size[1],
                    width_pt=round(page.rect.width, 2),
                    height_pt=round(page.rect.height, 2),
                ),
                rotation=page.rotation,
            ))
            reporter.step(number, document.page_count)
    finally:
        document.close()

    raster = BookRaster(book=book, pdf_path=pdf_path, dpi=RENDER_DPI, pages=pages)
    write_json(manifest_path(book), raster.as_dict())

    reused = len(pages) - redrawn
    reporter.finish(f"{redrawn} rendered, {reused} reused")
    if redrawn and reused == 0 and len(pages) > 1:
        reporter.note("pages changed size: later stages will recompute")
    return raster


def load_raster(book: str) -> BookRaster:
    record = read_json(manifest_path(book))
    if record is None:
        raise FileNotFoundError(
            f"'{book}' has not been rendered yet — run the render stage first."
        )
    return BookRaster.from_dict(record)


def main(argv: list[str]) -> int:
    if not argv:
        print("usage: python render.py <file.pdf | directory>")
        return 1

    target = Path(argv[0])
    if target.is_dir():
        pdfs = sorted(target.glob("*.pdf"))
        if not pdfs:
            print(f"no PDFs in {target}")
            return 1
    elif target.suffix.lower() == ".pdf":
        pdfs = [target]
    else:
        print(f"not a PDF or a directory: {target}")
        return 1

    for pdf in pdfs:
        render_book(pdf)
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
