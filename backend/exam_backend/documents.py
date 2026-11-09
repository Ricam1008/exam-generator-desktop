"""Read-only study document discovery and local extraction.

Optional OCR uses macOS Vision. Office conversion uses Docling's model-free
SimplePipeline. Neither path downloads a model or contacts a remote service.
"""
from __future__ import annotations

import hashlib
import importlib.metadata
import importlib.util
import json
import os
import posixpath
import re
import statistics
import sys
import tempfile
import unicodedata
import zipfile
from pathlib import Path
from typing import Any

SUPPORTED_EXTENSIONS = frozenset({'.pdf', '.docx', '.pptx', '.txt', '.md', '.markdown', '.png', '.jpg', '.jpeg', '.tif', '.tiff', '.webp'})
IMAGE_EXTENSIONS = frozenset({'.png', '.jpg', '.jpeg', '.tif', '.tiff', '.webp'})
EXTRACTOR_VERSION = 'documents-2'
MAX_SOURCE_BYTES = 512 * 1024 * 1024
MAX_UNPACKED_BYTES = 1024 * 1024 * 1024
MAX_IMAGE_PIXELS = 80_000_000


class DocumentError(ValueError):
    """A source cannot be used, with an actionable user-facing explanation."""


class ExtractionCanceled(InterruptedError):
    pass


def check_cancel(cancel_event: Any = None) -> None:
    if cancel_event is not None and cancel_event.is_set():
        raise ExtractionCanceled('Verarbeitung abgebrochen.')


def file_fingerprint(path: Path, cancel_event: Any = None) -> str:
    digest = hashlib.sha256()
    with path.open('rb') as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b''):
            check_cancel(cancel_event)
            digest.update(chunk)
    return digest.hexdigest()


def discover_sources(input_path: str | Path, paths: list[str] | None = None) -> list[dict[str, Any]]:
    root = Path(input_path).expanduser().resolve()
    if not root.exists():
        raise DocumentError('Der Materialordner existiert nicht. Bitte erneut auswählen.')
    if root.is_file():
        candidates, base = [root], root.parent
    elif paths is not None:
        candidates, base = [Path(item).expanduser().resolve() for item in paths], root
    else:
        # Do not follow directory symlinks or import generated workspaces.
        candidates, base = [], root
        for current, dirs, files in os.walk(root, followlinks=False):
            dirs[:] = sorted(name for name in dirs if name.lower() != 'exams' and not name.startswith('.') and not (Path(current) / name).is_symlink())
            candidates.extend(Path(current) / name for name in files)
    sources = []
    seen = set()
    for candidate in sorted(candidates, key=lambda path: str(path).casefold()):
        if candidate.suffix.lower() not in SUPPORTED_EXTENSIONS:
            continue
        resolved = candidate.resolve()
        if not resolved.is_relative_to(base) or candidate.is_symlink() or not resolved.is_file():
            if paths is not None:
                raise DocumentError('Ausgewählte Dateien müssen innerhalb des Materialordners liegen und dürfen keine Verknüpfungen sein.')
            continue
        relative = resolved.relative_to(base)
        if any(part.lower() == 'exams' or part.startswith('.') for part in relative.parts):
            continue
        if resolved in seen:
            continue
        seen.add(resolved)
        if resolved.stat().st_size > MAX_SOURCE_BYTES:
            raise DocumentError(f'{resolved.name}: Die Datei ist größer als 512 MB. Bitte in kleinere Dateien aufteilen.')
        identity = unicodedata.normalize('NFC', relative.as_posix())
        sources.append({'id': hashlib.sha256(identity.encode()).hexdigest()[:20], 'path': str(resolved), 'relative_path': relative.as_posix(), 'name': resolved.name, 'course': relative.parent.as_posix() if relative.parent != Path('.') else base.name, 'format': resolved.suffix.lower().lstrip('.'), 'fingerprint': file_fingerprint(resolved), 'size_bytes': resolved.stat().st_size})
    return sources


def _installed(module: str) -> bool:
    try:
        return importlib.util.find_spec(module) is not None
    except (ValueError, ModuleNotFoundError):
        return False


def _version(package: str) -> str:
    try:
        return importlib.metadata.version('docling-slim' if package == 'docling' else package)
    except importlib.metadata.PackageNotFoundError:
        return 'missing'


