"""Source-bound regressions; tests never download models or send source data."""
import hashlib
import json
import sys
import tempfile
import threading
import unittest
import zipfile
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / 'backend'))
from exam_backend import documents as d


class DocumentsTests(unittest.TestCase):
    def test_discovery_is_case_insensitive_and_preserves_identity_on_change(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            source = root / 'Course' / 'LECTURE.PDF'
            source.parent.mkdir()
            source.write_bytes(b'old')
            (root / 'Course' / 'exams').mkdir()
            (root / 'Course' / 'exams' / 'ignore.pdf').write_bytes(b'ignore')
            (root / '.cache').mkdir()
            (root / '.cache' / 'ignore.txt').write_text('ignore')
            before = d.discover_sources(root)
            self.assertEqual([s['relative_path'] for s in before], ['Course/LECTURE.PDF'])
            source.write_bytes(b'new')
            after = d.discover_sources(root)
            self.assertEqual(before[0]['id'], after[0]['id'])
            self.assertNotEqual(before[0]['fingerprint'], after[0]['fingerprint'])

    def test_names_that_slugify_identically_stay_distinct(self):
        with tempfile.TemporaryDirectory() as td:
            for name in ['Lecture 1.txt', 'Lecture-1.txt']:
                (Path(td) / name).write_text('Detailed university source material.')
            self.assertEqual(len({s['id'] for s in d.discover_sources(td)}), 2)

    def test_discovery_does_not_follow_external_symlink(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td) / 'input'; root.mkdir()
            external = Path(td) / 'private.txt'; external.write_text('Private information here.')
            (root / 'link.txt').symlink_to(external)
            self.assertEqual(d.discover_sources(root), [])
            with self.assertRaises(d.DocumentError):
                d.discover_sources(root, [str(external)])

    def test_text_cache_uses_content_and_extractor_not_ai_model(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td); source = root / 'input' / 'lecture.MD'; source.parent.mkdir()
            source.write_text('# Gedächtnis\n\nDas Arbeitsgedächtnis verarbeitet neue Informationen.', encoding='utf-8')
            before = source.read_bytes()
            one = d.extract_document(source, root / 'cache')
            two = d.extract_document(source, root / 'cache')
            self.assertFalse(one['cache_hit']); self.assertTrue(two['cache_hit'])
            self.assertNotIn('model', json.dumps(one['cache_key']))
            self.assertEqual(one['blocks'][1]['locator'], {'kind':'paragraph', 'index':2})
            self.assertEqual(source.read_bytes(), before)
            source.write_text('Neue Inhalte über Entwicklungspsychologie und Lernen.')
            self.assertFalse(d.extract_document(source, root / 'cache')['cache_hit'])

    def test_identical_content_cache_does_not_leak_previous_filename_or_id(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td); (root/'in').mkdir()
            a, b = root/'in/a.txt', root/'in/b.txt'
            a.write_text('University memory and learning concepts.'); b.write_bytes(a.read_bytes())
            first = d.extract_document(a, root/'cache')
            second = d.extract_document(b, root/'cache')
            self.assertTrue(second['cache_hit']); self.assertEqual(second['name'], 'b.txt')
            self.assertNotEqual(first['id'], second['id'])

    def test_empty_binary_and_unsupported_material_rejected(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td); (root/'in').mkdir()
            for name, content in [('empty.txt',b''),('binary.txt',b'hello\x00world'),('other.exe',b'exe')]:
                source = root/'in'/name; source.write_bytes(content)
                with self.assertRaises(d.DocumentError): d.extract_document(source,root/'cache')

    def test_cancellation_prevents_cache_write(self):
        with tempfile.TemporaryDirectory() as td:
            root=Path(td); source=root/'source.txt'; source.write_text('Valid university study content.')
            event=threading.Event(); event.set()
            with self.assertRaises(d.ExtractionCanceled): d.extract_document(source,root/'cache',event)
            self.assertFalse((root/'cache').exists())

    def test_mixed_pdf_ocr_keeps_original_page_numbers_and_missing_pages(self):
        with tempfile.TemporaryDirectory() as td:
            root=Path(td); source=root/'scan.pdf'; source.write_bytes(b'%PDF fake')
            class Image:
                def close(self): pass
            pages=[('Das Gedächtnis verarbeitet neue Informationen.',False),('',True),('',True)]
            with patch.object(d,'_pdf_native_pages',return_value=pages), patch.object(d,'_render_pdf_page',return_value=Image()), patch.object(d,'_image_text',side_effect=['Die zweite Seite erklärt die Lernprozesse.','']):
                result=d.extract_document(source,root/'cache')
            self.assertEqual([b['locator']['index'] for b in result['blocks']],[1,2])
            self.assertEqual(result['blocks'][1]['kind'],'ocr')
            self.assertEqual(result['page_count'],3)
            self.assertTrue(any('Seite 3' in w for w in result['warnings']))
            self.assertFalse(list((root/'cache').glob('*.json')))

    def test_page_markers_alone_are_not_usable_content(self):
        with tempfile.TemporaryDirectory() as td:
            root=Path(td); source=root/'scan.pdf'; source.write_bytes(b'%PDF fake')
            with patch.object(d,'_pdf_native_pages',return_value=[('',True)]), patch.object(d,'_render_pdf_page',side_effect=d.DocumentError('OCR fehlt')):
                with self.assertRaises(d.DocumentError): d.extract_document(source,root/'cache')

    def test_missing_ocr_warns_about_omitted_page_and_no_remote_fallback(self):
        with tempfile.TemporaryDirectory() as td:
            root=Path(td); source=root/'scan.pdf'; source.write_bytes(b'%PDF fake')
            with patch.object(d,'_pdf_native_pages',return_value=[('Valid native material about developmental psychology.',False),('',True)]), patch.object(d,'_render_pdf_page',side_effect=d.DocumentError('OCR fehlt')):
                result=d.extract_document(source,root/'cache')
            self.assertEqual(len(result['blocks']),1)
            self.assertTrue(any('OCR fehlt' in w for w in result['warnings']))

    def _office_file(self,path):
        with zipfile.ZipFile(path,'w') as archive: archive.writestr('word/document.xml','<document/>')

    def test_powerpoint_embedded_image_retains_slide_reference(self):
        with tempfile.TemporaryDirectory() as td:
            root=Path(td); source=root/'deck.pptx'; self._office_file(source)
            text=SimpleNamespace(label='text',text='Grundlagen der universitären Gedächtnisforschung.',prov=[SimpleNamespace(page_no=2)])
            picture=SimpleNamespace(label='picture',prov=[SimpleNamespace(page_no=3)],get_image=lambda doc:SimpleNamespace(width=400,height=300))
            doc=SimpleNamespace(iterate_items=lambda:iter([(text,0),(picture,0)]))
            with patch.object(d,'_docling_office',return_value=doc), patch.object(d,'_image_text',return_value='Die Abbildung zeigt die zeitliche Entwicklung der Gedächtnisleistung.'):
                result=d.extract_document(source,root/'cache')
            self.assertEqual([b['locator'] for b in result['blocks']],[{'kind':'slide','index':2},{'kind':'slide','index':3}])
            self.assertIn('[SLIDE 3 TEXT]',result['text'])
            self.assertIsNone(result['page_count'])

    def test_word_sections_never_claim_physical_page_numbers(self):
        with tempfile.TemporaryDirectory() as td:
            root=Path(td); source=root/'lecture.docx'; self._office_file(source)
            item=SimpleNamespace(label='text',text='Psychologische Theorien erklären menschliches Verhalten.',prov=[SimpleNamespace(page_no=9)])
            doc=SimpleNamespace(iterate_items=lambda:iter([(item,0)]))
            with patch.object(d,'_docling_office',return_value=doc): result=d.extract_document(source,root/'cache')
            self.assertEqual(result['blocks'][0]['locator'],{'kind':'section','index':1})
            self.assertNotIn('[PAGE',result['text'])

    def test_external_office_images_rejected_before_conversion(self):
        with tempfile.TemporaryDirectory() as td:
            root=Path(td); source=root/'remote.docx'
            with zipfile.ZipFile(source,'w') as archive:
                archive.writestr('word/_rels/document.xml.rels','<Relationships><Relationship Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/image" TargetMode="External" Target="https://example.invalid/private.png"/></Relationships>')
            with patch.object(d,'_docling_office') as convert:
                with self.assertRaises(d.DocumentError): d.extract_document(source,root/'cache')
                convert.assert_not_called()

    def test_ocr_groups_table_cells_by_line_baseline(self):
        annotations = [('420', 1, [.3,.60,.1,.04]), ('Einheit', 1, [.6,.74,.1,.03]), ('Gruppe', 1, [.1,.73,.1,.05]), ('Mittelwert', 1, [.3,.74,.2,.04]), ('Kontrolle', 1, [.1,.60,.1,.04]), ('ms',1,[.6,.61,.04,.02])]
        self.assertEqual(d._ordered_ocr_lines(annotations),'Gruppe Mittelwert Einheit\nKontrolle 420 ms')

    def test_ocr_retains_vision_reading_direction_for_rotated_text(self):
        annotations = [('Heading',1,[.1,.03,.04,.3]),('First paragraph',1,[.2,.03,.04,.6])]
        self.assertEqual(d._ordered_ocr_lines(annotations),'Heading\nFirst paragraph')

    def test_benchmark_reports_recall_and_order_independently(self):
        import importlib.util
        script = Path(__file__).resolve().parents[2] / 'scripts' / 'benchmark_document_ocr.py'
        spec = importlib.util.spec_from_file_location('document_benchmark', script)
        benchmark = importlib.util.module_from_spec(spec); spec.loader.exec_module(benchmark)
        perfect = benchmark.metrics('Alpha Beta Gamma', 'Alpha Beta Gamma', ['Alpha', 'Beta', 'Gamma'])
        reversed_order = benchmark.metrics('Alpha Beta Gamma', 'Gamma Beta Alpha', ['Alpha', 'Beta', 'Gamma'])
        self.assertEqual(perfect['word_recall'], 1)
        self.assertEqual(perfect['reading_order'], 1)
        self.assertEqual(reversed_order['word_recall'], 1)
        self.assertEqual(reversed_order['reading_order'], 0)
        self.assertLess(reversed_order['character_similarity'], 1)

    def test_source_changed_mid_extraction_is_not_cached(self):
        with tempfile.TemporaryDirectory() as td:
            root=Path(td); source=root/'input.txt'; source.write_text('Original university material.')
            original=d._extract_text
            def changed(path,event):
                value=original(path,event); path.write_text('Changed university material.'); return value
            with patch.object(d,'_extract_text',side_effect=changed):
                with self.assertRaises(d.DocumentError): d.extract_document(source,root/'cache')
            self.assertFalse((root/'cache').exists())

if __name__=='__main__': unittest.main()
