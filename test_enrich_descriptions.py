"""
test_enrich_descriptions.py
---------------------------
Offline tests for the Atlantia description enrichment. No network — they run the
HTML parser against a fixture mirroring atlantia.sca.org's event-page markup, and
exercise the placeholder/triviality guards.

Run:
    python -m unittest test_enrich_descriptions -v
"""
from __future__ import annotations

import io
import unittest

import enrich_descriptions as ed

# Mirrors the real page: fields are <div class="labelDiv">Label:</div> <div>value</div>
FIXTURE = """
<html><body><div class="entry-content">
  <h2>Allthing <br>(2026-06-20)</h2>
  <h3>Event Info</h3>
  <div class="labelDiv">Name:</div> <div>Allthing</div><br/>
  <div class="labelDiv">Group:</div> <div>Windmasters' Hill</div><br/>
  <div class="labelDiv">Description:</div> <div>The historic Icelandic Al&#254;ingi
       brought a   nation together.</div><br/>
  <div class="labelDiv">Email:</div> <div>steward@example.org</div><br/>
</div></body></html>
"""

NO_DESC_FIXTURE = """
<html><body><div class="entry-content">
  <div class="labelDiv">Name:</div> <div>Mystery Event</div><br/>
</div></body></html>
"""


class TestExtract(unittest.TestCase):
    def test_extracts_description_value(self):
        got = ed.extract_atlantia_description(FIXTURE)
        # whitespace collapsed to single spaces, the þ entity decoded
        self.assertEqual(got, "The historic Icelandic Alþingi brought a nation together.")

    def test_does_not_grab_other_fields(self):
        got = ed.extract_atlantia_description(FIXTURE)
        self.assertNotIn("steward@example.org", got)
        self.assertNotIn("Windmasters", got)

    def test_missing_description_returns_none(self):
        self.assertIsNone(ed.extract_atlantia_description(NO_DESC_FIXTURE))


EAST_FIXTURE = """
<html><body>
<article>
  <h2>A Day in the Park</h2>
  <div class="eventDetailsContent">
    The Shire of Blak Rose cordially invites you to join us at the
    Bandshell Pavilion on Saturday, May 30, 2026.
  </div>
</article>
</body></html>
"""


class TestExtractEast(unittest.TestCase):
    def test_extracts_eventDetailsContent(self):
        got = ed.extract_east_description(EAST_FIXTURE)
        self.assertIn("Shire of Blak Rose cordially invites", got)
        self.assertNotIn("<", got)   # tags stripped

    def test_missing_section_returns_none(self):
        self.assertIsNone(ed.extract_east_description("<html><body>no section</body></html>"))


class TestPlaceholder(unittest.TestCase):
    def test_matches_the_placeholder(self):
        self.assertTrue(ed.PLACEHOLDER_RE.match("Upcoming event in Storvik Event Flyer:"))
        self.assertTrue(ed.PLACEHOLDER_RE.match("Upcoming event in Windmasters' Hill Event Flyer:"))

    def test_ignores_a_real_writeup(self):
        self.assertFalse(ed.PLACEHOLDER_RE.match("Come join us! See the event flyer for details."))


class TestTriviality(unittest.TestCase):
    def test_coming_soon_is_not_real(self):
        self.assertFalse(ed.is_real("Coming soon"))
        self.assertFalse(ed.is_real(""))
        self.assertFalse(ed.is_real(None))
        self.assertFalse(ed.is_real("TBD"))

    def test_substantial_text_is_real(self):
        self.assertTrue(ed.is_real("A full weekend of tournaments, feast, and merriment."))


