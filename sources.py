from __future__ import annotations

import json
import re
import urllib.error
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ElementTree
from dataclasses import dataclass
from pathlib import Path

from cache import book_dir, write_json
from config import BOOKS_DIR
from progress import Reporter

ARXIV_API = "https://export.arxiv.org/api/query?id_list={identifier}"
ARXIV_PDF = "https://arxiv.org/pdf/{identifier}.pdf"

USER_AGENT = "paper-to-epub (+https://github.com/Elghoudani/paper-to-epub)"

ARXIV_ID = re.compile(
    r"(?:arxiv\.org/(?:abs|pdf|html)/|arxiv:)?"
    r"(\d{4}\.\d{4,5}(?:v\d+)?|[a-z-]+(?:\.[A-Z]{2})?/\d{7}(?:v\d+)?)"
    r"(?:\.pdf)?$",
    re.IGNORECASE,
)
IS_URL = re.compile(r"^https?://", re.IGNORECASE)

ATOM = "{http://www.w3.org/2005/Atom}"
ARXIV_NS = "{http://arxiv.org/schemas/atom}"

DOWNLOAD_CHUNK = 64 * 1024


@dataclass
class PaperSource:

    pdf_path: Path
    metadata: dict | None = None

    @property
    def book(self) -> str:
        return self.pdf_path.stem


def _tidy(text: str | None) -> str:
    return re.sub(r"\s+", " ", text or "").strip()


def arxiv_identifier(target: str) -> str | None:
    match = ARXIV_ID.search(target.strip())
    return match.group(1) if match else None


def fetch_arxiv_metadata(identifier: str, reporter: Reporter) -> dict | None:
    request = urllib.request.Request(
        ARXIV_API.format(identifier=identifier),
        headers={"User-Agent": USER_AGENT},
    )
    try:
        with urllib.request.urlopen(request, timeout=20) as response:
            feed = ElementTree.fromstring(response.read())
    except (urllib.error.URLError, ElementTree.ParseError, OSError) as exc:
        reporter.note(f"arXiv metadata unavailable ({exc})")
        return None

    entry = feed.find(f"{ATOM}entry")
    if entry is None or entry.find(f"{ATOM}title") is None:
        return None

    authors = [
        _tidy(node.findtext(f"{ATOM}name"))
        for node in entry.findall(f"{ATOM}author")
    ]
    category = entry.find(f"{ARXIV_NS}primary_category")

    pdf_link = None
    for link in entry.findall(f"{ATOM}link"):
        if link.get("title") == "pdf":
            pdf_link = link.get("href")
            break

    return {
        "arxiv_id": identifier,
        "title": _tidy(entry.findtext(f"{ATOM}title")),
        "authors": [name for name in authors if name],
        "abstract": _tidy(entry.findtext(f"{ATOM}summary")),
        "published": entry.findtext(f"{ATOM}published") or "",
        "updated": entry.findtext(f"{ATOM}updated") or "",
        "primary_category": (category.get("term") if category is not None else ""),
        "pdf_url": pdf_link or ARXIV_PDF.format(identifier=identifier),
    }


def download(url: str, destination: Path, reporter: Reporter) -> Path:
    destination.parent.mkdir(parents=True, exist_ok=True)
    request = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})

    with urllib.request.urlopen(request, timeout=60) as response:
        declared = int(response.headers.get("Content-Length") or 0)
        received = 0
        partial = destination.with_suffix(destination.suffix + ".part")

        with partial.open("wb") as handle:
            while True:
                chunk = response.read(DOWNLOAD_CHUNK)
                if not chunk:
                    break
                handle.write(chunk)
                received += len(chunk)
                if declared:
                    reporter.step(received, declared)

    if not received:
        partial.unlink(missing_ok=True)
        raise RuntimeError(f"nothing was returned by {url}")

    partial.replace(destination)
    reporter.note(f"{received / 1_000_000:.1f} MB → {destination.name}")
    return destination


def _safe_stem(url: str) -> str:
    name = Path(urllib.parse.urlparse(url).path).stem
    cleaned = re.sub(r"[^A-Za-z0-9._-]+", "_", name).strip("._")
    return cleaned[:120] or "paper"


def resolve(target: str, reporter: Reporter | None = None) -> PaperSource:
    reporter = reporter or Reporter()
    text = target.strip().strip('"').strip("'")

    local = Path(text).expanduser()
    if local.is_file():
        return PaperSource(pdf_path=local.resolve())

    if not IS_URL.match(text):
        stem = text[:-4] if text.lower().endswith(".pdf") else text
        in_books = BOOKS_DIR / f"{stem}.pdf"
        if in_books.is_file():
            return PaperSource(pdf_path=in_books.resolve())

    identifier = arxiv_identifier(text)
    if identifier and (IS_URL.match(text) is None or "arxiv.org" in text.lower()):
        reporter.stage("fetch", f"arXiv:{identifier}")
        metadata = fetch_arxiv_metadata(identifier, reporter)
        if metadata:
            reporter.note(f"{metadata['title']}")
        destination = BOOKS_DIR / f"{identifier.replace('/', '_')}.pdf"
        if not destination.is_file():
            url = (metadata or {}).get("pdf_url") or ARXIV_PDF.format(identifier=identifier)
            download(url, destination, reporter)
        source = PaperSource(pdf_path=destination.resolve(), metadata=metadata)
        if metadata:
            write_json(book_dir(source.book) / "metadata.json", metadata)
        return source

    if IS_URL.match(text):
        reporter.stage("fetch", text)
        destination = BOOKS_DIR / f"{_safe_stem(text)}.pdf"
        if not destination.is_file():
            download(text, destination, reporter)
        return PaperSource(pdf_path=destination.resolve())

    raise FileNotFoundError(
        f"'{target}' is not a file, a folder, an arXiv id, or a URL."
    )


def collect(target: str, reporter: Reporter | None = None) -> list[PaperSource]:
    folder = Path(target).expanduser()
    if folder.is_dir():
        pdfs = sorted(folder.glob("*.pdf"))
        if not pdfs:
            raise FileNotFoundError(f"no PDFs in {folder}")
        return [PaperSource(pdf_path=pdf.resolve()) for pdf in pdfs]
    return [resolve(target, reporter)]