def tool_status() -> dict[str, Any]:
    office, ocr, renderer = _installed('docling'), sys.platform == 'darwin' and _installed('ocrmac'), _installed('pypdfium2')
    return {'formats': sorted(ext.lstrip('.') for ext in SUPPORTED_EXTENSIONS), 'office_ready': office, 'ocr_ready': ocr, 'pdf_ocr_ready': ocr and renderer, 'pdf_ready': _installed('pdfplumber') or _installed('pypdf'), 'enhanced_ready': False, 'enhanced_reason': 'Granite-Docling bleibt bis zum bestandenen lokalen Qualitätsvergleich deaktiviert.', 'downloads_required': False, 'detail': 'Dokumentwerkzeuge werden mit der App installiert. OCR verarbeitet Bilder lokal; es werden keine Seiten hochgeladen.', 'versions': {name: _version(name) for name in ('docling', 'ocrmac', 'pypdfium2', 'pdfplumber', 'pypdf')}}


def _usable_text(text: str) -> bool:
    words = re.findall(r'[^\W\d_]{2,}', text, flags=re.UNICODE)
    return len(words) >= 3 and sum(map(len, words)) >= 12


def _block(text: str, kind: str, locator_kind: str, index: int, **extra: Any) -> dict[str, Any]:
    return {'text': text.strip(), 'kind': kind, 'locator': {'kind': locator_kind, 'index': index}, **extra}


def _image_text(image: Any, cancel_event: Any = None) -> str:
    check_cancel(cancel_event)
    if sys.platform != 'darwin' or not _installed('ocrmac'):
        raise DocumentError('Lokale Texterkennung ist nicht verfügbar. Bitte die Mac-App mit Dokumentwerkzeugen installieren.')
    from ocrmac import ocrmac
    try:
        annotations = ocrmac.OCR(image, recognition_level='accurate', language_preference=['de-DE', 'en-US']).recognize()
    except Exception as exc:
        raise DocumentError('Die lokale Texterkennung ist fehlgeschlagen. Bitte das Bild prüfen oder eine lesbare Kopie verwenden.') from exc
    check_cancel(cancel_event)
    return _ordered_ocr_lines(annotations)


def _ordered_ocr_lines(annotations: list) -> str:
    if not annotations:
        return ''
    # Vision already orders rotated text along its detected writing direction.
    if sum(row[2][3] > row[2][2] for row in annotations) > len(annotations) / 2:
        return '\n'.join(str(row[0]) for row in annotations if row[0]).strip()
    tolerance = statistics.median(float(row[2][3]) for row in annotations) * 0.6
    rows: list[list] = []
    for item in sorted(annotations, key=lambda row: -(float(row[2][1]) + float(row[2][3]) / 2)):
        center = float(item[2][1]) + float(item[2][3]) / 2
        if not rows or abs(center - (float(rows[-1][0][2][1]) + float(rows[-1][0][2][3]) / 2)) > tolerance:
            rows.append([])
        rows[-1].append(item)
    return '\n'.join(' '.join(str(item[0]) for item in sorted(row, key=lambda item: float(item[2][0])) if item[0]) for row in rows).strip()



def _open_image(path_or_bytes: Any) -> Any:
    try:
        from PIL import Image, ImageOps
    except ImportError as exc:
        raise DocumentError('Die Bildwerkzeuge fehlen. Bitte die App mit Dokumentwerkzeugen installieren.') from exc
    image = Image.open(path_or_bytes)
    if image.width * image.height > MAX_IMAGE_PIXELS:
        image.close()
        raise DocumentError('Das Bild ist zu groß. Bitte auf höchstens 80 Megapixel verkleinern.')
    return ImageOps.exif_transpose(image).convert('RGB')


