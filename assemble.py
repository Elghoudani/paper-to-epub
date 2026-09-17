from __future__ import annotations

import json
import re
import sys
from dataclasses import dataclass, field
from pathlib import Path

from ebooklib import epub

from config import (
    CHAPTER_SPLIT_TOP_LEVEL_ONLY,
    COVER_ENABLED,
    DATA_DIR,
    EPUB_CSS,
    LABEL_TO_HTML,
    OUTPUT_DIR,
    REGIONS_SUBDIR,
)
from extract import load_extraction_manifest
from links import LinkIndex, NoteIndex
from progress import Reporter

HEADINGS = ("Section-header", "SectionHeader")
LIST_ITEMS = ("List-item", "ListItem")

MEDIA_TYPES = {".png": "image/png", ".jpg": "image/jpeg", ".jpeg": "image/jpeg"}

SUBSECTION = re.compile(r"^\s*\d+(?:\.\d+)+")
NUMBERED_SECTION = re.compile(r"^\s*\d+[.)]?\s+\S")
ROMAN_SECTION = re.compile(r"^\s*[IVXLC]+[.)]\s+\S")
NAMED_SECTION = re.compile(
    r"^\s*(abstract|introduction|background|related\s+work|methods?|methodology|"
    r"materials|results?|experiments?|evaluation|discussion|conclusions?|"
    r"acknowledg\w*|references|bibliography|appendix|appendices|supplementary)\b",
    re.IGNORECASE,
)

AUTOLINK = re.compile(
    r"(?P<cite>\[(?P<cite_body>\d[\d\s,–—-]*)\])"
    r"|(?P<fig>\b(?:figs?\.|figs?|figures?)\s*(?P<fig_n>\d+[a-z]?)\b)"
    r"|(?P<tbl>\b(?:tabs?\.|tables?)\s*(?P<tbl_n>\d+|[ivxlcdm]+)\b)"
    r"|(?P<eq>\b(?:eqs?\.|equations?)\s*\(?(?P<eq_n>\d+)\)?)"
    r"|(?P<sec>\b(?:secs?\.|sections?)\s*(?P<sec_n>\d+(?:\.\d+)*)\b)",
    re.IGNORECASE,
)
CITE_PIECES = re.compile(r"([,\s–—-]+)")


def escape(text: str) -> str:
    return (
        text.replace("&", "&amp;")
        .replace("<", "&lt;")
        .replace(">", "&gt;")
        .replace('"', "&quot;")
        .replace("'", "&#39;")
    )


def shorten(text: str, limit: int) -> str:
    return text if len(text) <= limit else text[: limit - 1] + "…"


def heading_level(text: str) -> int:
    stripped = text.strip()
    if SUBSECTION.match(stripped):
        return 3
    if NUMBERED_SECTION.match(stripped) or ROMAN_SECTION.match(stripped):
        return 2
    if NAMED_SECTION.match(stripped):
        return 2
    letters = [character for character in stripped if character.isalpha()]
    if letters and all(character.isupper() for character in letters) and len(stripped.split()) <= 6:
        return 2
    return 3 if CHAPTER_SPLIT_TOP_LEVEL_ONLY else 2


@dataclass
class Chapter:

    title: str
    regions: list[dict] = field(default_factory=list)
    subsections: list[tuple[str, str]] = field(default_factory=list)
    front_matter: dict | None = None
    filename: str = ""

    @property
    def has_content(self) -> bool:
        if self.front_matter:
            return True
        for region in self.regions:
            if region.get("label") in HEADINGS:
                continue
            content = region.get("content", {})
            if content.get("is_empty"):
                continue
            if content.get("type") == "image":
                return True
            if content.get("type") == "text" and content.get("text"):
                return True
        return False


def gather_regions(manifest: dict) -> list[dict]:
    regions: list[dict] = []
    for page in manifest["pages"]:
        regions.extend(drop_nested_images(page["regions"], page.get("width_px", 0)))
    return regions


def crop_bounds(region: dict, page_width: int) -> tuple[float, float, float, float]:
    left, top, right, bottom = region["bbox_px"]
    if region.get("label") == "Formula" and page_width:
        if right - left >= page_width * 0.45:
            return 0, top, page_width, bottom
        middle = page_width // 2
        if (left + right) / 2 < middle:
            return 0, top, middle, bottom
        return middle, top, page_width, bottom
    return left, top, right, bottom


