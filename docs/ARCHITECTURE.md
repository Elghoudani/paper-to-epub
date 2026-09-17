# Architecture

How the pipeline is put together, and the reasoning behind the parts that look
odd. If you are changing code, read this first.

## The rule everything else follows

**Maths, figures and tables are images. Always.** Any region labelled
`Picture`, `Figure`, `Table`, `Formula`, `FigureGroup`, `TableOfContents` or
`Form` is rendered to an image and never parsed as text. Body text is the only
thing that becomes text.

This is not a limitation to be fixed later. Flattening an equation into a string
produces noise, and no amount of post-processing recovers it.

## Four stages

```
PDF
 ↓ render.py     PDF → 300 dpi rasters + manifest.json
 ↓ layout.py     pages → regions with reading order
 ↓ extract.py    text → styled runs, visuals → images, + cleanup
 ↓ assemble.py   → EPUB
```

`config.py` holds every setting. `convert.py` runs the four in order and accepts
a path, a bare book name, an arXiv ID or URL, or a PDF URL. `cache.py` decides
when a stage may reuse its previous work, `progress.py` gives the terminal and
the HTTP engine a single progress format, and `sources.py` resolves whatever the
user typed into a PDF on disk.

```
data/<book>/
  manifest.json              stage 1
  pages/page_NNNN.png        stage 1
  layout/page_NNNN.json      stage 2
  layout_manifest.json       stage 2
  extraction/page_NNNN.json  stage 3, raw
  extraction_manifest.json   stage 3, cleaned — what stage 4 reads
  regions/*.png|.jpg         stage 3
output/<book>.epub           stage 4
```

## Caching, and how it is invalidated

Every stage skips work already done, because layout detection takes minutes on a
CPU and nothing downstream should pay for it twice.

Three things invalidate a cache:

1. **`LAYOUT_VERSION`** (`layout.py`) — bump when the layout JSON format or the
   ordering algorithm changes.
2. **`EXTRACT_VERSION`** (`extract.py`) — bump when the extraction format changes.
   Resume requires both stamps to match.
3. **Page dimensions.** Every bounding box is in *raster pixels*. A page
   re-rendered at a different resolution does not make a cache stale, it makes it
   *wrong* — every stored box points at the wrong part of the image, and nothing
   downstream would notice. So stage 1 re-renders any page whose file does not
   match the size `RENDER_DPI` implies, and `StageCache` in `cache.py` discards
   any record whose page dimensions differ from the current raster.

Cleanup is deliberately **not** cached. The per-page extraction JSON holds the
raw extraction; the cleanup passes run on every load. Writing cleaned data back
into the cache means a later run can never reconsider what an earlier pass
deleted — change a junk pattern and it would silently do nothing.

## Stage 2 — reading order without a model

surya returns regions grouped by label, not in reading order. surya ships a
model for ordering; it is not used here (load errors, 200 MB, and geometry does
better on academic layouts).

`reading_order` in `layout.py`:

- A region wider than 60% of the page is a full-width separator.
- Walking top to bottom, each region gets a sort key of `(band, column, top,
  left)`. A full-width region closes the band it meets, takes a band of its own,
  and opens the next one. Sorting on that key puts the left column before the
  right within every band, and equal tops read left to right.

Take page width from the page metadata, never from the region boxes. Estimating
it broke on pages where no region reached the right margin: the estimated centre
landed left of the true centre and every right-column region was classified as
left.

### surya 0.6.0 API notes

```python
from surya.model.detection.model import load_model, load_processor
from surya.settings import settings
from surya.layout import batch_layout_detection

checkpoint = settings.LAYOUT_MODEL_CHECKPOINT
model      = load_model(checkpoint=checkpoint)
processor  = load_processor(checkpoint=checkpoint)
results    = batch_layout_detection(images, model, processor)
```

`LayoutPredictor` does not exist in 0.6.0 — that is 0.7+. `_load_surya()`
handles several versions defensively; change it carefully. Boxes may expose
`.bbox` or `.polygon`, and `.position`, `.order` or nothing at all.

Labels this corpus produces: `Text`, `Formula`, `Section-header`, `List-item`,
`Picture`, `Caption`, `Table`, `Title`, `Footnote`.

Pages rotated 90° or 270° skip detection and become a single full-page figure.

## Stage 3 — text

**Word-level extraction with centre-point filtering.** `get_text("text",
clip=...)` truncates words that start before the clip boundary — in a two-column
paper "Peninsula" comes out as "ula". Extract words, keep those whose centre is
inside the region, sort by block, line, then x.

