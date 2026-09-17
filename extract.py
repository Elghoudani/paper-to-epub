from __future__ import annotations

import json
import re
import sys
import time
from collections import defaultdict
from dataclasses import dataclass
from pathlib import Path

import fitz
from PIL import Image, ImageChops, ImageFilter, ImageOps

from config import (
    DATA_DIR,
    IMAGE_FIGURE_FILL_WIDTH,
    IMAGE_FORMULA_PX_PER_PT,
    IMAGE_GRAYSCALE,
    IMAGE_JPEG_QUALITY,
    IMAGE_LINEART_BLACK_POINT,
    IMAGE_LINEART_WHITE_POINT,
    IMAGE_MAX_HEIGHT_PX,
    IMAGE_MAX_WIDTH_PX,
    IMAGE_MIN_RENDER_DPI,
    IMAGE_NATIVE_DPI_HEADROOM,
    IMAGE_PHOTO_AUTOCONTRAST_CUTOFF,
    IMAGE_RENDER_FROM_PDF,
    IMAGE_ROTATE_ASPECT,
    IMAGE_ROTATE_WIDE_TABLES,
    IMAGE_SURVEY_DPI,
    IMAGE_TRIM,
    IMAGE_TRIM_PADDING,
    IMAGE_TRIM_PADDING_PT,
    IMAGE_TRIM_THRESHOLD,
    PRESERVE_INLINE_STYLE,
    REGIONS_SUBDIR,
    RENDER_DPI,
    STITCH_MAX_CHARS,
    STITCH_PARAGRAPHS,
)
from cache import PageGeometry, StageCache, read_json, write_json
from layout import LAYOUT_VERSION, load_layout
from progress import Reporter

EXTRACT_VERSION = 3

IMAGE_CROP_PADDING = 4

JUNK_REPEAT_THRESHOLD = 3
JUNK_TOP_FRAC    = 0.08
JUNK_BOTTOM_FRAC = 0.05
JUNK_LEFT_FRAC   = 0.05
JUNK_RIGHT_FRAC  = 0.05
JUNK_SHORT_MAX_CHARS = 60
JUNK_IMG_MAX_AREA = 200 * 200
JUNK_PATTERN_SOURCES = [
    r"downloaded\s+from",
    r"by\s+guest\s+on",
    r"ip\s+address",
    r"all\s+rights\s+reserved",
    r"©\s*\d{4}",
    r"publishing\s+disclaimer",
    r"check\s+for\s+updates",
    r"crossmark",
]


def _load_junk_patterns() -> re.Pattern:
    sources = list(JUNK_PATTERN_SOURCES)
    extra = Path(__file__).parent / "junk_patterns.txt"
    if extra.exists():
        for line in extra.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if line and not line.startswith("#"):
                sources.append(line)
    return re.compile("(" + "|".join(sources) + ")", re.IGNORECASE)


JUNK_PATTERNS = _load_junk_patterns()


@dataclass
class PageSource:

    layout: dict
    pdf_page: object
    raster: Image.Image
    styles: dict
    images: list
    geometry: PageGeometry

    @property
    def number(self) -> int:
        return self.layout["page_number"]

    def close(self) -> None:
        self.raster.close()


def _open_page(layout: dict, document, geometry: PageGeometry) -> PageSource:
    pdf_page = document[layout["page_index"]]
    try:
        images = pdf_page.get_image_info()
    except Exception:
        images = []
    return PageSource(
        layout=layout,
        pdf_page=pdf_page,
        raster=Image.open(layout["image_path"]).convert("RGB"),
        styles=_build_style_index(pdf_page) if PRESERVE_INLINE_STYLE else {},
        images=images,
        geometry=geometry,
    )


