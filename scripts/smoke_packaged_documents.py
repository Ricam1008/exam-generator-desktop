#!/usr/bin/env python3
"""Exercise a packaged backend's real document API with synthetic files only.

Use the build Python to create fixtures; the backend runs from an empty cwd with
an isolated HOME, no PYTHONPATH/virtual environment, and offline model settings.
No application source modules are imported. On macOS run with normal Vision
access, because a restricted process can return no OCR observations.
"""
from __future__ import annotations

import argparse
import hashlib
import io
import json
import os
import queue
import subprocess
import sys
import tempfile
import threading
import time
import urllib.error
import urllib.request
from pathlib import Path

OCR_LINES = ['Gedächtnis und Lernen', 'Das Arbeitsgedächtnis verarbeitet neue Informationen.', 'Retrieval practice improves long term retention.']
OCR_WORDS = ['arbeitsgedächtnis', 'retrieval', 'retention']


def fixture_image():
    from PIL import Image, ImageDraw, ImageFont
    fonts = ['/System/Library/Fonts/Supplemental/Arial.ttf', '/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf']
    font = next((ImageFont.truetype(path, 36) for path in fonts if Path(path).is_file()), ImageFont.load_default(size=36))
    image = Image.new('RGB', (1500, 500), 'white')
    draw = ImageDraw.Draw(image)
    for index, line in enumerate(OCR_LINES):
        draw.text((35, 35 + index * 100), line, font=font, fill='black')
    return image


def create_fixtures(root: Path) -> dict[str, dict]:
    from docx import Document
    from pptx import Presentation
    from pptx.util import Inches
    from pypdf import PdfReader, PdfWriter
    from pypdf.generic import DictionaryObject, NameObject, DecodedStreamObject
    root.mkdir()
    cases = {}
    image = fixture_image()
    try:
        for ext in ['png', 'jpg', 'jpeg', 'tif', 'tiff', 'webp']:
            name = f'lecture.{ext}'
            if ext == 'tif':
                image.save(root / name, save_all=True, append_images=[image])
            else:
                image.save(root / name)
            cases[name] = {'words': OCR_WORDS, 'locator_kind': 'page', 'locator_indices': [1, 2] if ext == 'tif' else [1]}
        rotated = image.rotate(90, expand=True)
        rotated.save(root / 'rotated.png')
        rotated.close()
        cases['rotated.png'] = {'words': OCR_WORDS, 'locator_kind': 'page', 'locator_indices': [1]}

        native = PdfWriter()
        page = native.add_blank_page(width=600, height=400)
        font = DictionaryObject({NameObject('/Type'): NameObject('/Font'), NameObject('/Subtype'): NameObject('/Type1'), NameObject('/BaseFont'): NameObject('/Helvetica')})
        page[NameObject('/Resources')] = DictionaryObject({NameObject('/Font'): DictionaryObject({NameObject('/F1'): native._add_object(font)})})
        content = DecodedStreamObject()
        content.set_data(b'BT /F1 18 Tf 30 300 Td (Working memory retains information for cognitive tasks.) Tj ET')
        page[NameObject('/Contents')] = native._add_object(content)
        image_pdf = io.BytesIO()
        image.save(image_pdf, format='PDF')
        image_pdf.seek(0)
        native.append(PdfReader(image_pdf))
        with (root / 'mixed.PDF').open('wb') as handle:
            native.write(handle)
        cases['mixed.PDF'] = {'words': OCR_WORDS + ['working memory retains'], 'locator_kind': 'page', 'locator_indices': [1, 2], 'required_block_kinds': ['text', 'ocr'], 'page_count': 2}

        doc = Document()
        doc.add_heading('Gedächtnisforschung', level=1)
        doc.add_paragraph('Psychologische Theorien erklären Lernen und Erinnerung.')
        table = doc.add_table(rows=2, cols=2)
        table.cell(0, 0).text, table.cell(0, 1).text = 'Gruppe', 'Ergebnis'
        table.cell(1, 0).text, table.cell(1, 1).text = 'Training', '360 ms'
        picture = io.BytesIO()
        image.save(picture, format='PNG')
        picture.seek(0)
        doc.add_picture(picture)
        doc.save(root / 'lecture.docx')
        cases['lecture.docx'] = {'words': OCR_WORDS + ['psychologische theorien', '360 ms'], 'locator_kind': 'section', 'required_block_kinds': ['table', 'ocr'], 'page_count': None}

        presentation = Presentation()
        slide = presentation.slides.add_slide(presentation.slide_layouts[6])
        slide.shapes.add_textbox(Inches(1), Inches(1), Inches(8), Inches(1)).text = 'Psychologische Konzepte über Gedächtnis und Entwicklung.'
        slide = presentation.slides.add_slide(presentation.slide_layouts[6])
        picture.seek(0)
        slide.shapes.add_picture(picture, Inches(.5), Inches(.5), width=Inches(9))
        presentation.save(root / 'lecture.pptx')
        cases['lecture.pptx'] = {'words': OCR_WORDS + ['psychologische konzepte'], 'locator_kind': 'slide', 'locator_indices': [1, 2], 'required_block_kinds': ['text', 'ocr'], 'page_count': None}

        for name in ['lecture.txt', 'lecture.md']:
            (root / name).write_text('Gedächtnisforschung\n\nPsychologische Theorien erklären Lernen und Erinnerung.', encoding='utf-8')
            cases[name] = {'words': ['gedächtnisforschung', 'psychologische theorien'], 'locator_kind': 'paragraph', 'locator_indices': [1, 2], 'page_count': None}
    finally:
        image.close()
    return cases