class TestArtemisiaFBMarker(unittest.TestCase):
    """The Artemisia flyer parser anchors on the 'Facebook Event Page/Link'
    label. Real PDFs have shipped with both spellings ('Facebook' and the
    occasional 'Fecebook' typo) and a couple of label variants, so the regex
    is deliberately tolerant of those exact forms but strict otherwise."""

    def test_matches_standard_event_page_label(self):
        self.assertIsNotNone(ed._ARTEMISIA_FB_MARKER_RE.search("Facebook Event Page"))

    def test_matches_event_link_label(self):
        self.assertIsNotNone(ed._ARTEMISIA_FB_MARKER_RE.search("Facebook Event Link"))

    def test_matches_page_for_this_event_label(self):
        self.assertIsNotNone(
            ed._ARTEMISIA_FB_MARKER_RE.search("Facebook Page for this Event")
        )

    def test_tolerates_fecebook_typo(self):
        # Several flyers contain "Fecebook Event Page" — observed in real PDFs.
        self.assertIsNotNone(ed._ARTEMISIA_FB_MARKER_RE.search("Fecebook Event Page"))

    def test_does_not_match_a_bare_facebook_mention(self):
        # A description that mentions Facebook in passing ('like us on Facebook!')
        # must NOT trip the marker, or we'd lop off most of the prose.
        self.assertIsNone(
            ed._ARTEMISIA_FB_MARKER_RE.search("Follow us on Facebook for updates!")
        )