def _extract_page(source: PageSource, regions_dir: Path) -> dict:
    extracted = [
        _extract_region(
            region, source.pdf_page, source.raster,
            source.geometry.width_px, source.geometry.height_px,
            regions_dir, source.styles,
            image_info=source.images,
            scale_pt=source.geometry.points_per_pixel,
        )
        for region in source.layout["regions"]
    ]
    return {
        "page_index": source.layout["page_index"],
        "page_number": source.number,
        **source.geometry.as_dict(),
        "regions": extracted,
    }


def _clean(pages: list[dict], reporter: Reporter) -> None:
    removed = ChromeFilter(pages).apply()
    reporter.note(
        f"dropped {sum(removed.values())} regions — "
        f"{removed['repeating']} repeating, {removed['pattern']} watermark, "
        f"{removed['position']} margin text, {removed['margin_img']} margin image, "
        f"{removed['blank']} blank"
    )

    demoted = _reclassify_headings(pages)
    if demoted:
        reporter.note(f"demoted {demoted} mis-detected heading(s) to body text")

    dropcaps = _fix_dropcaps(pages)
    if dropcaps:
        reporter.note(f"re-joined {dropcaps} drop cap(s)")

    if STITCH_PARAGRAPHS:
        merged = _stitch_paragraphs(pages)
        reporter.note(f"re-joined {merged} paragraph fragment(s) across columns and pages")


def extract_content(book_name: str, reporter: Reporter | None = None) -> dict:
    from render import load_raster

    layout = load_layout(book_name)
    pdf_path = load_raster(book_name).pdf_path
    pages = layout["pages"]

    reporter = reporter or Reporter()
    reporter.stage("extract", f"{len(pages)} pages from {pdf_path.name}")

    regions_dir = DATA_DIR / book_name / REGIONS_SUBDIR
    regions_dir.mkdir(parents=True, exist_ok=True)
    store = StageCache(
        book_name, "extraction",
        source_layout_version=LAYOUT_VERSION,
        extract_version=EXTRACT_VERSION,
    )
    store.dir.mkdir(parents=True, exist_ok=True)

    started = time.perf_counter()
    document = fitz.open(str(pdf_path))
    results: list[dict] = []

    try:
        for position, page_layout in enumerate(pages, start=1):
            geometry = PageGeometry.from_dict(page_layout)
            cached = store.load(page_layout["page_number"], geometry)
            if cached is None:
                source = _open_page(page_layout, document, geometry)
                try:
                    cached = store.save(page_layout["page_number"],
                                        _extract_page(source, regions_dir))
                finally:
                    source.close()
            results.append(cached)
            reporter.step(position, len(pages))
    finally:
        document.close()

    _clean(results, reporter)

    counts = [0, 0, 0]
    for page in results:
        for index, value in enumerate(_count_content(page["regions"])):
            counts[index] += value

    manifest = {
        "book_name": book_name,
        "page_count": len(results),
        "text_regions": counts[0],
        "img_regions": counts[1],
        "pages": results,
    }
    write_json(DATA_DIR / book_name / "extraction_manifest.json", manifest)

    reporter.finish(
        f"{counts[0]} text, {counts[1]} image, {counts[2]} empty "
        f"in {time.perf_counter() - started:.1f}s"
    )
    return manifest


def load_extraction_manifest(book_name: str) -> dict:
    manifest = read_json(DATA_DIR / book_name / "extraction_manifest.json")
    if manifest is None:
        raise FileNotFoundError(
            f"'{book_name}' has not been extracted yet — run the extract stage first."
        )
    return manifest


def _extract_region(
    region: dict,
    pdf_page,
    page_img: Image.Image,
    width_px: int,
    height_px: int,
    regions_dir: Path,
    style_index: dict,
    image_info: list | None = None,
    scale_pt: float = 72.0 / 300,
) -> dict:
    result = dict(region)

    if region["policy"] == "text":
        result["content"] = _extract_text(region, pdf_page, style_index)
    else:
        result["content"] = _extract_image(
            region, page_img, width_px, height_px, regions_dir,
            pdf_page=pdf_page, image_info=image_info, scale_pt=scale_pt,
        )

    return result


