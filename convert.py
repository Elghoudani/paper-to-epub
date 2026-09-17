from __future__ import annotations

import argparse
import sys
import time
import traceback
from dataclasses import dataclass
from pathlib import Path

from config import OUTPUT_DIR
from progress import Reporter
from sources import PaperSource, collect


@dataclass
class Outcome:
    name: str
    epub: Path | None
    seconds: float
    error: str | None = None

    @property
    def ok(self) -> bool:
        return self.error is None


def convert_paper(source: PaperSource, batch_size: int = 8, force: bool = False,
                  reporter: Reporter | None = None) -> Path:
    from render import render_book
    from layout import detect_layout
    from extract import extract_content
    from assemble import build_epub

    reporter = reporter or Reporter()
    book = source.book
    destination = OUTPUT_DIR / f"{book}.epub"

    if destination.exists() and not force:
        reporter.note(f"{book}.epub already exists — pass --force to rebuild")
        return destination

    render_book(source.pdf_path, reporter)
    detect_layout(book, batch_size=batch_size, reporter=reporter)
    extract_content(book, reporter=reporter)
    return build_epub(book, reporter=reporter)


def _run_one(source: PaperSource, batch_size: int, force: bool) -> Outcome:
    started = time.perf_counter()
    reporter = Reporter()
    try:
        epub = convert_paper(source, batch_size=batch_size, force=force, reporter=reporter)
        return Outcome(source.book, epub, time.perf_counter() - started)
    except Exception:
        traceback.print_exc()
        return Outcome(source.book, None, time.perf_counter() - started,
                       error="conversion failed — see the traceback above")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="convert.py",
        description="Convert research papers into EPUBs for e-ink readers.",
    )
    parser.add_argument(
        "target",
        help="a PDF, a folder of PDFs, a book name from books/, an arXiv id or URL",
    )
    parser.add_argument(
        "--batch-size", type=int, default=8, metavar="N",
        help="pages sent to the layout model at once (default 8; lower it if memory runs out)",
    )
    parser.add_argument(
        "--force", action="store_true",
        help="rebuild the EPUB even if one already exists",
    )
    arguments = parser.parse_args(argv)

    try:
        sources = collect(arguments.target)
    except (FileNotFoundError, RuntimeError) as exc:
        print(f"error: {exc}")
        return 1

    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    if len(sources) > 1:
        print(f"{len(sources)} papers to convert")

    started = time.perf_counter()
    outcomes = [_run_one(source, arguments.batch_size, arguments.force)
                for source in sources]
    total = time.perf_counter() - started

    print()
    for outcome in outcomes:
        if outcome.ok:
            size = outcome.epub.stat().st_size / 1_000_000 if outcome.epub else 0
            print(f"  ok      {outcome.name}  ({outcome.seconds:.0f}s, {size:.1f} MB)")
        else:
            print(f"  failed  {outcome.name}  ({outcome.error})")
    if len(outcomes) > 1:
        print(f"\n{sum(1 for o in outcomes if o.ok)}/{len(outcomes)} converted in {total:.0f}s")

    return 0 if all(outcome.ok for outcome in outcomes) else 1


if __name__ == "__main__":
    sys.exit(main())