**Inline style.** Word extraction carries no font information, and the dict
extraction that does clips exactly like `clip=`. So spans are indexed by
vertical position and each word looks up the span containing its centre. Flags:
`1` superscript, `2` italic, `16` bold — plus a font-name check, because plenty
of PDFs ship a bold face as a separate font and never set the flag.

A text region carries both forms:

```python
{"text": "see Escherichia coli",
 "runs": [{"t": "see "}, {"t": "Escherichia coli", "i": 1}]}
```

**Every cleanup pass reads `text`. `runs` exist only for rendering.** Anything
that mutates one must mutate the other — see `_merge_into`, which is tested for
exactly that.

## Stage 3 — cleanup, in order

1. **Junk filter** — frequency (a string on three or more pages is a running
   header), pattern (`junk_patterns.txt` plus built-ins, matched as substrings
   because rotated margin text arrives reordered), and position (short text or
   small images in the outer 5–8%).
2. **Heading sanity** — layout detection labels fragments as `Section-header`.
   A "heading" that is a single lowercase word, has no letters, ends in a
   conjunction or runs past 90 characters is demoted to body text; one opening
   with `Table` or `Fig.` becomes a caption. Without this, headings like "and"
   and "= 100)" each opened their own chapter.
3. **Drop caps** — `I N wireless` → `IN wireless`, by removing the gap between
   runs rather than flattening them, so the paragraph keeps its italics.
4. **Paragraph stitching** — re-joins paragraphs the columns tore apart, across
   the whole book in reading order. A region that does not end in terminal
   punctuation is almost always unfinished; a lowercase opening on the next one
   confirms it. Figures, captions and footnote blocks are *stepped over* rather
   than treated as boundaries — floats and affiliation notes land mid-paragraph
   constantly. Headings, titles and list items do break the chain.

## Stage 3 — images

Order matters:

**survey → trim → render → contrast → rotate wide tables → fit ceiling →
sharpen → encode**

- **Rendered from the PDF, not cropped from the raster.** A cheap 120 dpi survey
  render finds the ink; the real render covers only that rectangle, at the output
  size. Most academic figures and every equation are vector, so this is type
  rather than twice-resampled pixels. The raster crop remains as a fallback.
- **Two sizing rules.** Figures render at the ceiling and display at `width:
  100%` — on paper a plot gets a column of a much larger page, and kept at its
  printed proportion it would take a third of a 6-inch screen. Equations use a
  fixed pixels-per-point so they sit level with the body text; stretching a
  three-symbol formula to the full width looks absurd.
- **Never render past the source.** When embedded bitmaps cover more than 60% of
  a region, the zoom is capped at their native resolution plus headroom. A figure
  embedded at 116 dpi gains nothing from a 1200 px render but doubles in size.
- **Two contrast curves.** The hard curve (white point 238, black point 35) is
  right for line art and destructive for photographs — it flattens highlights and
  shadows and takes the texture with them. Photographs get a percentile stretch.
  `_looks_photographic` decides from the histogram, and also decides PNG vs JPEG.
- **Sharpen after resizing, never before**, and not at all for vector regions
  rendered at output size — unsharp masking crisp type only adds halos.
- **Formula crops are extended to the full column first**, because detection
  clips long equations, then trimmed back to the ink.

## Stage 4 — assembly

Pure assembly. Every cleanup decision was made in stage 3; do not add cleanup
here.

- **Chapters split at top-level headings only.** Every spine item is a hard page
  break on Kindle, so one file per heading gave 41 page breaks in a 20-page
  paper. Numbered `1.`, roman `I.`, named (`Introduction`, `Appendix`, …) or
  short all-caps open a file; `3.1`-style and everything else become `<h3>`
  inside it, anchored so the contents can nest them.
- **A chapter holding only its heading is folded into the next**, never emitted
  as a dead entry.
- **`<figure>`, not `<div>`**, with `float: none; clear: both` — older readers
  float a `<div>` and wrap body text alongside a half-width equation.
- **Links are injected per styled run**, so the link builder never sees an
  `<em>` and the style wrapper never sees a half-built `<a href>`.
- **Escaping includes quotes**, because the same strings reach attributes.
- **Cover** via `cover.py`, with `create_page=False`: the cover-image property is
  what the library view reads, and an extra cover page is just a blank screen.
  A missing system font skips the cover rather than failing the conversion.

## Tests

`tests/test_cleanup.py` covers the pure logic — the continuation rule, merging,
drop caps, heading classification, escaping, and upload-name sanitising. No PDF,
no model, milliseconds to run.

```bash
python -m unittest discover -s tests
```

What is not covered: anything needing a real PDF. A golden-file check on chapter
and image counts for a known paper would be the obvious next test.
