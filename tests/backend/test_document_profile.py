"""Structural descriptions must not invent pages, decode images, or drop empty slides."""
from pathlib import Path
import sys
import tempfile
import threading
import unittest
import zipfile
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / 'backend'))
from exam_backend import documents as d


class DocumentProfileTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)

    def tearDown(self):
        self.temp.cleanup()

    def test_word_counts_actual_structure_and_repeated_image_without_page_numbers(self):
        path = self.root / 'source.docx'
        with zipfile.ZipFile(path, 'w') as archive:
            archive.writestr('word/document.xml', '''<w:document xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main" xmlns:a="http://schemas.openxmlformats.org/drawingml/2006/main" xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships"><w:body><w:p/><w:tbl><w:tr><w:tc><w:p/></w:tc></w:tr></w:tbl><a:blip r:embed="img"/><a:blip r:embed="img"/><w:sectPr/></w:body></w:document>''')
            archive.writestr('word/_rels/document.xml.rels', '<Relationships><Relationship Id="img" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/image" Target="media/image1.png"/></Relationships>')
            archive.writestr('word/media/image1.png', b'deliberately not a decodable image')
        result = d.profile_document(path)
        self.assertIsNone(result['pages'])
        self.assertEqual((result['paragraphs'], result['tables'], result['sections']), (2, 1, 1))
        self.assertEqual((result['image_occurrences'], result['unique_images']), (2, 1))

    def test_powerpoint_counts_empty_final_slide(self):
        path = self.root / 'deck.pptx'
        with zipfile.ZipFile(path, 'w') as archive:
            archive.writestr('ppt/presentation.xml', '<p:presentation xmlns:p="http://schemas.openxmlformats.org/presentationml/2006/main" xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships"><p:sldIdLst><p:sldId id="1" r:id="one"/><p:sldId id="2" r:id="two"/></p:sldIdLst></p:presentation>')
            archive.writestr('ppt/_rels/presentation.xml.rels', '<Relationships><Relationship Id="one" Target="slides/slide1.xml"/><Relationship Id="two" Target="slides/slide2.xml"/></Relationships>')
            archive.writestr('ppt/slides/slide1.xml', '<p:sld xmlns:p="http://schemas.openxmlformats.org/presentationml/2006/main" xmlns:a="http://schemas.openxmlformats.org/drawingml/2006/main"><a:p/><a:tbl/></p:sld>')
            archive.writestr('ppt/slides/slide2.xml', '<sld/>')
        result = d.profile_document(path)
        self.assertEqual((result['slides'], result['paragraphs'], result['tables']), (2, 1, 1))
        self.assertIsNone(result['pages'])
        self.assertEqual(result['image_occurrences'], 0)

    def test_pdf_repeated_placements_and_unique_asset(self):
        from pypdf import PdfWriter
        from pypdf.generic import DecodedStreamObject, DictionaryObject, NameObject, NumberObject
        path = self.root / 'placed.pdf'
        writer = PdfWriter()
        page = writer.add_blank_page(200, 200)
        image = DecodedStreamObject(); image.set_data(b'\xff\x00\x00')
        image.update({NameObject('/Type'): NameObject('/XObject'), NameObject('/Subtype'): NameObject('/Image'), NameObject('/Width'): NumberObject(1), NameObject('/Height'): NumberObject(1), NameObject('/BitsPerComponent'): NumberObject(8), NameObject('/ColorSpace'): NameObject('/DeviceRGB')})
        page[NameObject('/Resources')] = DictionaryObject({NameObject('/XObject'): DictionaryObject({NameObject('/Im'): writer._add_object(image)})})
        content = DecodedStreamObject(); content.set_data(b'q 10 0 0 10 0 0 cm /Im Do Q q 10 0 0 10 20 20 cm /Im Do Q')
        page[NameObject('/Contents')] = writer._add_object(content)
        writer.add_blank_page(200, 200)
        with path.open('wb') as handle:
            writer.write(handle)
        result = d.profile_document(path)
        self.assertEqual((result['pages'], result['image_occurrences'], result['unique_images']), (2, 2, 1))
        self.assertTrue(any('Vektordiagramme' in warning for warning in result['warnings']))

    def test_unreadable_pdf_reports_unknown_images_instead_of_zero(self):
        path = self.root / 'bad.pdf'; path.write_bytes(b'broken')
        with patch('pdfplumber.open', side_effect=ValueError('broken')):
            result = d.profile_document(path)
        self.assertIsNone(result['image_occurrences'])
        self.assertTrue(result['warnings'])

    def test_image_reads_dimensions_without_decoding_pixels(self):
        from PIL import Image
        path = self.root / 'image.png'
        Image.new('RGB', (40, 30)).save(path)
        with patch.object(Image.Image, 'load', side_effect=AssertionError('Must not decode pixels')):
            result = d.profile_document(path)
        self.assertEqual((result['width'], result['height']), (40, 30))

    def test_metadata_failure_does_not_discard_extracted_text_and_cache_contains_metadata(self):
        path = self.root / 'source.txt'; path.write_text('Lernen verbindet Begriffe mit neuen Erfahrungen.')
        with patch.object(d, 'profile_document', return_value={'pages': None, 'warnings': ['Beschreibung nicht verfügbar']}):
            result = d.extract_document(path, self.root / 'cache')
        self.assertIn('Lernen', result['text'])
        self.assertIn('Beschreibung', result['metadata']['warnings'][0])
        cached = d.extract_document(path, self.root / 'cache')
        self.assertTrue(cached['cache_hit'])
        self.assertEqual(cached['metadata'], result['metadata'])

    def test_cancellation_propagates(self):
        event = threading.Event(); event.set()
        with self.assertRaises(d.ExtractionCanceled):
            d.profile_document(self.root / 'missing.txt', event)


if __name__ == '__main__':
    unittest.main()
