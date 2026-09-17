from __future__ import annotations

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from extract import (
    _fix_dropcaps,
    _is_continuation,
    _join_dropcap_runs,
    _looks_like_heading,
    _merge_into,
    _stitch_paragraphs,
)
from assemble import Chapter, escape, heading_level
from layout import Region, reading_order
from server import safe_book_name
from sources import arxiv_identifier


class Continuation(unittest.TestCase):

    def test_unterminated_region_continues(self):
        self.assertTrue(_is_continuation("advanced combining methods allow the", "employment of"))

    def test_finished_sentence_does_not(self):
        self.assertFalse(_is_continuation("This sentence is over.", "A new one begins."))

    def test_lowercase_after_abbreviation_continues(self):
        self.assertTrue(_is_continuation("as shown in Fig.", "the left panel shows"))

    def test_reference_entry_starts_fresh(self):
        self.assertFalse(_is_continuation("end of the paragraph.", "[1] Smith, J. et al."))

    def test_bullet_starts_fresh(self):
        self.assertFalse(_is_continuation("the following holds", "• first item"))

    def test_hyphen_break_continues(self):
        self.assertTrue(_is_continuation("consid-", "erable effort"))


class Merging(unittest.TestCase):
    def test_hyphen_join_is_tight(self):
        target = {"text": "consid-"}
        _merge_into(target, {"text": "erable effort"})
        self.assertEqual(target["text"], "considerable effort")

    def test_ordinary_join_gets_one_space(self):
        target = {"text": "allow the"}
        _merge_into(target, {"text": "employment of"})
        self.assertEqual(target["text"], "allow the employment of")

    def test_runs_stay_in_step_with_text(self):
        target = {"text": "see the", "runs": [{"t": "see the"}]}
        _merge_into(target, {"text": "result", "runs": [{"t": "result", "i": 1}]})
        self.assertEqual("".join(r["t"] for r in target["runs"]), target["text"])
        self.assertEqual(target["runs"][-1].get("i"), 1)

    def test_stitching_walks_across_pages(self):
        def region(rid, label, text):
            return {"region_id": rid, "label": label,
                    "content": {"type": "text", "text": text}}

        pages = [
            {"page_number": 1, "regions": [region("a", "Text", "the column ran out")]},
            {"page_number": 2, "regions": [
                region("b", "Picture", ""),
                region("c", "Text", "halfway through this sentence."),
                region("d", "Section-header", "2. Methods"),
                region("e", "Text", "A fresh paragraph."),
            ]},
        ]
        pages[1]["regions"][0]["content"] = {"type": "image"}

        merged = _stitch_paragraphs(pages)
        self.assertEqual(merged, 1)
        self.assertEqual(
            pages[0]["regions"][0]["content"]["text"],
            "the column ran out halfway through this sentence.",
        )
        self.assertEqual([r["region_id"] for r in pages[1]["regions"]], ["b", "d", "e"])


class DropCaps(unittest.TestCase):
    def test_initial_rejoins_its_word(self):
        pages = [{"page_number": 1, "regions": [{
            "label": "Text",
            "content": {"type": "text", "text": "I N wireless transmission, detection"},
        }]}]
        self.assertEqual(_fix_dropcaps(pages), 1)
        self.assertTrue(pages[0]["regions"][0]["content"]["text"].startswith("IN wireless"))

    def test_ordinary_sentence_is_left_alone(self):
        pages = [{"page_number": 1, "regions": [{
            "label": "Text",
            "content": {"type": "text", "text": "A study of the effect"},
        }]}]
        self.assertEqual(_fix_dropcaps(pages), 0)

    def test_join_keeps_styles(self):
        runs = [{"t": "I", "b": 1}, {"t": " N wireless "}, {"t": "signals", "i": 1}]
        joined = _join_dropcap_runs(runs)
        self.assertEqual("".join(r["t"] for r in joined), "IN wireless signals")
        self.assertEqual(joined[-1].get("i"), 1)