def _extract_image(path: Path, cancel_event: Any) -> tuple[list[dict], list[str], str]:
    try:
        from PIL import Image
        with Image.open(path) as source:
            blocks = []
            for index in range(getattr(source, 'n_frames', 1)):
                check_cancel(cancel_event)
                source.seek(index)
                if source.width * source.height > MAX_IMAGE_PIXELS:
                    raise DocumentError('Das Bild ist zu groß. Bitte auf höchstens 80 Megapixel verkleinern.')
                image = source.copy()
                try:
                    from PIL import ImageOps
                    text = _image_text(ImageOps.exif_transpose(image).convert('RGB'), cancel_event)
                finally:
                    image.close()
                if _usable_text(text):
                    blocks.append(_block(text, 'ocr', 'page', index + 1))
        return blocks, [], 'macOS Vision'
    except ImportError as exc:
        raise DocumentError('Die Bildwerkzeuge fehlen. Bitte die App mit Dokumentwerkzeugen installieren.') from exc
    except OSError as exc:
        raise DocumentError('Die Bilddatei ist beschädigt oder nicht lesbar. Bitte erneut als PNG/JPEG speichern.') from exc


def _render_pdf_page(path: Path, index: int) -> Any:
    if not _installed('pypdfium2'):
        raise DocumentError('Die PDF-Texterkennung fehlt. Bitte die App mit Dokumentwerkzeugen installieren.')
    import pypdfium2
    with pypdfium2.PdfDocument(str(path)) as pdf:
        page = pdf[index]
        bitmap = None
        try:
            width, height = page.get_size()
            scale = min(2.0, (MAX_IMAGE_PIXELS / max(width * height, 1)) ** 0.5)
            bitmap = page.render(scale=scale)
            return bitmap.to_pil().copy()
        finally:
            if bitmap is not None:
                bitmap.close()
            page.close()


def _pdf_native_pages(path: Path, cancel_event: Any = None) -> list[tuple[str, bool]]:
    if _installed('pdfplumber'):
        import pdfplumber
        pages = []
        with pdfplumber.open(path) as pdf:
            for index, page in enumerate(pdf.pages, 1):
                check_cancel(cancel_event)
                parts = [page.extract_text() or '']
                for table_index, table in enumerate(page.extract_tables() or [], 1):
                    rows = [' | '.join(re.sub(r'\s+', ' ', str(cell or '')).strip() for cell in row) for row in table if row]
                    if rows:
                        parts.append(f'[TABLE page={index} index={table_index}]\n' + '\n'.join(rows))
                pages.append(('\n\n'.join(parts), bool(page.images)))
        return pages
    if _installed('pypdf'):
        from pypdf import PdfReader
        reader = PdfReader(path)
        if reader.is_encrypted:
            raise DocumentError('Dieses PDF ist verschlüsselt. Bitte eine entsperrte Kopie verwenden.')
        pages = []
        for page in reader.pages:
            check_cancel(cancel_event)
            resources = page.get('/Resources', {})
            if hasattr(resources, 'get_object'):
                resources = resources.get_object()
            pages.append((page.extract_text() or '', bool(resources.get('/XObject'))))
        return pages
    raise DocumentError('Die PDF-Werkzeuge fehlen. Bitte die App mit Dokumentwerkzeugen installieren.')


def _extract_pdf(path: Path, cancel_event: Any, mode: str) -> tuple[list[dict], list[str], str]:
    blocks, warnings, used_ocr = [], [], False
    try:
        pages = _pdf_native_pages(path, cancel_event)
    except (DocumentError, ExtractionCanceled):
        raise
    except Exception as exc:
        raise DocumentError(f'{path.name}: PDF nicht lesbar oder verschlüsselt. Bitte eine intakte, entsperrte Kopie verwenden.') from exc
    for index, (text, has_image) in enumerate(pages, 1):
        page_ocr = False
        check_cancel(cancel_event)
        meaningful = re.sub(r'\[TABLE[^\]]*\]', '', text)
        needs_ocr = mode == 'ocr' or not _usable_text(meaningful) or (has_image and len(meaningful.split()) < 15)
        if needs_ocr:
            try:
                image = _render_pdf_page(path, index - 1)
                try:
                    recognized = _image_text(image, cancel_event)
                finally:
                    image.close()
                if _usable_text(recognized):
                    if len(recognized.strip()) > len(meaningful.strip()) or not _usable_text(meaningful):
                        text = recognized
                        used_ocr = True
                        page_ocr = True
                else:
                    warnings.append(f'Seite {index}: Auch die Texterkennung hat keinen nutzbaren Inhalt gefunden.')
            except DocumentError as exc:
                warnings.append(f'Seite {index}: {exc}')
        if _usable_text(re.sub(r'\[TABLE[^\]]*\]', '', text)):
            blocks.append(_block(text, 'ocr' if page_ocr else 'text', 'page', index, total_pages=len(pages)))
        else:
            warnings.append(f'Seite {index}: Kein nutzbarer Text. Diese Seite fehlt in der Prüfung.')
    return blocks, warnings, 'PDF native + macOS Vision' if used_ocr else 'PDF native'