class TestExtractArtemisia(unittest.TestCase):
    """The PDF extractor itself. We build minimal real PDFs in-memory with
    pypdf so the tests run offline and don't depend on captured-binary
    fixtures."""

    @staticmethod
    def _make_pdf(text: str) -> bytes:
        # pypdf-only fixture builder: lay each line out at 720 - i*14 so the
        # text extractor sees them in order. Two-page docs to exercise the
        # multi-page join.
        from pypdf import PdfWriter
        from pypdf.generic import (
            ArrayObject, ContentStream, DecodedStreamObject,
            DictionaryObject, FloatObject, NameObject, NumberObject,
        )

        # Build one page's content stream from the supplied text lines.
        def _page_dict(writer, lines):
            ops = ["BT /F1 12 Tf "]
            y = 720
            for line in lines:
                escaped = line.replace("\\", "\\\\").replace("(", "\\(").replace(")", "\\)")
                ops.append(f"1 0 0 1 72 {y} Tm ({escaped}) Tj ")
                y -= 14
            ops.append("ET")
            stream = DecodedStreamObject()
            stream.set_data("".join(ops).encode("latin-1"))
            stream_ref = writer._add_object(stream)
            font = DictionaryObject({
                NameObject("/Type"): NameObject("/Font"),
                NameObject("/Subtype"): NameObject("/Type1"),
                NameObject("/BaseFont"): NameObject("/Helvetica"),
            })
            font_ref = writer._add_object(font)
            resources = DictionaryObject({
                NameObject("/Font"): DictionaryObject({NameObject("/F1"): font_ref}),
            })
            page = DictionaryObject({
                NameObject("/Type"): NameObject("/Page"),
                NameObject("/MediaBox"): ArrayObject(
                    [NumberObject(0), NumberObject(0),
                     NumberObject(612), NumberObject(792)]),
                NameObject("/Resources"): resources,
                NameObject("/Contents"): stream_ref,
            })
            return page

        writer = PdfWriter()
        lines = text.splitlines() or [""]
        # Split into two pages so we exercise multi-page joining.
        mid = max(1, len(lines) // 2)
        for chunk in (lines[:mid], lines[mid:] or [""]):
            page = _page_dict(writer, chunk)
            # Use writer's internal add_page logic via the page-tree append.
            page_ref = writer._add_object(page)
            writer._root_object[NameObject("/Pages")][NameObject("/Kids")].append(page_ref)
            writer._root_object[NameObject("/Pages")][NameObject("/Count")] = NumberObject(
                len(writer._root_object[NameObject("/Pages")][NameObject("/Kids")])
            )
        buf = io.BytesIO()
        writer.write(buf)
        return buf.getvalue()

    def test_returns_text_after_facebook_marker(self):
        body = "\n".join([
            "Estrella War XLII",
            "Hosted by the Kingdom of Atenveldt",
            "Facebook Event Page",
            "Join us for a week of fighting, feasting, and fellowship",
            "in the Arizona desert. Site fee includes camping.",
        ])
        pdf = self._make_pdf(body)
        got = ed.extract_artemisia_description(pdf)
        self.assertIsNotNone(got)
        self.assertIn("week of fighting", got)
        self.assertIn("Site fee includes camping", got)
        # Header text before the marker should be excluded.
        self.assertNotIn("Estrella War", got)
        self.assertNotIn("Atenveldt", got)

    def test_returns_none_when_marker_is_missing(self):
        # Fail-silent: rather than ship arbitrary flyer text as the
        # description, return None so the event keeps its placeholder.
        pdf = self._make_pdf("Event flyer\nDate: TBD\nLocation: Coming soon")
        self.assertIsNone(ed.extract_artemisia_description(pdf))

    def test_handles_malformed_pdf(self):
        self.assertIsNone(ed.extract_artemisia_description(b"not a pdf"))
        self.assertIsNone(ed.extract_artemisia_description(b""))

    def test_normalises_whitespace_in_description(self):
        # PDFs frequently embed runs of spaces and stray newlines around the
        # extracted text. We promise single-space-collapsed output.
        body = "\n".join([
            "Facebook Event Link",
            "    Lots    of     spaces here.",
            "And a second line.",
        ])
        got = ed.extract_artemisia_description(self._make_pdf(body))
        self.assertIsNotNone(got)
        self.assertNotIn("  ", got)               # no double-spaces
        self.assertIn("Lots of spaces here", got)


class TestArtemisiaTrigger(unittest.TestCase):
    """The main loop's is_artemisia gate — make sure we trigger when a Drive
    URL has no useful description, and skip when one is already populated."""

    def test_triggers_when_description_is_empty(self):
        url = "https://drive.google.com/file/d/abc123/view"
        desc = ""
        is_artemisia = (ed.ARTEMISIA_DRIVE_HOST in url
                        and (not desc.strip()
                             or desc.strip().lower().endswith(".pdf")
                             or len(desc.strip()) < 50))
        self.assertTrue(is_artemisia)

    def test_triggers_when_description_is_just_pdf_filename(self):
        url = "https://drive.google.com/file/d/abc123/view"
        desc = "estrella-war-flyer.pdf"
        is_artemisia = (ed.ARTEMISIA_DRIVE_HOST in url
                        and (not desc.strip()
                             or desc.strip().lower().endswith(".pdf")
                             or len(desc.strip()) < 50))
        self.assertTrue(is_artemisia)

    def test_skips_when_description_is_substantial(self):
        url = "https://drive.google.com/file/d/abc123/view"
        desc = ("Estrella War XLII brings together fighters and artisans for "
                "a week in the Arizona desert. Join us for archery, fencing, "
                "feast, and dancing each evening.")
        is_artemisia = (ed.ARTEMISIA_DRIVE_HOST in url
                        and (not desc.strip()
                             or desc.strip().lower().endswith(".pdf")
                             or len(desc.strip()) < 50))
        self.assertFalse(is_artemisia)


class TestDriveIdExtraction(unittest.TestCase):
    def test_extracts_id_from_view_url(self):
        m = ed._DRIVE_ID_RE.search("https://drive.google.com/file/d/1AbC-_xyz/view")
        self.assertIsNotNone(m)
        self.assertEqual(m.group(1), "1AbC-_xyz")

    def test_extracts_id_from_edit_url(self):
        m = ed._DRIVE_ID_RE.search("https://drive.google.com/file/d/xyz_777/edit?usp=sharing")
        self.assertIsNotNone(m)
        self.assertEqual(m.group(1), "xyz_777")


if __name__ == "__main__":
    unittest.main(verbosity=2)