def drop_nested_images(regions: list[dict], page_width: int) -> list[dict]:
    images = [index for index, region in enumerate(regions) if region.get("policy") == "image"]
    if len(images) < 2:
        return regions

    bounds = {index: crop_bounds(regions[index], page_width) for index in images}
    areas = {
        index: max(1.0, (box[2] - box[0]) * (box[3] - box[1]))
        for index, box in bounds.items()
    }

    swallowed: set[int] = set()
    for index in images:
        if index in swallowed:
            continue
        inner = bounds[index]
        for other in images:
            if other == index or other in swallowed or areas[other] < areas[index]:
                continue
            outer = bounds[other]
            overlap_x = min(inner[2], outer[2]) - max(inner[0], outer[0])
            overlap_y = min(inner[3], outer[3]) - max(inner[1], outer[1])
            if overlap_x <= 0 or overlap_y <= 0:
                continue
            if (overlap_x * overlap_y) / areas[index] > 0.65:
                swallowed.add(index)
                break

    return [region for index, region in enumerate(regions) if index not in swallowed]


def group_chapters(regions: list[dict], fallback_title: str) -> list[Chapter]:
    chapters: list[Chapter] = []
    current = Chapter(title=fallback_title)
    anchors = 0

    for region in regions:
        content = region.get("content", {})
        text = (content.get("text") or "").strip()
        is_heading = region.get("label") in HEADINGS and content.get("type") == "text" and text

        if not is_heading:
            current.regions.append(region)
            continue

        level = heading_level(text)
        region["heading_level"] = level

        if level >= 3:
            anchors += 1
            anchor = f"sec-{anchors}"
            region["anchor_id"] = anchor
            current.subsections.append((anchor, shorten(text, 80)))
            current.regions.append(region)
            continue

        carried: list[dict] = []
        if current.has_content:
            chapters.append(current)
        else:
            carried = current.regions

        current = Chapter(title=shorten(text, 80), regions=carried + [region])

    if current.has_content:
        chapters.append(current)
    return chapters


class ChapterWriter:

    def __init__(self, links: LinkIndex, notes: NoteIndex, images: dict, filename: str) -> None:
        self.links = links
        self.notes = notes
        self.images = images
        self.filename = filename

    def render(self, regions: list[dict]) -> str:
        blocks: list[str] = []
        pending_list: list[str] = []

        def flush() -> None:
            if pending_list:
                blocks.append("<ul>\n" + "\n".join(pending_list) + "\n</ul>")
                pending_list.clear()

        for region in regions:
            if self.notes.is_definition(region):
                continue

            content = region.get("content", {})
            if content.get("is_empty"):
                continue

            if content.get("type") == "image":
                flush()
                figure = self._figure(region, content)
                if figure:
                    blocks.append(figure)
                continue

            if content.get("type") != "text" or not content.get("text"):
                continue

            html = self.inline(content)
            label = region.get("label", "Text")

            if label in LIST_ITEMS:
                pending_list.append(f"  <li>{html}</li>")
                continue

            flush()
            blocks.append(self._block(region, label, html))

        flush()
        return "\n".join(blocks)

    def _figure(self, region: dict, content: dict) -> str:
        filename = content.get("image_filename", "")
        if not filename or filename not in self.images:
            return ""
        self.links.place(region, self.filename)

        if content.get("rotated"):
            style = "rotated"
        elif region.get("label") == "Formula":
            style = "formula"
        else:
            style = "visual"

        anchor = region.get("xref_id")
        anchor_attribute = f' id="{anchor}"' if anchor else ""
        alt = escape(region.get("label", "figure").lower())
        return (
            f'<figure{anchor_attribute} class="{style}">'
            f'<img src="images/{filename}" alt="{alt}" />'
            f"</figure>"
        )

    def _block(self, region: dict, label: str, html: str) -> str:
        if label in HEADINGS:
            level = region.get("heading_level", 2)
            anchor = region.get("anchor_id")
            anchor_attribute = f' id="{anchor}"' if anchor else ""
            return f"<h{level}{anchor_attribute}>{html}</h{level}>"

        tag = LABEL_TO_HTML.get(label, "p")
        return f"<{tag}>{html}</{tag.split()[0]}>"

    def inline(self, content: dict) -> str:
        runs = content.get("runs")
        if not runs:
            return self.autolink(content["text"])

        pieces: list[str] = []
        for run in runs:
            text = run.get("t", "")
            if not text:
                continue
            if not text.strip():
                pieces.append(text)
                continue

            lead = " " if text[:1].isspace() else ""
            trail = " " if text[-1:].isspace() else ""
            html = self.autolink(text).strip()
            if run.get("s"):
                html = f"<sup>{html}</sup>"
            if run.get("i"):
                html = f"<em>{html}</em>"
            if run.get("b"):
                html = f"<strong>{html}</strong>"
            pieces.append(f"{lead}{html}{trail}")
        return "".join(pieces)

    def autolink(self, text: str) -> str:
        return AUTOLINK.sub(self._link_for, escape(text))

    def _link_for(self, match: re.Match) -> str:
        whole = match.group(0)

        if match.group("cite"):
            return self._citation(whole, match.group("cite_body"))

        for kind, prefix in (("fig", "fig"), ("tbl", "tbl"), ("eq", "eq")):
            if match.group(kind):
                number = match.group(f"{kind}_n").lower()
                href = self.links.href(f"{prefix}-{number}", self.filename)
                return f'<a class="xref-link" href="{href}">{whole}</a>' if href else whole

        if match.group("sec"):
            href = self.links.section_href(match.group("sec_n"), self.filename)
            return f'<a class="xref-link" href="{href}">{whole}</a>' if href else whole

        return whole

    def _citation(self, whole: str, body: str) -> str:
        rendered: list[str] = []
        linked = False

        for piece in CITE_PIECES.split(body):
            if not piece.isdigit():
                rendered.append(piece)
                continue
            number = int(piece)
            if number not in self.notes:
                rendered.append(piece)
                continue
            anchor, first = self.notes.cite(number, self.filename)
            identifier = f' id="{anchor}"' if first else ""
            rendered.append(
                f'<a{identifier} class="fn-ref" href="notes.xhtml#fn-{number}"'
                f' epub:type="noteref">{piece}</a>'
            )
            linked = True

        return f"[{''.join(rendered)}]" if linked else whole