def _check_office_archive(path: Path) -> None:
    try:
        with zipfile.ZipFile(path) as archive:
            if sum(item.file_size for item in archive.infolist()) > MAX_UNPACKED_BYTES:
                raise DocumentError('Das entpackte Dokument ist zu groß. Bitte in kleinere Dokumente aufteilen.')
            for name in archive.namelist():
                if name.endswith('.rels'):
                    data = archive.read(name)
                    # Linked images are not embedded study material; do not allow remote retrieval.
                    if re.search(rb'TargetMode\s*=\s*[\'\"]External[\'\"]', data) and b'/image' in data:
                        from xml.etree import ElementTree
                        for relation in ElementTree.fromstring(data):
                            if relation.get('TargetMode') == 'External' and relation.get('Type', '').endswith('/image'):
                                raise DocumentError('Das Dokument enthält extern verknüpfte Bilder. Bitte die Bilder einbetten und erneut speichern.')
    except zipfile.BadZipFile as exc:
        raise DocumentError('Das Office-Dokument ist beschädigt oder verschlüsselt. Bitte erneut als DOCX/PPTX speichern.') from exc


def profile_document(path: str | Path, cancel_event: Any = None) -> dict[str, Any]:
    """Count document structure without rendering images or contacting a provider."""
    path = Path(path)
    suffix = path.suffix.lower()
    result = dict.fromkeys(('pages', 'slides', 'paragraphs', 'sections', 'tables', 'image_occurrences', 'unique_images'))
    result.update(format=suffix.lstrip('.'), size_bytes=None, warnings=[])
    check_cancel(cancel_event)
    try:
        result['size_bytes'] = path.stat().st_size
        if result['size_bytes'] > MAX_SOURCE_BYTES:
            raise DocumentError('Datei zu groß für die Materialanalyse.')
        if suffix in {'.docx', '.pptx'}:
            _check_office_archive(path)
            from xml.etree import ElementTree as ET
            with zipfile.ZipFile(path) as archive:
                xml_bytes = 0
                def xml(name):
                    nonlocal xml_bytes
                    check_cancel(cancel_event)
                    size = archive.getinfo(name).file_size
                    xml_bytes += size
                    if size > 16 * 1024 * 1024 or xml_bytes > 64 * 1024 * 1024:
                        raise DocumentError('XML-Metadaten sind zu groß für die Materialanalyse.')
                    data = archive.read(name)
                    if b'<!DOCTYPE' in data or b'<!ENTITY' in data or b'\x00' in data:
                        raise DocumentError('XML-Entitäten werden in der Materialanalyse nicht verarbeitet.')
                    return ET.fromstring(data)
                if suffix == '.pptx':
                    presentation = xml('ppt/presentation.xml')
                    relations = {r.get('Id'): r.get('Target') for r in xml('ppt/_rels/presentation.xml.rels')}
                    ids = presentation.findall('.//{http://schemas.openxmlformats.org/presentationml/2006/main}sldId')
                    parts = [posixpath.normpath(posixpath.join('ppt', relations[item.get('{http://schemas.openxmlformats.org/officeDocument/2006/relationships}id')])).lstrip('/') for item in ids]
                    result['slides'] = len(parts)
                    paragraph_tag, table_tag = 'a:p', 'a:tbl'
                else:
                    parts = ['word/document.xml'] + sorted(name for name in archive.namelist() if re.fullmatch(r'word/(header|footer|footnotes|endnotes)\d*\.xml', name))
                    paragraph_tag, table_tag = 'w:p', 'w:tbl'
                ns = {'w': 'http://schemas.openxmlformats.org/wordprocessingml/2006/main', 'a': 'http://schemas.openxmlformats.org/drawingml/2006/main'}
                paragraphs = tables = pictures = sections = 0
                images = set()
                for part in parts:
                    tree = xml(part)
                    paragraphs += len(tree.findall('.//' + paragraph_tag, ns))
                    tables += len(tree.findall('.//' + table_tag, ns))
                    sections += len(tree.findall('.//w:sectPr', ns))
                    relpath = posixpath.join(posixpath.dirname(part), '_rels', posixpath.basename(part) + '.rels')
                    relations = {r.get('Id'): r.get('Target') for r in xml(relpath) if r.get('Type', '').endswith('/image')} if relpath in archive.namelist() else {}
                    for element in tree.iter():
                        check_cancel(cancel_event)
                        rid = element.get('{http://schemas.openxmlformats.org/officeDocument/2006/relationships}embed') if element.tag.endswith('}blip') else element.get('{http://schemas.openxmlformats.org/officeDocument/2006/relationships}id') if element.tag.endswith('}imagedata') else None
                        if rid:
                            pictures += 1
                            if rid not in relations:
                                raise DocumentError('Eine Bildverknüpfung konnte nicht zugeordnet werden.')
                            target = posixpath.normpath(posixpath.join(posixpath.dirname(part), relations[rid])).lstrip('/')
                            if target not in archive.namelist():
                                raise DocumentError('Ein eingebettetes Bild fehlt im Dokument.')
                            images.add(target)
                result.update(paragraphs=paragraphs, tables=tables, image_occurrences=pictures, unique_images=len(images))
                if suffix == '.docx':
                    result['sections'] = sections
                    result['warnings'].append('Word-Seitenzahlen sind ohne gerenderte Datei nicht bekannt. Bildvorkommen zählen Platzierungen im Dokument, nicht Wiederholungen eines Kopfzeilenbildes auf gedruckten Seiten.')
        elif suffix == '.pdf':
            result['warnings'].append('Bildzahlen erfassen Rasterbilder; Vektordiagramme werden nicht zuverlässig gezählt.')
            if _installed('pdfplumber'):
                import pdfplumber
                with pdfplumber.open(path) as pdf:
                    result['pages'] = len(pdf.pages)
                    occurrences, images, unknown_identity = 0, set(), False
                    for page in pdf.pages:
                        check_cancel(cancel_event)
                        for image in page.images:
                            check_cancel(cancel_event)
                            occurrences += 1
                            stream = image.get('stream')
                            identity = getattr(stream, 'objid', None)
                            if identity is not None:
                                images.add(identity)
                            else:
                                unknown_identity = True
                    result['image_occurrences'] = occurrences
                    result['unique_images'] = None if unknown_identity else len(images)
            else:
                from pypdf import PdfReader
                reader = PdfReader(path)
                if reader.is_encrypted:
                    raise DocumentError('PDF ist verschlüsselt.')
                result['pages'] = len(reader.pages)
                result['warnings'].append('Rasterbildvorkommen konnten ohne PDF-Strukturanalyse nicht ermittelt werden.')
        elif suffix in IMAGE_EXTENSIONS:
            from PIL import Image
            with Image.open(path) as image:
                result.update(width=image.width, height=image.height, image_occurrences=1, unique_images=1, frames=getattr(image, 'n_frames', 1))
        elif suffix in {'.txt', '.md', '.markdown'}:
            text = path.read_text(encoding='utf-8-sig')
            result.update(paragraphs=sum(bool(p.strip()) for p in re.split(r'\n\s*\n', text)), image_occurrences=0, unique_images=0)
        else:
            raise DocumentError('Dateiformat wird in der Materialanalyse nicht unterstützt.')
        check_cancel(cancel_event)
    except ExtractionCanceled:
        raise
    except Exception as exc:
        result['warnings'].append(f'Materialbeschreibung unvollständig: {type(exc).__name__}.')
    return result