_Y_BUCKET = 8.0

_FLAG_SUPERSCRIPT = 1
_FLAG_ITALIC      = 2
_FLAG_BOLD        = 16

_PLAIN = (False, False, False)


def _build_style_index(pdf_page) -> dict:
    try:
        data = pdf_page.get_text("dict")
    except Exception:
        return {}

    index: dict[int, list] = defaultdict(list)
    for block in data.get("blocks", []):
        if block.get("type") != 0:
            continue
        for line in block.get("lines", []):
            for span in line.get("spans", []):
                bbox = span.get("bbox")
                if not bbox:
                    continue
                flags = span.get("flags", 0)
                font  = (span.get("font") or "").lower()
                style = (
                    bool(flags & _FLAG_ITALIC) or "italic" in font or "oblique" in font,
                    bool(flags & _FLAG_BOLD) or "bold" in font or "black" in font,
                    bool(flags & _FLAG_SUPERSCRIPT),
                )
                if style == _PLAIN:
                    continue
                lo = int(bbox[1] // _Y_BUCKET)
                hi = int(bbox[3] // _Y_BUCKET)
                for bucket in range(lo, hi + 1):
                    index[bucket].append((bbox, style))
    return index


def _style_for_word(index: dict, word: tuple) -> tuple:
    if not index:
        return _PLAIN
    cx = (word[0] + word[2]) / 2
    cy = (word[1] + word[3]) / 2
    for bbox, style in index.get(int(cy // _Y_BUCKET), ()):
        if bbox[0] - 0.5 <= cx <= bbox[2] + 0.5 and bbox[1] - 0.5 <= cy <= bbox[3] + 0.5:
            return style
    return _PLAIN


def _extract_text(region: dict, pdf_page, style_index: dict) -> dict:
    x0, y0, x1, y1 = region["bbox_pt"]

    try:
        words = pdf_page.get_text("words")
    except Exception:
        words = []

    kept: list[tuple] = []
    for w in words:
        cx = (w[0] + w[2]) / 2
        cy = (w[1] + w[3]) / 2
        if x0 <= cx <= x1 and y0 <= cy <= y1:
            kept.append(w)

    if not kept:
        return {"type": "text", "text": "", "is_empty": True}

    kept.sort(key=lambda w: (w[5], w[6], w[0]))

    runs = _words_to_runs(kept, style_index)
    text = "".join(r["t"] for r in runs).strip()

    content = {
        "type":     "text",
        "text":     text,
        "is_empty": len(text) == 0,
    }
    if PRESERVE_INLINE_STYLE and any(k in r for r in runs for k in ("i", "b", "s")):
        content["runs"] = runs
    return content


def _words_to_runs(kept: list[tuple], style_index: dict) -> list[dict]:
    runs: list[dict] = []
    prev_line: tuple | None = None

    def append(style: tuple, piece: str) -> None:
        if not piece:
            return
        if runs and runs[-1]["_st"] == style:
            runs[-1]["t"] += piece
        else:
            runs.append({"_st": style, "t": piece})

    for w in kept:
        line_key = (w[5], w[6])
        word     = w[4]
        style    = _style_for_word(style_index, w)

        if prev_line is None or not runs:
            pass
        elif line_key == prev_line:
            append(runs[-1]["_st"], " ")
        else:
            tail = runs[-1]["t"] if runs else ""
            if len(tail) >= 2 and tail.endswith("-") and tail[-2].isalpha():
                runs[-1]["t"] = tail[:-1]
            else:
                append(runs[-1]["_st"], " ")

        append(style, word)
        prev_line = line_key

    cleaned: list[dict] = []
    for run in runs:
        text = _clean_text(run["t"])
        if not text:
            continue
        italic, bold, sup = run["_st"]
        out = {"t": text}
        if italic:
            out["i"] = 1
        if bold:
            out["b"] = 1
        if sup:
            out["s"] = 1
        if cleaned and _flags(cleaned[-1]) == _flags(out):
            cleaned[-1]["t"] += out["t"]
        else:
            cleaned.append(out)

    return cleaned


def _flags(run: dict) -> tuple:
    return (run.get("i", 0), run.get("b", 0), run.get("s", 0))


def _optimize_image_for_eink(
    crop: Image.Image, grayscale: bool, photo: bool = False
) -> Image.Image:
    if photo:
        base = crop.convert("L") if (grayscale or crop.mode in ("1", "L")) else crop.convert("RGB")
        return ImageOps.autocontrast(base, cutoff=IMAGE_PHOTO_AUTOCONTRAST_CUTOFF)

    white, black = IMAGE_LINEART_WHITE_POINT, IMAGE_LINEART_BLACK_POINT
    lut = []
    for i in range(256):
        if i >= white:
            lut.append(255)
        elif i <= black:
            lut.append(0)
        else:
            lut.append(int((i - black) / (white - black) * 255))

    if grayscale or crop.mode in ("1", "L"):
        return crop.convert("L").point(lut)
    return crop.convert("RGB").point(lut * 3)


def _is_grayscale(im: Image.Image) -> bool:
    if im.mode in ("1", "L"):
        return True
    if im.mode != "RGB":
        return False
    r, g, b = im.split()
    return (ImageChops.difference(r, g).getextrema()[1] < 12
            and ImageChops.difference(g, b).getextrema()[1] < 12)


def _trim_to_ink(im: Image.Image) -> Image.Image | None:
    gray = im.convert("L")
    mask = gray.point(lambda v: 255 if v < IMAGE_TRIM_THRESHOLD else 0)
    box  = mask.getbbox()
    if box is None:
        return None

    pad = IMAGE_TRIM_PADDING
    x0 = max(0, box[0] - pad)
    y0 = max(0, box[1] - pad)
    x1 = min(im.width,  box[2] + pad)
    y1 = min(im.height, box[3] + pad)
    return im.crop((x0, y0, x1, y1))


def _looks_photographic(im: Image.Image) -> bool:
    hist  = im.convert("L").histogram()
    total = sum(hist) or 1
    white = sum(hist[240:]) / total
    mid   = sum(hist[40:230]) / total
    return white < 0.55 and mid > 0.25


def _render_pdf_rect(pdf_page, rect, zoom: float) -> Image.Image:
    pix = pdf_page.get_pixmap(
        matrix=fitz.Matrix(zoom, zoom),
        clip=rect,
        colorspace=fitz.csRGB,
        alpha=False,
    )
    return Image.frombytes("RGB", (pix.width, pix.height), pix.samples)


def _native_dpi_for_rect(image_info: list, rect) -> float | None:
    covered = 0.0
    best_dpi = 0.0
    area = abs(rect.get_area())
    if area <= 0:
        return None

    for info in image_info:
        try:
            bbox = fitz.Rect(info["bbox"])
        except Exception:
            continue
        overlap = bbox & rect
        if overlap.is_empty or bbox.width <= 0:
            continue
        covered += abs(overlap.get_area())
        best_dpi = max(best_dpi, info.get("width", 0) / bbox.width * 72)

    if covered / area < 0.6 or best_dpi <= 0:
        return None
    return best_dpi


def _extract_image_from_pdf(
    region: dict,
    pdf_page,
    image_info: list,
    crop_px: tuple,
    scale_pt: float,
) -> Image.Image | None:
    x0, y0, x1, y1 = crop_px
    rect = fitz.Rect(x0 * scale_pt, y0 * scale_pt, x1 * scale_pt, y1 * scale_pt)
    rect = rect & pdf_page.rect
    if rect.is_empty or rect.width <= 0 or rect.height <= 0:
        return None

    try:
        survey_zoom = IMAGE_SURVEY_DPI / 72.0
        survey = _render_pdf_rect(pdf_page, rect, survey_zoom)
    except Exception:
        return None

    ink = survey.convert("L").point(
        lambda v: 255 if v < IMAGE_TRIM_THRESHOLD else 0
    ).getbbox()
    survey.close()
    if ink is None:
        return None

    pad = IMAGE_TRIM_PADDING_PT
    tight = fitz.Rect(
        max(rect.x0, rect.x0 + ink[0] / survey_zoom - pad),
        max(rect.y0, rect.y0 + ink[1] / survey_zoom - pad),
        min(rect.x1, rect.x0 + ink[2] / survey_zoom + pad),
        min(rect.y1, rect.y0 + ink[3] / survey_zoom + pad),
    )
    if tight.width <= 1 or tight.height <= 1:
        return None

    if region.get("label") == "Formula" or not IMAGE_FIGURE_FILL_WIDTH:
        zoom = IMAGE_FORMULA_PX_PER_PT
    else:
        zoom = IMAGE_MAX_WIDTH_PX / tight.width
    zoom = min(zoom, IMAGE_MAX_WIDTH_PX / tight.width,
               IMAGE_MAX_HEIGHT_PX / tight.height)

    native_dpi = _native_dpi_for_rect(image_info, tight)
    if native_dpi:
        zoom = min(zoom, native_dpi * IMAGE_NATIVE_DPI_HEADROOM / 72.0)
    zoom = max(zoom, IMAGE_MIN_RENDER_DPI / 72.0)
    zoom = min(zoom, IMAGE_MAX_WIDTH_PX / tight.width,
               IMAGE_MAX_HEIGHT_PX / tight.height)

    try:
        return _render_pdf_rect(pdf_page, tight, zoom)
    except Exception:
        return None


def _extract_image(
    region: dict,
    page_img: Image.Image,
    width_px: int,
    height_px: int,
    regions_dir: Path,
    pdf_page=None,
    image_info: list | None = None,
    scale_pt: float = 72.0 / 300,
) -> dict:
    x0, y0, x1, y1 = region["bbox_px"]
    label      = region.get("label", "")
    is_formula = label == "Formula"

    if is_formula:
        x0, x1 = _widen_to_column(x0, x1, width_px)

    x0, y0, x1, y1 = _pad(x0, y0, x1, y1, width_px, height_px)

    source = None
    from_pdf = False
    if IMAGE_RENDER_FROM_PDF and pdf_page is not None:
        source = _extract_image_from_pdf(
            region, pdf_page, image_info or [], (x0, y0, x1, y1), scale_pt
        )
        from_pdf = source is not None

    if source is None:
        source = page_img.crop((x0, y0, x1, y1))
        if IMAGE_TRIM:
            trimmed = _trim_to_ink(source)
            source.close()
            if trimmed is None:
                return {"type": "image", "is_empty": True}
            source = trimmed

    if source.width < 8 or source.height < 8:
        source.close()
        return {"type": "image", "is_empty": True}

    photo     = _looks_photographic(source)
    grayscale = IMAGE_GRAYSCALE or _is_grayscale(source)

    out = _optimize_image_for_eink(source, grayscale, photo=photo)
    source.close()

    rotated = False
    if (IMAGE_ROTATE_WIDE_TABLES and label == "Table"
            and out.width / max(1, out.height) >= IMAGE_ROTATE_ASPECT):
        out = out.rotate(-90, expand=True)
        rotated = True

    out = _downscale(out)

    if photo:
        out = out.filter(ImageFilter.UnsharpMask(radius=1.0, percent=90, threshold=3))
    elif not from_pdf:
        out = out.filter(ImageFilter.UnsharpMask(radius=1.0, percent=110, threshold=3))

    if photo:
        filename = f"{region['region_id']}.jpg"
        save_kwargs = {"format": "JPEG", "quality": IMAGE_JPEG_QUALITY,
                       "optimize": True, "progressive": False}
        if out.mode not in ("L", "RGB"):
            out = out.convert("L" if grayscale else "RGB")
    else:
        filename = f"{region['region_id']}.png"
        save_kwargs = {"format": "PNG", "optimize": True}

    out_path = regions_dir / filename
    out.save(str(out_path), **save_kwargs)
    size = (out.width, out.height)
    out.close()

    return {
        "type":           "image",
        "image_path":     out_path.relative_to(DATA_DIR).as_posix(),
        "image_filename": filename,
        "width_px":       size[0],
        "height_px":      size[1],
        "rotated":        rotated,
        "source":         "pdf" if from_pdf else "raster",
    }


def _widen_to_column(left: float, right: float, page_width: int) -> tuple[float, float]:
    if right - left >= page_width * 0.45:
        return 0, page_width
    middle = page_width // 2
    if (left + right) / 2 < middle:
        return 0, middle
    return middle, page_width


def _pad(left: float, top: float, right: float, bottom: float,
         page_width: int, page_height: int) -> tuple[float, float, float, float]:
    return (
        max(0, left - IMAGE_CROP_PADDING),
        max(0, top - IMAGE_CROP_PADDING),
        min(page_width, right + IMAGE_CROP_PADDING),
        min(page_height, bottom + IMAGE_CROP_PADDING),
    )


def _downscale(im: Image.Image) -> Image.Image:
    scale = min(
        IMAGE_MAX_WIDTH_PX / im.width,
        IMAGE_MAX_HEIGHT_PX / im.height,
        1.0,
    )
    if scale >= 1.0:
        return im
    return im.resize(
        (max(1, round(im.width * scale)), max(1, round(im.height * scale))),
        Image.LANCZOS,
    )


_PAGE_MARKER  = re.compile(r"Downloaded from https?://\S+", re.IGNORECASE)
_IP_LINE      = re.compile(r"IP address:.*", re.IGNORECASE)
_WHITESPACE   = re.compile(r"\s+")


def _clean_text(raw: str) -> str:
    if not raw:
        return ""
    text = _PAGE_MARKER.sub("", raw)
    text = _IP_LINE.sub("", text)
    lead  = " " if text[:1].isspace() else ""
    trail = " " if text[-1:].isspace() else ""
    core  = _WHITESPACE.sub(" ", text).strip()
    if not core:
        return " " if (lead or trail) else ""
    return f"{lead}{core}{trail}"


class ChromeFilter:

    REPEATS_ON_PAGES = 3
    MARGINS = {"top": 0.08, "bottom": 0.05, "left": 0.05, "right": 0.05}
    SHORT_TEXT = 60
    SMALL_IMAGE_AREA = 200 * 200

    def __init__(self, pages: list[dict]) -> None:
        self.pages = pages
        self.repeated = self._repeated_text()
        self.removed = {"repeating": 0, "pattern": 0, "position": 0,
                        "margin_img": 0, "blank": 0}

    def _repeated_text(self) -> set[str]:
        appearances: dict[str, set[int]] = defaultdict(set)
        for page in self.pages:
            for region in page["regions"]:
                content = region.get("content", {})
                if content.get("type") != "text":
                    continue
                text = (content.get("text") or "").strip()
                if text:
                    appearances[normalise(text)].add(page["page_number"])
        return {
            text for text, pages in appearances.items()
            if len(pages) >= self.REPEATS_ON_PAGES
        }

    def _in_margin(self, box: list[float], width: int, height: int) -> bool:
        left, top, right, bottom = box
        return (right < width * self.MARGINS["left"]
                or left > width * (1 - self.MARGINS["right"])
                or bottom < height * self.MARGINS["top"]
                or top > height * (1 - self.MARGINS["bottom"]))

    def _verdict(self, region: dict, width: int, height: int) -> str | None:
        content = region.get("content", {})
        box = region.get("bbox_px")
        known_page = bool(width and height and box and len(box) == 4)

        if content.get("type") == "image":
            if content.get("is_empty"):
                return "blank"
            left, top, right, bottom = box if known_page else (0, 0, 0, 0)
            area = (right - left) * (bottom - top)
            if known_page and area < self.SMALL_IMAGE_AREA and self._in_margin(box, width, height):
                return "margin_img"
            return None

        text = (content.get("text") or "").strip()
        if not text:
            return None

        if JUNK_PATTERNS.search(text):
            return "pattern"
        if normalise(text) in self.repeated:
            return "repeating"
        if known_page and len(text) < self.SHORT_TEXT and self._in_margin(box, width, height):
            return "position"
        return None

    def apply(self) -> dict[str, int]:
        for page in self.pages:
            width = page.get("width_px") or 0
            height = page.get("height_px") or 0
            keep: list[dict] = []
            for region in page["regions"]:
                reason = self._verdict(region, width, height)
                if reason is None:
                    keep.append(region)
                else:
                    self.removed[reason] = self.removed.get(reason, 0) + 1
            page["regions"] = keep
        return self.removed


def normalise(text: str) -> str:
    return re.sub(r"\s+", " ", text.lower()).strip()


HEADING_LABELS = ("Section-header", "SectionHeader")

_CAPTIONISH = re.compile(r"^\s*(table|tab\.|fig\.|figure|algorithm|listing|scheme)\b",
                         re.IGNORECASE)
_DANGLING_WORDS = {
    "and", "or", "the", "a", "an", "of", "to", "in", "for", "with", "that",
    "which", "is", "are", "by", "on", "as", "from", "at", "we",
}


def _looks_like_heading(text: str) -> bool:
    t = text.strip()
    if len(t) < 3 or len(t) > 90:
        return False
    if not any(ch.isalpha() for ch in t):
        return False
    if t.endswith((",", ";", "-", "–")):
        return False

    words = t.split()
    if len(words) > 12:
        return False
    if words[-1].lower().strip(".") in _DANGLING_WORDS:
        return False
    if t[0].islower():
        return False
    if len(words) == 1 and len(t) < 4:
        return False
    return True


def _reclassify_headings(page_records: list[dict]) -> int:
    changed = 0
    for page in page_records:
        for region in page["regions"]:
            if region.get("label") not in HEADING_LABELS:
                continue
            content = region.get("content", {})
            if content.get("type") != "text":
                continue
            text = (content.get("text") or "").strip()
            if not text:
                continue
            if _CAPTIONISH.match(text) and len(text.split()) > 2:
                region["label"] = "Caption"
                changed += 1
            elif not _looks_like_heading(text):
                region["label"] = "Text"
                changed += 1
    return changed


_DROPCAP = re.compile(r"^([A-Z])\s+([A-Z]{1,9})(?=\s+[a-z])")


def _fix_dropcaps(page_records: list[dict]) -> int:
    fixed = 0
    for page in page_records:
        for region in page["regions"]:
            if region.get("label") not in BODY_LABELS:
                continue
            content = region.get("content", {})
            if content.get("type") != "text":
                continue
            text = content.get("text") or ""
            new_text = _DROPCAP.sub(r"\1\2", text, count=1)
            if new_text == text:
                continue
            content["text"] = new_text
            runs = content.get("runs")
            if runs:
                content["runs"] = _join_dropcap_runs(runs)
            fixed += 1
    return fixed


def _join_dropcap_runs(runs: list[dict]) -> list[dict]:
    full = "".join(r["t"] for r in runs)
    match = _DROPCAP.match(full)
    if not match:
        return runs

    gap_start, gap_end = 1, match.start(2)
    out: list[dict] = []
    pos = 0
    for run in runs:
        text  = run["t"]
        start, end = pos, pos + len(text)
        pos = end
        lo, hi = max(start, gap_start), min(end, gap_end)
        if lo < hi:
            text = text[: lo - start] + text[hi - start :]
        if text:
            out.append({**run, "t": text})
    return out


BODY_LABELS  = ("Text", "TextInlineMath")
BREAK_LABELS = HEADING_LABELS + ("Title", "List-item", "ListItem")

_TERMINAL = ".?!:;”’\"')"
_NEW_BLOCK = re.compile(r"^\s*(\[\d+\]|\(\d+\)|\d+\.\s|[•▪–—]\s)")


def _is_continuation(prev: str, nxt: str) -> bool:
    if not prev or not nxt:
        return False
    if _NEW_BLOCK.match(nxt):
        return False
    if prev.endswith("-") and len(prev) > 1 and prev[-2].isalpha():
        return True
    if prev[-1] in _TERMINAL:
        return nxt[0].islower()
    return True


def _stitch_paragraphs(page_records: list[dict]) -> int:
    flat = [(page, region) for page in page_records for region in page["regions"]]

    prev_region: dict | None = None
    dropped: set[int] = set()
    merged = 0

    for _page, region in flat:
        label   = region.get("label", "")
        content = region.get("content", {})

        if label in BREAK_LABELS:
            prev_region = None
            continue
        if content.get("type") != "text" or content.get("is_empty"):
            continue
        if label not in BODY_LABELS:
            continue

        text = (content.get("text") or "").strip()
        if not text:
            continue

        if prev_region is not None:
            prev_content = prev_region["content"]
            prev_text    = prev_content["text"]
            if (len(prev_text) + len(text) <= STITCH_MAX_CHARS
                    and _is_continuation(prev_text, text)):
                _merge_into(prev_content, content)
                dropped.add(id(region))
                merged += 1
                continue

        prev_region = region

    if dropped:
        for page in page_records:
            page["regions"] = [r for r in page["regions"] if id(r) not in dropped]

    return merged


def _merge_into(target: dict, extra: dict) -> None:
    head = target["text"]
    tail = extra["text"].strip()

    if head.endswith("-") and len(head) > 1 and head[-2].isalpha():
        target["text"] = head[:-1] + tail
        joiner = ""
        strip_hyphen = True
    else:
        target["text"] = f"{head} {tail}"
        joiner = " "
        strip_hyphen = False

    head_runs = target.get("runs")
    tail_runs = extra.get("runs")
    if head_runs is None and tail_runs is None:
        return
    head_runs = list(head_runs or [{"t": head}])
    tail_runs = list(tail_runs or [{"t": tail}])

    if strip_hyphen and head_runs[-1]["t"].endswith("-"):
        head_runs[-1] = {**head_runs[-1], "t": head_runs[-1]["t"][:-1]}
    elif joiner:
        head_runs[-1] = {**head_runs[-1], "t": head_runs[-1]["t"].rstrip() + joiner}

    if head_runs and tail_runs and _flags(head_runs[-1]) == _flags(tail_runs[0]):
        head_runs[-1] = {**head_runs[-1], "t": head_runs[-1]["t"] + tail_runs[0]["t"].lstrip()}
        tail_runs = tail_runs[1:]

    target["runs"] = head_runs + tail_runs


def _count_content(regions: list[dict]) -> tuple[int, int, int]:
    tc = ic = ec = 0
    for r in regions:
        content = r.get("content", {})
        t = content.get("type")
        if t == "image":
            if content.get("is_empty"):
                ec += 1
            else:
                ic += 1
        elif t == "text":
            if content.get("is_empty"):
                ec += 1
            else:
                tc += 1
    return tc, ic, ec


if __name__ == "__main__":
    if len(sys.argv) < 2:
        print("usage: python extract.py <book>")
        sys.exit(1)

    extract_content(sys.argv[1])