def render_front_matter(metadata: dict) -> str:
    parts = ['<div class="front-matter">',
             f'<h1 class="paper-title">{escape(metadata.get("title", ""))}</h1>']

    authors = metadata.get("authors") or []
    if authors:
        parts.append(f'<p class="author-list">{escape(", ".join(authors))}</p>')

    facts = []
    if metadata.get("arxiv_id"):
        facts.append(escape(f"arXiv:{metadata['arxiv_id']}"))
    if metadata.get("primary_category"):
        facts.append(escape(metadata["primary_category"]))
    if metadata.get("published"):
        facts.append(escape(metadata["published"][:10]))
    if facts:
        parts.append(f'<p class="meta-badge">{" · ".join(facts)}</p>')

    if metadata.get("abstract"):
        parts.append('<div class="abstract-box">')
        parts.append('<div class="abstract-title">ABSTRACT</div>')
        parts.append(f'<p class="abstract-text">{escape(metadata["abstract"])}</p>')
        parts.append("</div>")

    parts.append("</div>")
    return "\n".join(parts)


def render_notes(notes: NoteIndex) -> str:
    parts = ["<h2>References</h2>"]
    for number in sorted(notes.notes):
        note = notes.notes[number]
        back = (
            f'<a class="fn-back" href="{note.first_seen_in}#fnref-{number}"'
            f' epub:type="backlink">&#8593;</a> '
            if note.first_seen_in else ""
        )
        parts.append(
            f'<aside id="fn-{number}" class="fn-entry" epub:type="footnote">'
            f"<p>{back}<strong>[{number}]</strong> {escape(note.text)}</p>"
            f"</aside>"
        )
    return "\n".join(parts)


def read_metadata(book: str) -> dict | None:
    path = DATA_DIR / book / "metadata.json"
    if not path.is_file():
        return None
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except ValueError:
        return None


def find_title(manifest: dict) -> str | None:
    for page in manifest["pages"][:1]:
        for region in page["regions"]:
            if region.get("label") != "Title":
                continue
            content = region.get("content", {})
            text = (content.get("text") or "").strip()
            if content.get("type") == "text" and len(text) >= 15:
                return text
    return None


def register_images(book: epub.EpubBook, regions: list[dict], folder: Path) -> dict:
    embedded: dict[str, epub.EpubItem] = {}

    for region in regions:
        content = region.get("content", {})
        if content.get("type") != "image" or content.get("is_empty"):
            continue
        filename = content.get("image_filename")
        if not filename or filename in embedded:
            continue

        path = folder / filename
        if not path.is_file():
            hint = content.get("image_path") or ""
            path = DATA_DIR / hint if hint else path
        if not path.is_file():
            continue

        item = epub.EpubItem(
            uid=f"img_{filename}",
            file_name=f"images/{filename}",
            media_type=MEDIA_TYPES.get(path.suffix.lower(), "image/png"),
            content=path.read_bytes(),
        )
        book.add_item(item)
        embedded[filename] = item

    return embedded