def file_hash(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def check_document(result: dict, expected: dict) -> None:
    text = result.get('text', '').casefold()
    missing = [word for word in expected['words'] if word not in text]
    if missing:
        raise AssertionError(f'Missing extracted content: {missing}; warnings={result.get("warnings", [])}')
    blocks = result.get('blocks', [])
    if not blocks or any(b.get('locator', {}).get('kind') != expected['locator_kind'] for b in blocks):
        raise AssertionError(f'Incorrect provenance kind; expected {expected["locator_kind"]}')
    indices = sorted({b['locator']['index'] for b in blocks})
    if 'locator_indices' in expected and indices != expected['locator_indices']:
        raise AssertionError(f'Incorrect provenance indices: {indices}')
    kinds = {b.get('kind') for b in blocks}
    if not set(expected.get('required_block_kinds', [])).issubset(kinds):
        raise AssertionError(f'Missing extraction block kind: {expected.get("required_block_kinds")}; got {sorted(kinds)}')
    if 'page_count' in expected and result.get('page_count') != expected['page_count']:
        raise AssertionError(f'Incorrect page count: {result.get("page_count")}')


def api_call(opener, url: str, token: str, path: str, payload: dict | None = None):
    headers = {'Authorization': 'Bearer ' + token}
    body = None
    if payload is not None:
        body = json.dumps(payload).encode()
        headers['Content-Type'] = 'application/json'
    req = urllib.request.Request(url + path, data=body, headers=headers, method='GET' if payload is None else 'POST')
    try:
        with opener.open(req, timeout=180) as response:
            return json.load(response)
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode('utf-8', errors='replace')
        raise RuntimeError(f'{path} HTTP {exc.code}: {detail}') from exc


def run(backend: Path, report: dict) -> None:
    with tempfile.TemporaryDirectory(prefix='exam-packaged-docs-') as temporary:
        root = Path(temporary)
        source = root / 'input'
        cases = create_fixtures(source)
        originals = {name: file_hash(source / name) for name in cases}
        home, cwd = root / 'home', root / 'empty-cwd'
        home.mkdir()
        cwd.mkdir()
        env = os.environ.copy()
        for key in ['PYTHONPATH', 'PYTHONHOME', 'VIRTUAL_ENV', 'CONDA_PREFIX', 'DYLD_LIBRARY_PATH', 'DYLD_FRAMEWORK_PATH']:
            env.pop(key, None)
        env.update({'HOME': str(home), 'PATH': '/usr/bin:/bin:/usr/sbin:/sbin', 'HF_HOME': str(home / '.cache' / 'huggingface'), 'XDG_CACHE_HOME': str(home / '.cache'), 'TORCH_HOME': str(home / '.cache' / 'torch'), 'HF_HUB_OFFLINE': '1', 'TRANSFORMERS_OFFLINE': '1', 'PYINSTALLER_RESET_ENVIRONMENT': '1', 'HTTP_PROXY': 'http://127.0.0.1:9', 'HTTPS_PROXY': 'http://127.0.0.1:9', 'NO_PROXY': '127.0.0.1,localhost'})
        stdout_lines = queue.Queue()
        with (root / 'backend.stderr').open('w+') as stderr:
            process = subprocess.Popen([str(backend), 'serve', '--port', '0'], cwd=cwd, env=env, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=stderr, text=True)
            def collect_stdout():
                for line in process.stdout:
                    stdout_lines.put(line)
            threading.Thread(target=collect_stdout, daemon=True).start()
            ready = None
            try:
                deadline = time.monotonic() + 90
                while time.monotonic() < deadline and process.poll() is None:
                    try:
                        line = stdout_lines.get(timeout=.2)
                    except queue.Empty:
                        continue
                    try:
                        value = json.loads(line)
                        if value.get('event') == 'ready':
                            ready = value
                            break
                    except (ValueError, AttributeError):
                        continue
                if not ready:
                    raise RuntimeError(f'Packaged backend did not become ready (exit={process.poll()}).')
                opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
                url, token = ready['url'], ready['token']
                tools = api_call(opener, url, token, '/api/tools')
                report['tools'] = tools
                checks = report['checks']
                missing = [key for key in ['pdf_ready', 'office_ready', 'ocr_ready', 'pdf_ocr_ready'] if not tools.get(key)]
                checks.append({'name': 'bundled_document_tools', 'ok': not missing, 'missing': missing})
                scan = api_call(opener, url, token, '/api/scan', {'input_path': str(source)})
                sources = {item['name']: item for item in scan['sources']}
                checks.append({'name': 'discovery_all_formats', 'ok': set(sources) == set(cases), 'expected_count': len(cases), 'actual_count': len(sources)})
                for name, expected in cases.items():
                    started = time.monotonic()
                    try:
                        item = sources[name]
                        result = api_call(opener, url, token, '/api/extract', {'input_path': str(source), 'source_id': item['id']})
                        check_document(result, expected)
                        checks.append({'name': name, 'ok': True, 'seconds': round(time.monotonic() - started, 3), 'method': result.get('method'), 'block_count': len(result['blocks']), 'warnings': result.get('warnings', [])})
                    except Exception as exc:
                        checks.append({'name': name, 'ok': False, 'seconds': round(time.monotonic() - started, 3), 'error': str(exc)})
                    print(f'{"PASS" if checks[-1]["ok"] else "FAIL"} {name}', flush=True)
                changed_before_edit = [name for name, fingerprint in originals.items() if file_hash(source / name) != fingerprint]
                checks.append({'name': 'all_originals_preserved_before_controlled_edit', 'ok': not changed_before_edit, 'changed': changed_before_edit})
                try:
                    source_id = sources['lecture.txt']['id']
                    cached = api_call(opener, url, token, '/api/extract', {'input_path': str(source), 'source_id': source_id})
                    if not cached.get('cache_hit'):
                        raise AssertionError('Second text extraction did not hit the cache.')
                    (source / 'lecture.txt').write_text('Aktualisierte Inhalte über Entwicklungspsychologie und Lernprozesse.', encoding='utf-8')
                    changed = api_call(opener, url, token, '/api/extract', {'input_path': str(source), 'source_id': source_id})
                    if changed.get('cache_hit') or 'aktualisierte' not in changed['text'].casefold() or cached['fingerprint'] == changed['fingerprint']:
                        raise AssertionError('Changed source incorrectly reused stale extraction.')
                    checks.append({'name': 'cache_and_source_change', 'ok': True})
                except Exception as exc:
                    checks.append({'name': 'cache_and_source_change', 'ok': False, 'error': str(exc)})
                unchanged = [name for name, fingerprint in originals.items() if name != 'lecture.txt' and file_hash(source / name) != fingerprint]
                checks.append({'name': 'originals_preserved', 'ok': not unchanged, 'changed': unchanged})
                model_files = list((home / '.cache' / 'huggingface').rglob('*'))
                checks.append({'name': 'no_model_weights_downloaded', 'ok': not any(p.is_file() for p in model_files)})
            finally:
                if process.poll() is None:
                    try:
                        process.stdin.write('shutdown\n')
                        process.stdin.flush()
                        process.wait(timeout=15)
                    except (BrokenPipeError, subprocess.TimeoutExpired):
                        process.kill()
                        process.wait(timeout=5)
                if process.stdin:
                    process.stdin.close()
                if process.stdout:
                    process.stdout.close()
                report['backend_exit_code'] = process.returncode
                stderr.seek(0)
                report['backend_stderr_tail'] = stderr.read()[-6000:]


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('backend', type=Path, help='Packaged backend executable, not a Python module')
    parser.add_argument('--report', type=Path, default=Path('/tmp/exam-packaged-document-smoke.json'))
    args = parser.parse_args()
    backend = args.backend.expanduser().resolve()
    report = {'backend': str(backend), 'backend_sha256': file_hash(backend), 'started_at': time.strftime('%Y-%m-%dT%H:%M:%S%z'), 'isolated_home': True, 'offline_models': True, 'checks': []}
    try:
        run(backend, report)
    except Exception as exc:
        report['checks'].append({'name': 'smoke_execution', 'ok': False, 'error': str(exc)})
    report['ok'] = bool(report['checks']) and all(check['ok'] for check in report['checks'])
    args.report.parent.mkdir(parents=True, exist_ok=True)
    args.report.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding='utf-8')
    print(f'{"PASS" if report["ok"] else "FAIL"}: {sum(c["ok"] for c in report["checks"])}/{len(report["checks"])} checks. Report: {args.report}')
    return 0 if report['ok'] else 1


if __name__ == '__main__':
    raise SystemExit(main())