def _docling_office(path: Path) -> Any:
    if not _installed('docling'):
        raise DocumentError('Die Word-/PowerPoint-Werkzeuge fehlen. Bitte die App mit Dokumentwerkzeugen installieren.')
    from docling.datamodel.base_models import InputFormat
    from docling.document_converter import DocumentConverter
    source_format = InputFormat.DOCX if path.suffix.lower() == '.docx' else InputFormat.PPTX
    converter = DocumentConverter(allowed_formats=[source_format])
    result = converter.convert(str(path), raises_on_error=True, max_file_size=MAX_SOURCE_BYTES)
    return result.document


def _extract_office(path: Path, cancel_event: Any) -> tuple[list[dict], list[str], str]:
    _check_office_archive(path)
    check_cancel(cancel_event)
    try:
        document = _docling_office(path)
    except DocumentError:
        raise
    except Exception as exc:
        raise DocumentError(f'{path.name}: Das Dokument konnte nicht gelesen werden. Bitte erneut als DOCX/PPTX speichern.') from exc
    check_cancel(cancel_event)
    blocks, warnings = [], []
    is_slides = path.suffix.lower() == '.pptx'
    for ordinal, (item, _level) in enumerate(document.iterate_items(), 1):
        check_cancel(cancel_event)
        provenance = getattr(item, 'prov', []) or []
        page_no = getattr(provenance[0], 'page_no', None) if provenance else None
        locator_kind = 'slide' if is_slides and page_no else 'section'
        locator_index = int(page_no) if locator_kind == 'slide' else ordinal
        label = str(getattr(item, 'label', 'text')).lower()
        if 'picture' in label:
            try:
                image = item.get_image(document)
                if image is None:
                    warnings.append(f'Bild in {"Folie" if is_slides else "Abschnitt"} {locator_index}: Das eingebettete Bild konnte nicht gelesen werden.')
                    continue
                if image.width * image.height > MAX_IMAGE_PIXELS:
                    raise DocumentError('Das eingebettete Bild ist zu groß (mehr als 80 Megapixel).')
                text = _image_text(image, cancel_event)
                if _usable_text(text):
                    blocks.append(_block(text, 'ocr', locator_kind, locator_index))
                else:
                    warnings.append(f'Bild in {"Folie" if is_slides else "Abschnitt"} {locator_index}: Kein lesbarer Text; Diagramminhalte werden nicht erfunden.')
            except DocumentError as exc:
                warnings.append(f'Bild in Abschnitt/Folie {locator_index}: {exc}')
            continue
        if 'table' in label and hasattr(item, 'export_to_markdown'):
            text, kind = item.export_to_markdown(doc=document), 'table'
        else:
            text, kind = str(getattr(item, 'text', '') or ''), 'text'
        if text.strip():
            blocks.append(_block(text, kind, locator_kind, locator_index))
    return blocks, warnings, 'Docling + macOS Vision' if any(b['kind'] == 'ocr' for b in blocks) else 'Docling'


