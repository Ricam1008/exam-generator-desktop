"""Synthetic format smoke tests, enabled when bundled document tools are installed."""
import io
import os
import socket
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0,str(Path(__file__).resolve().parents[2]/'backend'))
from exam_backend import documents as d

READY = os.environ.get('EXAM_DOCUMENT_RUNTIME_TESTS') == '1' and all(d._installed(m) for m in ('docling','ocrmac','PIL','pypdf','pdfplumber','pypdfium2','docx','pptx')) and sys.platform=='darwin'


@unittest.skipUnless(READY,'Set EXAM_DOCUMENT_RUNTIME_TESTS=1 with packaged Mac document tools and normal Vision access')
class DocumentRuntimeTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory(); self.root=Path(self.temp.name)
        self.source=self.root/'input'; self.source.mkdir(); self.cache=self.root/'cache'
        self.environment=patch.dict(os.environ,{'HF_HUB_OFFLINE':'1','TRANSFORMERS_OFFLINE':'1'})
        self.environment.start()
        self.network=patch.object(socket.socket,'connect',side_effect=AssertionError('Document processing attempted network access'))
        self.network.start()

    def tearDown(self):
        self.network.stop(); self.environment.stop(); self.temp.cleanup()

    def image(self):
        from PIL import Image,ImageDraw,ImageFont
        font_path='/System/Library/Fonts/Supplemental/Arial.ttf'
        font=ImageFont.truetype(font_path,36) if Path(font_path).exists() else ImageFont.load_default(size=36)
        image=Image.new('RGB',(1500,500),'white'); draw=ImageDraw.Draw(image)
        for index,line in enumerate(['Gedächtnis und Lernen','Das Arbeitsgedächtnis verarbeitet neue Informationen.','Retrieval practice improves long term retention.']): draw.text((35,35+index*100),line,font=font,fill='black')
        return image

    def assert_recognized(self,document):
        text=document['text'].casefold()
        self.assertIn('arbeitsgedächtnis',text)
        self.assertIn('retrieval',text)
        self.assertTrue(document['blocks'])

    def test_native_ocr_reads_every_supported_image_format(self):
        image=self.image()
        for extension in ['png','jpg','jpeg','tif','tiff','webp']:
            with self.subTest(extension=extension):
                path=self.source/f'lecture.{extension}'; image.save(path)
                self.assert_recognized(d.extract_document(path,self.cache))

    def test_mixed_native_and_scanned_pdf(self):
        from pypdf import PdfReader,PdfWriter
        from pypdf.generic import DictionaryObject,NameObject,DecodedStreamObject
        writer=PdfWriter(); page=writer.add_blank_page(width=600,height=400)
        font=DictionaryObject({NameObject('/Type'):NameObject('/Font'),NameObject('/Subtype'):NameObject('/Type1'),NameObject('/BaseFont'):NameObject('/Helvetica')})
        page[NameObject('/Resources')]=DictionaryObject({NameObject('/Font'):DictionaryObject({NameObject('/F1'):writer._add_object(font)})})
        content=DecodedStreamObject(); content.set_data(b'BT /F1 18 Tf 30 300 Td (Working memory retains information for cognitive tasks.) Tj ET')
        page[NameObject('/Contents')]=writer._add_object(content)
        image_pdf=io.BytesIO(); self.image().save(image_pdf,format='PDF'); image_pdf.seek(0)
        writer.append(PdfReader(image_pdf))
        path=self.source/'mixed.PDF'
        with path.open('wb') as handle: writer.write(handle)
        result=d.extract_document(path,self.cache)
        self.assert_recognized(result)
        self.assertEqual(result['page_count'],2)
        self.assertEqual([b['locator']['index'] for b in result['blocks']],[1,2])
        self.assertIn('working memory retains',result['blocks'][0]['text'].casefold())
        self.assertEqual(result['blocks'][1]['kind'],'ocr')

    def test_word_text_table_and_embedded_image(self):
        from docx import Document
        document=Document(); document.add_heading('Gedächtnisforschung',level=1)
        document.add_paragraph('Psychologische Theorien erklären Lernen und Erinnerung.')
        table=document.add_table(rows=2,cols=2)
        table.cell(0,0).text='Gruppe'; table.cell(0,1).text='Ergebnis'; table.cell(1,0).text='Training'; table.cell(1,1).text='360 ms'
        image=io.BytesIO(); self.image().save(image,format='PNG'); image.seek(0); document.add_picture(image)
        path=self.source/'lecture.docx'; document.save(path)
        result=d.extract_document(path,self.cache)
        self.assert_recognized(result)
        self.assertTrue(any(b['kind']=='table' for b in result['blocks']))
        self.assertTrue(all(b['locator']['kind']=='section' for b in result['blocks']))
        self.assertIsNone(result['page_count'])

    def test_powerpoint_image_only_slide_and_text_slide(self):
        from pptx import Presentation
        from pptx.util import Inches
        presentation=Presentation()
        slide=presentation.slides.add_slide(presentation.slide_layouts[6])
        slide.shapes.add_textbox(Inches(1),Inches(1),Inches(8),Inches(1)).text='Psychologische Konzepte über Gedächtnis und Entwicklung.'
        slide=presentation.slides.add_slide(presentation.slide_layouts[6])
        image=io.BytesIO(); self.image().save(image,format='PNG'); image.seek(0)
        slide.shapes.add_picture(image,Inches(.5),Inches(.5),width=Inches(9))
        path=self.source/'lecture.pptx'; presentation.save(path)
        result=d.extract_document(path,self.cache)
        self.assert_recognized(result)
        self.assertTrue(any(b['kind']=='ocr' and b['locator']=={'kind':'slide','index':2} for b in result['blocks']))
        self.assertTrue(any(b['locator']=={'kind':'slide','index':1} for b in result['blocks']))

if __name__=='__main__': unittest.main()