class Headings(unittest.TestCase):
    def test_real_headings_pass(self):
        for text in ("INTRODUCTION", "3.1 Model design", "IV. Results", "Appendix E"):
            self.assertTrue(_looks_like_heading(text), text)

    def test_fragments_fail(self):
        for text in ("and", "= 100)", "the results of the study which", "a"):
            self.assertFalse(_looks_like_heading(text), text)

    def test_top_level_opens_a_file_subsection_does_not(self):
        self.assertEqual(heading_level("I. INTRODUCTION"), 2)
        self.assertEqual(heading_level("2 Related work"), 2)
        self.assertEqual(heading_level("References"), 2)
        self.assertEqual(heading_level("3.1 Model design"), 3)
        self.assertEqual(heading_level("Learning rate schedule"), 3)

    def test_heading_alone_is_not_content(self):
        heading = {"label": "Section-header",
                   "content": {"type": "text", "text": "APPENDIX E"}}
        self.assertFalse(Chapter(title="x", regions=[heading]).has_content)
        body = {"label": "Text", "content": {"type": "text", "text": "Something."}}
        self.assertTrue(Chapter(title="x", regions=[heading, body]).has_content)

    def test_blank_image_is_not_content(self):
        blank = {"label": "Figure", "content": {"type": "image", "is_empty": True}}
        self.assertFalse(Chapter(title="x", regions=[blank]).has_content)


class ReadingOrder(unittest.TestCase):

    @staticmethod
    def region(name, left, top, right, bottom):
        return Region(name, "Text", "text", 0, (left, top, right, bottom), (0, 0, 0, 0))

    def test_two_columns_banded_by_full_width_elements(self):
        page = [
            self.region("title", 50, 0, 950, 60),
            self.region("left-1", 50, 100, 480, 300),
            self.region("right-1", 520, 100, 950, 300),
            self.region("figure", 50, 320, 950, 500),
            self.region("left-2", 50, 520, 480, 700),
            self.region("right-2", 520, 520, 950, 700),
        ]
        order = [r.id for r in reading_order(page, 1000)]
        self.assertEqual(
            order, ["title", "left-1", "right-1", "figure", "left-2", "right-2"]
        )

    def test_equal_tops_read_left_to_right(self):
        page = [
            self.region("later", 300, 100, 470, 200),
            self.region("earlier", 50, 100, 280, 200),
        ]
        self.assertEqual([r.id for r in reading_order(page, 1000)], ["earlier", "later"])

    def test_single_column_stays_top_to_bottom(self):
        page = [
            self.region("b", 100, 400, 900, 500),
            self.region("a", 100, 100, 900, 200),
        ]
        self.assertEqual([r.id for r in reading_order(page, 1000)], ["a", "b"])

    def test_reading_order_is_renumbered(self):
        page = [self.region("b", 100, 400, 900, 500), self.region("a", 100, 100, 900, 200)]
        self.assertEqual([r.order for r in reading_order(page, 1000)], [0, 1])


class Sources(unittest.TestCase):
    def test_arxiv_ids_are_recognised(self):
        self.assertEqual(arxiv_identifier("2401.12345"), "2401.12345")
        self.assertEqual(arxiv_identifier("https://arxiv.org/abs/2401.12345v2"), "2401.12345v2")
        self.assertEqual(arxiv_identifier("math/0303109"), "math/0303109")

    def test_a_plain_pdf_url_is_not_an_arxiv_id(self):
        self.assertIsNone(arxiv_identifier("https://example.com/paper.pdf"))


class Escaping(unittest.TestCase):
    def test_quotes_are_escaped(self):
        self.assertNotIn('"', escape('a "quoted" caption'))
        self.assertEqual(escape("<b> & 'x'"), "&lt;b&gt; &amp; &#39;x&#39;")


class UploadNames(unittest.TestCase):

    def test_traversal_is_stripped(self):
        self.assertEqual(safe_book_name("../../etc/passwd.pdf"), "passwd")
        self.assertEqual(safe_book_name("/etc/shadow"), "shadow")

    def test_ordinary_name_survives_readably(self):
        self.assertEqual(safe_book_name("My Paper (final).pdf"), "My Paper _final")

    def test_empty_falls_back(self):
        self.assertEqual(safe_book_name(""), "paper")
        self.assertEqual(safe_book_name("...pdf"), "paper")


if __name__ == "__main__":
    unittest.main()