def _extract_text(path: Path, cancel_event: Any) -> tuple[list[dict], list[str], str]:
    raw = path.read_bytes()
    try:
        text = raw.decode('utf-16') if raw.startswith((b'\xff\xfe', b'\xfe\xff')) else raw.decode('utf-8-sig')
    except UnicodeDecodeError as exc:
        raise DocumentError('Die Textdatei ist nicht UTF-8/UTF-16. Bitte mit UTF-8-Kodierung speichern.') from exc
    if '\x00' in text:
        raise DocumentError('Die Datei enthält binäre Daten und kann nicht als Lerntext verwendet werden.')
    blocks = []
    for index, paragraph in enumerate(re.split(r'\n\s*\n', text), 1):
        check_cancel(cancel_event)
        if paragraph.strip():
            blocks.append(_block(paragraph, 'text', 'paragraph', index))
    return blocks, [], 'Text'


def _serialized_text(blocks: list[dict[str, Any]]) -> str:
    labels = {'page': 'PAGE', 'slide': 'SLIDE', 'paragraph': 'PARAGRAPH', 'section': 'SECTION'}
    parts = []
    for block in blocks:
        locator = block['locator']
        kind, index = locator['kind'], locator['index']
        prefix = f'--- Page {index} ---\n' if kind == 'page' else ''
        parts.append(f"{prefix}[{labels[kind]} {index} TEXT]\n{block['text']}")
    return '\n\n'.join(parts)


