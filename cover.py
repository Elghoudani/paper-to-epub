from __future__ import annotations

from io import BytesIO
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont

from config import (
    COVER_FONT_BOLD_CANDIDATES,
    COVER_FONT_CANDIDATES,
    DEVICE_SCREEN_PX,
)


def _first_font(candidates: list[str], size: int) -> ImageFont.FreeTypeFont | None:
    for path in candidates:
        if Path(path).exists():
            try:
                return ImageFont.truetype(path, size)
            except OSError:
                continue
    return None


def _wrap(draw: ImageDraw.ImageDraw, text: str, font, max_width: int) -> list[str]:
    words = text.split()
    lines: list[str] = []
    current = ""
    for word in words:
        trial = f"{current} {word}".strip()
        if draw.textlength(trial, font=font) <= max_width or not current:
            current = trial
        else:
            lines.append(current)
            current = word
    if current:
        lines.append(current)
    return lines


def build_cover(
    title: str,
    authors: list[str] | None = None,
    source_line: str = "",
    size: tuple[int, int] = DEVICE_SCREEN_PX,
) -> bytes | None:
    width, height = size
    margin = round(width * 0.11)
    text_width = width - 2 * margin

    title_size = round(width * 0.062)
    body_size  = round(width * 0.032)
    small_size = round(width * 0.024)

    title_font = _first_font(COVER_FONT_BOLD_CANDIDATES, title_size)
    body_font  = _first_font(COVER_FONT_CANDIDATES, body_size)
    small_font = _first_font(COVER_FONT_CANDIDATES, small_size)
    if title_font is None or body_font is None or small_font is None:
        return None

    canvas = Image.new("L", size, 255)
    draw   = ImageDraw.Draw(canvas)

    lines = _wrap(draw, title.strip() or "Untitled", title_font, text_width)
    while len(lines) > 6 and title_size > round(width * 0.032):
        title_size = round(title_size * 0.85)
        title_font = _first_font(COVER_FONT_BOLD_CANDIDATES, title_size)
        lines = _wrap(draw, title.strip() or "Untitled", title_font, text_width)

    line_height = round(title_size * 1.28)
    block_height = len(lines) * line_height
    y = max(round(height * 0.22), round(height * 0.42) - block_height // 2)

    draw.line([(margin, y - round(height * 0.045)),
               (margin + round(text_width * 0.18), y - round(height * 0.045))],
              fill=0, width=3)

    for line in lines:
        draw.text((margin, y), line, font=title_font, fill=0)
        y += line_height

    if authors:
        shown = ", ".join(authors[:4])
        if len(authors) > 4:
            shown += ", et al."
        y += round(height * 0.02)
        for line in _wrap(draw, shown, body_font, text_width)[:3]:
            draw.text((margin, y), line, font=body_font, fill=70)
            y += round(body_size * 1.35)

    if source_line:
        baseline = height - margin
        draw.text((margin, baseline - small_size), source_line,
                  font=small_font, fill=110)
        draw.line([(margin, baseline - round(small_size * 1.9)),
                   (width - margin, baseline - round(small_size * 1.9))],
                  fill=200, width=2)

    buffer = BytesIO()
    canvas.save(buffer, format="PNG", optimize=True)
    return buffer.getvalue()
