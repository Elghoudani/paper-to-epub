from pathlib import Path


BASE_DIR   = Path(__file__).parent
BOOKS_DIR  = BASE_DIR / "books"
DATA_DIR   = BASE_DIR / "data"
OUTPUT_DIR = BASE_DIR / "output"

PAGES_SUBDIR   = "pages"
LAYOUT_SUBDIR  = "layout"
REGIONS_SUBDIR = "regions"


DEVICE_SCREEN_PX = (1072, 1448)


RENDER_DPI = 300


VISUAL_LABELS = frozenset({
    "Picture", "Figure", "Table", "Formula",
    "FigureGroup", "TableOfContents", "Form",
})

TEXT_LABELS = frozenset({
    "Text", "TextInlineMath", "Title", "Caption", "Footnote",
    "Section-header", "SectionHeader",
    "List-item", "ListItem",
})

DISCARDED_LABELS = frozenset({
    "Page-header", "Page-footer", "PageHeader", "PageFooter",
})

LABEL_POLICY: dict[str, str] = {
    **{label: "image" for label in VISUAL_LABELS},
    **{label: "text" for label in TEXT_LABELS},
    **{label: "skip" for label in DISCARDED_LABELS},
}

MIN_REGION_PX = 10


PRESERVE_INLINE_STYLE = True

STITCH_PARAGRAPHS = True

STITCH_MAX_CHARS = 6000


IMAGE_TRIM = True
IMAGE_TRIM_THRESHOLD = 245
IMAGE_TRIM_PADDING   = 8

IMAGE_MAX_WIDTH_PX  = 1200
IMAGE_MAX_HEIGHT_PX = 1600

IMAGE_FIGURE_FILL_WIDTH = True

IMAGE_FORMULA_PX_PER_PT = 3.2

IMAGE_RENDER_FROM_PDF = True

IMAGE_SURVEY_DPI = 120

IMAGE_TRIM_PADDING_PT = 2.5

IMAGE_NATIVE_DPI_HEADROOM = 1.5
IMAGE_MIN_RENDER_DPI = 200

IMAGE_GRAYSCALE = True

IMAGE_LINEART_WHITE_POINT = 238
IMAGE_LINEART_BLACK_POINT = 35
IMAGE_PHOTO_AUTOCONTRAST_CUTOFF = 0.4

IMAGE_JPEG_QUALITY = 88

IMAGE_ROTATE_WIDE_TABLES = True
IMAGE_ROTATE_ASPECT      = 1.8


LABEL_TO_HTML: dict[str, str] = {
    "Title":          "h1",
    "Section-header": "h2",
    "SectionHeader":  "h2",
    "Text":           "p",
    "TextInlineMath": "p",
    "List-item":      "li",
    "ListItem":       "li",
    "Caption":        "p class='caption'",
    "Footnote":       "p class='footnote'",
}

CHAPTER_SPLIT_TOP_LEVEL_ONLY = True

COVER_ENABLED = True

COVER_FONT_CANDIDATES = [
    "/usr/share/fonts/truetype/dejavu/DejaVuSerif.ttf",
    "/usr/share/fonts/truetype/liberation/LiberationSerif-Regular.ttf",
    "/usr/share/fonts/truetype/freefont/FreeSerif.ttf",
    "/System/Library/Fonts/Supplemental/Times New Roman.ttf",
    "C:/Windows/Fonts/times.ttf",
]
COVER_FONT_BOLD_CANDIDATES = [
    "/usr/share/fonts/truetype/dejavu/DejaVuSerif-Bold.ttf",
    "/usr/share/fonts/truetype/liberation/LiberationSerif-Bold.ttf",
    "/usr/share/fonts/truetype/freefont/FreeSerifBold.ttf",
    "/System/Library/Fonts/Supplemental/Times New Roman Bold.ttf",
    "C:/Windows/Fonts/timesbd.ttf",
]


EPUB_CSS = """
/* ── Body text ───────────────────────────────────────────────────────────── */
body              { line-height: 1.5; margin: 0 0.6em; }
p                 { margin: 0.6em 0; text-indent: 0; }
h1                { font-size: 1.5em; line-height: 1.25; margin: 0.4em 0 0.8em;
                    text-align: left; }
h2                { font-size: 1.25em; margin: 1.4em 0 0.5em; clear: both;
                    page-break-after: avoid; }
h3                { font-size: 1.05em; font-style: italic; margin: 1.2em 0 0.4em;
                    clear: both; page-break-after: avoid; }
li                { margin: 0.35em 0; }
sup               { font-size: 0.7em; line-height: 0; vertical-align: super; }

p.caption         { font-size: 0.85em; text-align: center; font-style: italic;
                    margin: 0.3em 0 1.2em; clear: both; }
p.footnote        { font-size: 0.8em; margin: 1em 0 0.4em; clear: both; }

/* ── Figures ─────────────────────────────────────────────────────────────── */
/* <figure>, not <div>: older Kindle renderers float a <div> and wrap body text
   alongside a half-width equation. float:none + clear:both stops that. */
figure.visual     { display: block; margin: 1.1em 0; text-align: center;
                    clear: both; float: none; page-break-inside: avoid; }
/* Figures fill the screen: the intrinsic pixels decide sharpness, the width
   decides how much of the panel the picture gets, and on a 6-inch screen the
   answer is all of it. */
figure.visual img { display: block; width: 100%; max-width: 100%; height: auto;
                    margin: 0 auto; float: none; }

figure.formula    { display: block; margin: 0.8em 0; text-align: center;
                    clear: both; float: none; page-break-inside: avoid; }
figure.formula img { display: block; max-width: 100%; height: auto; margin: 0 auto; }

/* A table rotated a quarter turn gets the screen to itself — squeezed into the
   text flow it would be unreadable at any font size. */
figure.rotated    { display: block; margin: 0; text-align: center; clear: both;
                    page-break-before: always; page-break-after: always; }
figure.rotated img { display: block; width: 100%; max-width: 100%; height: auto;
                     margin: 0 auto; }

/* ── Links ───────────────────────────────────────────────────────────────── */
a.fn-ref          { font-size: 0.75em; vertical-align: super;
                    text-decoration: none; }
a.fn-back         { text-decoration: none; margin-right: 0.4em; }
a.xref-link       { text-decoration: none; }
aside.fn-entry    { font-size: 0.85em; margin: 0.6em 0; }
aside.fn-entry p  { margin: 0; }

/* ── Front matter ────────────────────────────────────────────────────────── */
.front-matter     { margin-bottom: 1.5em; }
.paper-title      { font-size: 1.5em; line-height: 1.25; margin-bottom: 0.5em; }
.author-list      { font-style: italic; margin-bottom: 0.4em; }
.meta-badge       { font-size: 0.8em; margin-bottom: 1.2em; }
.abstract-box     { margin: 1.2em 0; }
.abstract-title   { font-weight: bold; font-size: 0.8em; letter-spacing: 0.08em;
                    margin-bottom: 0.3em; }
.abstract-text    { font-size: 0.95em; margin: 0; }
"""