def extract_document(path: str | Path, cache_root: str | Path, cancel_event: Any = None, mode: str = 'auto') -> dict[str, Any]:
    source = Path(path).expanduser().resolve()
    if source.suffix.lower() not in SUPPORTED_EXTENSIONS:
        raise DocumentError('Dieses Dateiformat wird nicht unterstützt.')
    if not source.is_file() or source.stat().st_size == 0:
        raise DocumentError('Die Quelldatei fehlt oder ist leer.')
    if source.stat().st_size > MAX_SOURCE_BYTES:
        raise DocumentError('Die Datei ist größer als 512 MB. Bitte aufteilen.')
    if mode not in {'auto', 'ocr'}:
        raise DocumentError('Erweiterte OCR ist erst nach einem bestandenen lokalen Qualitätsvergleich verfügbar.')
    cache = Path(cache_root).expanduser().resolve()
    if cache == source or cache.is_relative_to(source.parent) and cache == source.parent:
        raise DocumentError('Der Extraktionscache muss außerhalb der Quelldateien liegen.')
    check_cancel(cancel_event)
    fingerprint = file_fingerprint(source, cancel_event)
    key = {'version': EXTRACTOR_VERSION, 'fingerprint': fingerprint, 'format': source.suffix.lower(), 'mode': mode, 'ocr_languages': ['de-DE', 'en-US'], 'versions': tool_status()['versions']}
    cache_path = cache / (hashlib.sha256(json.dumps(key, sort_keys=True).encode()).hexdigest() + '.json')
    if cache_path.is_file():
        try:
            cached = json.loads(cache_path.read_text(encoding='utf-8'))
            if cached.get('cache_key') == key and isinstance(cached.get('blocks'), list) and cached.get('text'):
                check_cancel(cancel_event)
                return {**cached, 'id': hashlib.sha256(str(source).encode()).hexdigest()[:20], 'path': str(source), 'name': source.name, 'cache_hit': True}
        except ExtractionCanceled:
            raise
        except (OSError, ValueError, TypeError):
            pass
    suffix = source.suffix.lower()
    if suffix == '.pdf':
        blocks, warnings, method = _extract_pdf(source, cancel_event, mode)
    elif suffix in {'.docx', '.pptx'}:
        blocks, warnings, method = _extract_office(source, cancel_event)
    elif suffix in IMAGE_EXTENSIONS:
        blocks, warnings, method = _extract_image(source, cancel_event)
    else:
        blocks, warnings, method = _extract_text(source, cancel_event)
    check_cancel(cancel_event)
    if not blocks or not any(_usable_text(block['text']) for block in blocks):
        reason = ' '.join(warnings[:3])
        raise DocumentError('Kein nutzbarer Lerntext gefunden. Bitte Texterkennung prüfen oder eine lesbare Datei verwenden.' + (f' {reason}' if reason else ''))
    # Do not cache a partially unreadable scan: installing OCR must make the next run retry it.
    metadata = profile_document(source, cancel_event)
    if suffix == '.pdf':
        metadata['tables'] = sum(len(re.findall(r'\[TABLE page=\d+ index=\d+\]', b['text'])) for b in blocks) if _installed('pdfplumber') and not any(b['kind'] == 'ocr' for b in blocks) else None
    if file_fingerprint(source, cancel_event) != fingerprint:
        raise DocumentError('Die Quelldatei wurde während der Verarbeitung geändert. Bitte erneut starten.')
    payload = {'id': hashlib.sha256(str(source).encode()).hexdigest()[:20], 'path': str(source), 'name': source.name, 'format': suffix.lstrip('.'), 'fingerprint': fingerprint, 'text': _serialized_text(blocks), 'blocks': blocks, 'warnings': warnings, 'method': method, 'page_count': metadata['pages'] if metadata['pages'] is not None else max((b.get('total_pages', b['locator']['index']) for b in blocks if b['locator']['kind'] == 'page'), default=None), 'metadata': metadata, 'cache_key': key, 'cache_hit': False}
    if not warnings:
        cache.mkdir(parents=True, exist_ok=True)
        temp_name = None
        try:
            with tempfile.NamedTemporaryFile(mode='w', encoding='utf-8', dir=cache, prefix='.extract-', suffix='.tmp', delete=False) as handle:
                temp_name = handle.name
                json.dump(payload, handle, ensure_ascii=False)
                handle.flush()
                os.fsync(handle.fileno())
            check_cancel(cancel_event)
            os.replace(temp_name, cache_path)
        finally:
            if temp_name:
                Path(temp_name).unlink(missing_ok=True)
    return payload