def attach_cover(book: epub.EpubBook, title: str, authors: list[str],
                 metadata: dict | None) -> None:
    try:
        from cover import build_cover
    except ImportError:
        return

    facts = []
    if metadata:
        if metadata.get("arxiv_id"):
            facts.append(f"arXiv:{metadata['arxiv_id']}")
        if metadata.get("primary_category"):
            facts.append(metadata["primary_category"])
        if metadata.get("published"):
            facts.append(metadata["published"][:10])

    try:
        art = build_cover(title, authors, " · ".join(facts))
    except Exception as exc:
        print(f"cover skipped: {exc}")
        return

    if not art:
        print("cover skipped: no serif font on this system")
        return

    book.set_cover("cover.png", art, create_page=False)


def build_epub(book_name: str, reporter: Reporter | None = None) -> Path:
    manifest = load_extraction_manifest(book_name)
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    destination = OUTPUT_DIR / f"{book_name}.epub"

    reporter = reporter or Reporter()
    reporter.stage("assemble", f"{manifest['page_count']} pages -> {destination.name}")

    metadata = read_metadata(book_name)
    title = (metadata or {}).get("title") or find_title(manifest) or book_name
    authors = list((metadata or {}).get("authors") or [])

    book = epub.EpubBook()
    book.set_identifier(
        f"arxiv:{metadata['arxiv_id']}" if metadata and metadata.get("arxiv_id")
        else f"paper2epub:{book_name}"
    )
    book.set_title(title)
    book.set_language("en")
    for author in authors:
        book.add_author(author)
    if metadata:
        if metadata.get("abstract"):
            book.add_metadata("DC", "description", metadata["abstract"])
        if metadata.get("published"):
            book.add_metadata("DC", "date", metadata["published"][:10])
        if metadata.get("primary_category"):
            book.add_metadata("DC", "subject", metadata["primary_category"])

    if COVER_ENABLED:
        attach_cover(book, title, authors, metadata)

    stylesheet = epub.EpubItem(
        uid="main_css", file_name="style/main.css",
        media_type="text/css", content=EPUB_CSS,
    )
    book.add_item(stylesheet)

    regions = gather_regions(manifest)

    links = LinkIndex()
    links.scan(regions)
    notes = NoteIndex()
    notes.scan(regions)

    chapters = group_chapters(regions, book_name.replace("_", " ").title())
    if metadata and (metadata.get("abstract") or metadata.get("title")):
        chapters.insert(0, Chapter(title="Title & Abstract", front_matter=metadata))

    for index, chapter in enumerate(chapters):
        chapter.filename = f"chap_{index:04d}.xhtml"
        if not chapter.front_matter:
            links.place_section(chapter.title, chapter.filename)
            for region in chapter.regions:
                links.place(region, chapter.filename)

    embedded = register_images(book, regions, DATA_DIR / book_name / REGIONS_SUBDIR)
    reporter.note(
        f"{len(chapters)} chapters, {len(notes.notes)} references, "
        f"{len(links.targets)} cross-reference targets"
    )

    written: list[epub.EpubHtml] = []
    contents: list = []

    for chapter in chapters:
        if chapter.front_matter:
            body = render_front_matter(chapter.front_matter)
        else:
            writer = ChapterWriter(links, notes, embedded, chapter.filename)
            body = writer.render(chapter.regions)
        if not body.strip():
            continue

        item = _page(book, chapter.title, chapter.filename, body, stylesheet)
        written.append(item)

        entry = epub.Link(chapter.filename, chapter.title, chapter.filename[:-6])
        children = [
            epub.Link(f"{chapter.filename}#{anchor}", label, anchor)
            for anchor, label in chapter.subsections
        ]
        contents.append((entry, children) if children else entry)

    if notes.any:
        item = _page(book, "References", "notes.xhtml", render_notes(notes), stylesheet)
        written.append(item)
        contents.append(epub.Link("notes.xhtml", "References", "references"))

    if not written:
        reporter.note("nothing to write — extraction produced no content")
        return destination

    book.spine = ["nav"] + written
    book.toc = tuple(contents)
    book.add_item(epub.EpubNcx())
    book.add_item(epub.EpubNav())
    epub.write_epub(str(destination), book, {})

    reporter.finish(f"{destination.stat().st_size / 1_000_000:.1f} MB -> {destination}")
    return destination


def _page(book: epub.EpubBook, title: str, filename: str, body: str,
          stylesheet: epub.EpubItem) -> epub.EpubHtml:
    item = epub.EpubHtml(title=title, file_name=filename, lang="en")
    item.content = (
        '<html xmlns:epub="http://www.idpf.org/2007/ops">'
        f"<head><title>{escape(title)}</title></head>"
        f"<body>{body}</body></html>"
    )
    item.add_item(stylesheet)
    book.add_item(item)
    return item


if __name__ == "__main__":
    if len(sys.argv) < 2:
        print("usage: python assemble.py <book>")
        sys.exit(1)
    build_epub(sys.argv[1])
