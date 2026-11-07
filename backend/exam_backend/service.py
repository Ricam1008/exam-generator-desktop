"""Authenticated loopback control API and scoped exam previews."""
from __future__ import annotations

import hashlib
import json
import mimetypes
import os
import re
import secrets
import threading
import time
from http.cookies import SimpleCookie
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, unquote, urlparse

from . import cli, runtime

ORIGINS = {'tauri://localhost', 'http://tauri.localhost', 'https://tauri.localhost', 'http://127.0.0.1:5173', 'http://localhost:5173', 'http://127.0.0.1:5174'}
ARTIFACT = re.compile(r'(?:exam_index\.html|[^/]+/exams/index\.html|[^/]+/exams/[^/]+/(?:index\.html|exam\.json|source\.txt))\Z')


class Handler(BaseHTTPRequestHandler):
    server_version = 'ExamGenerator/0.2'

    def log_message(self, format, *args):
        # Launch URLs contain short-lived capabilities; do not write request URLs.
        return

    def end_headers(self):
        origin = self.headers.get('Origin')
        if origin in ORIGINS:
            self.send_header('Access-Control-Allow-Origin', origin)
            self.send_header('Vary', 'Origin')
        self.send_header('X-Content-Type-Options', 'nosniff')
        self.send_header('Referrer-Policy', 'no-referrer')
        self.send_header('Cache-Control', 'no-store')
        super().end_headers()

    def boundary(self):
        host = self.headers.get('Host', '')
        expected = {f'127.0.0.1:{cli.STATE.port}', f'localhost:{cli.STATE.port}'}
        if host not in expected:
            cli.json_response(self, 403, {'error': 'Unzulässiger Host.'})
            return False
        origin = self.headers.get('Origin')
        local = {f'http://127.0.0.1:{cli.STATE.port}', f'http://localhost:{cli.STATE.port}'}
        if origin and origin not in ORIGINS | local:
            cli.json_response(self, 403, {'error': 'Unzulässiger Ursprung.'})
            return False
        if self.headers.get('Sec-Fetch-Site') == 'cross-site' and origin not in ORIGINS:
            cli.json_response(self, 403, {'error': 'Seitenübergreifender Zugriff abgelehnt.'})
            return False
        return True

    def authorized(self):
        if not self.boundary():
            return False
        token = self.headers.get('Authorization', '').removeprefix('Bearer ')
        if not token or not secrets.compare_digest(token, cli.STATE.token):
            cli.json_response(self, 401, {'error': 'Diese Sitzung ist nicht autorisiert. App neu öffnen.'})
            return False
        return True

    def do_OPTIONS(self):
        if not self.boundary():
            return
        self.send_response(204)
        self.send_header('Access-Control-Allow-Headers', 'Authorization, Content-Type')
        self.send_header('Access-Control-Allow-Methods', 'GET, POST, OPTIONS')
        self.end_headers()

    def do_GET(self):
        path = urlparse(self.path).path
        if path.startswith('/launch/'):
            return self.launch(path.split('/')[-1])
        if path.startswith('/preview/'):
            return self.preview('GET')
        if not self.authorized():
            return
        try:
            from . import providers, documents, oauth
            if path == '/api/health':
                data = {'ok': True, 'version': '0.2.0'}
            elif path == '/api/profiles':
                data = {'profiles': [dict(p, has_credential=p.get('credential_saved', False)) for p in providers.list_profiles()], 'grading_profile_id': cli.load_registry().get('grading_profile_id')}
            elif path == '/api/tools':
                data = documents.tool_status()
            elif path == '/api/oauth/status':
                data = oauth.status('chatgpt')
            elif path == '/api/library':
                cli.load_registry()
                data = {'workspaces': sorted([v for v in cli.STATE.workspaces.values() if Path(v['root']).is_dir()], key=lambda v: v.get('updated_at', ''), reverse=True)}
            elif path.startswith('/api/jobs/'):
                tail = path[len('/api/jobs/'):]
                key = tail.removesuffix('/log')
                job = cli.STATE.jobs.get(key)
                if not job:
                    return cli.json_response(self, 404, {'error': 'Auftrag nicht gefunden.'})
                if tail.endswith('/log'):
                    return cli.text_response(self, 200, cli.job_log_text(job))
                data = job.__dict__.copy()
            else:
                return cli.json_response(self, 404, {'error': 'Nicht gefunden.'})
            cli.json_response(self, 200, data)
        except Exception as exc:
            cli.json_response(self, 400, {'error': cli.redact_log(str(exc))})

    def do_POST(self):
        path = urlparse(self.path).path
        if path.startswith('/preview/'):
            return self.preview('POST')
        if not self.authorized():
            return
        try:
            from . import providers, documents, oauth
            payload = cli.read_json(self)
            if path == '/api/check':
                checks = [{'id': 'backend', 'label': 'Lokaler Dienst', 'ok': True, 'detail': 'Verbunden'}]
                if payload.get('input_path') and payload.get('output_path'):
                    cli.ensure_separate_output(payload['input_path'], payload['output_path'])
                data = {'ok': True, 'checks': checks, 'default_output': str(cli.DEFAULT_OUTPUT_DIR)}
            elif path == '/api/profiles':
                data = providers.save_profile(payload)
            elif path == '/api/profiles/delete':
                providers.delete_profile(payload['id'])
                data = {'ok': True}
            elif path == '/api/models':
                data = {'models': providers.list_models(payload.get('profile_id', 'ollama'))}
            elif path == '/api/test-model':
                data = providers.test_profile(payload.get('profile_id', 'ollama'), payload.get('model'))
            elif path == '/api/oauth/start':
                data = oauth.start('chatgpt')
                data['url'] = data.get('authorization_url')
            elif path == '/api/oauth/disconnect':
                data = oauth.disconnect('chatgpt') or {'ok': True}
            elif path == '/api/scan':
                data = cli.scan_documents(payload)
            elif path == '/api/extract':
                scan = cli.scan_documents(payload)
                source = next((s for s in scan['sources'] if s['id'] == payload.get('source_id')), None)
                if not source:
                    raise ValueError('Dokument nicht gefunden. Material erneut einlesen.')
                cache = Path.home() / '.exam-generator-desktop' / 'extraction-cache'
                data = documents.extract_document(Path(source['path']), cache, mode=payload.get('mode', 'auto'))
            elif path == '/api/generate':
                data = {'job_id': cli.start_job(payload).id}
            elif path.startswith('/api/jobs/') and path.endswith('/cancel'):
                key = path[len('/api/jobs/'):-len('/cancel')]
                job = cli.STATE.jobs.get(key)
                if not job:
                    return cli.json_response(self, 404, {'error': 'Auftrag nicht gefunden.'})
                data = {'ok': True, 'canceled': cli.request_job_cancel(job), 'job': job.__dict__}
            elif path == '/api/open-preview':
                cli.load_registry()
                workspace_id = payload.get('workspace_id')
                if workspace_id not in cli.STATE.workspaces:
                    raise ValueError('Arbeitsbereich nicht gefunden.')
                key = secrets.token_urlsafe(32)
                with cli.STATE.lock:
                    cli.STATE.preview_tokens = {k: v for k, v in cli.STATE.preview_tokens.items() if v['expires'] > time.monotonic()}
                    cli.STATE.preview_tokens[key] = {'workspace_id': workspace_id, 'expires': time.monotonic() + 60, 'launch': True}
                data = {'url': f'http://127.0.0.1:{cli.STATE.port}/launch/{key}'}
            elif path == '/api/settings':
                data = cli.load_registry()
                profile_id = payload.get('grading_profile_id')
                if profile_id is not None and profile_id not in {p['id'] for p in providers.list_profiles()}:
                    raise ValueError('Bewertungs-Verbindung nicht gefunden.')
                data['grading_profile_id'] = profile_id
                cli.save_registry(data)
                data = {'ok': True, 'grading_profile_id': profile_id}
            else:
                return cli.json_response(self, 404, {'error': 'Nicht gefunden.'})
            cli.json_response(self, 200, data)
        except Exception as exc:
            cli.json_response(self, 400, {'error': cli.redact_log(str(exc))})

    def launch(self, key):
        if not self.boundary():
            return
        with cli.STATE.lock:
            entry = cli.STATE.preview_tokens.pop(key, None)
            if not entry or not entry.get('launch') or entry['expires'] < time.monotonic():
                return cli.json_response(self, 403, {'error': 'Vorschau-Link abgelaufen. In der App erneut öffnen.'})
            session = secrets.token_urlsafe(32)
            wid = entry['workspace_id']
            cli.STATE.preview_tokens[session] = dict(entry, launch=False, expires=time.monotonic() + 86400)
        self.send_response(303)
        self.send_header('Set-Cookie', f'exam_{wid}={session}; Path=/preview/{wid}/; HttpOnly; SameSite=Strict; Max-Age=86400')
        self.send_header('Location', f'/preview/{wid}/exam_index.html')
        self.end_headers()

    def preview(self, method):
        if not self.boundary():
            return
        parsed = urlparse(self.path)
        parts = parsed.path.split('/', 3)
        if len(parts) != 4:
            return cli.json_response(self, 404, {'error': 'Nicht gefunden.'})
        wid, rel = parts[2], unquote(parts[3])
        cookies = SimpleCookie()
        try:
            cookies.load(self.headers.get('Cookie', ''))
        except Exception:
            return cli.json_response(self, 403, {'error': 'Vorschau nicht autorisiert.'})
        morsel = cookies.get('exam_' + wid)
        entry = cli.STATE.preview_tokens.get(morsel.value if morsel else '')
        if not entry or entry.get('launch') or entry['workspace_id'] != wid or entry['expires'] < time.monotonic():
            return cli.json_response(self, 403, {'error': 'Vorschau in der App erneut öffnen.'})
        workspace = cli.STATE.workspaces.get(wid)
        if not workspace:
            return cli.json_response(self, 404, {'error': 'Arbeitsbereich nicht gefunden.'})
        root = Path(workspace['root']).resolve()
        try:
            if rel in {'attempt', 'grade-open-answer'}:
                payload = cli.read_json(self) if method == 'POST' else parse_qs(parsed.query)
                exam_path = payload.get('exam_path', '')
                if isinstance(exam_path, list):
                    exam_path = exam_path[0]
                if not isinstance(exam_path, str) or not ARTIFACT.fullmatch(exam_path) or not exam_path.endswith('/exam.json'):
                    raise ValueError('Ungültige Prüfung.')
                target = (root / exam_path).resolve()
                target.relative_to(root)
                raw = target.read_bytes()
                fingerprint = hashlib.sha256(raw).hexdigest()
                exam = json.loads(raw)
                attempts_dir = root / '.attempts'
                if attempts_dir.is_symlink():
                    raise ValueError('Ungültiger Lernstand-Ordner.')
                attempt_path = (attempts_dir / (hashlib.sha256(exam_path.encode()).hexdigest() + '.json')).resolve()
                attempt_path.relative_to(root)
                if rel == 'attempt':
                    if method == 'POST':
                        if payload.get('fingerprint') != fingerprint:
                            return cli.json_response(self, 409, {'error': 'Die Prüfung wurde geändert. Bitte neu öffnen.'})
                        data = payload.get('attempt')
                        if not isinstance(data, dict):
                            raise ValueError('Ungültiger Lernstand.')
                        runtime.atomic_json(attempt_path, {'fingerprint': fingerprint, 'attempt': data})
                        return cli.json_response(self, 200, {'ok': True})
                    try:
                        saved = json.loads(attempt_path.read_text(encoding='utf-8'))
                    except (OSError, ValueError):
                        saved = {}
                    return cli.json_response(self, 200, {'fingerprint': fingerprint, 'attempt': saved.get('attempt') if saved.get('fingerprint') == fingerprint else None})
                if method != 'POST':
                    return cli.json_response(self, 405, {'error': 'POST erforderlich.'})
                question = next((q for q in exam.get('open_ended', []) if q['id'] == payload.get('question_id')), None)
                if question is None:
                    raise ValueError('Frage nicht gefunden.')
                answer = payload.get('student_answer')
                if not isinstance(answer, str) or len(answer) > 50000:
                    raise ValueError('Antwort ist ungültig oder zu lang.')
                from . import providers, local_server
                metadata = exam.get('metadata', {})
                profile_id = cli.load_registry().get('grading_profile_id') or metadata.get('provider_id') or 'ollama'
                profile = next(p for p in providers.list_profiles() if p['id'] == profile_id)
                model = profile.get('model') if cli.load_registry().get('grading_profile_id') else metadata.get('model') or profile.get('model')
                rubric = question.get('grading_rubric') or exam.get('rubric_templates', {}).get(question.get('rubric_template')) or {}
                body = dict(question=question['question'], student_answer=answer, expected_answer=question.get('expected_answer'), key_concepts=question.get('key_concepts', []), grading_rubric=rubric)
                prompt = local_server.TUTOR_EVALUATOR_SYSTEM_PROMPT
                if metadata.get('language') == 'en':
                    prompt += '\nOUTPUT REQUIREMENT: Return all feedback in English.'
                result = providers.complete(profile_id, model, [{'role': 'system', 'content': prompt}, {'role': 'user', 'content': local_server.build_user_prompt(body)}], timeout=180, temperature=0, profile_snapshot=None if cli.load_registry().get('grading_profile_id') else metadata.get('profile_snapshot'))
                if result.finish_reason in {'length', 'incomplete', 'max_tokens'}:
                    raise ValueError('Bewertung wurde abgeschnitten. Bitte erneut versuchen.')
                data = local_server.normalize_grading_result(local_server.extract_json_object(result.text))
                return cli.json_response(self, 200, data)
            if method != 'GET' or not ARTIFACT.fullmatch(rel) or any(part.startswith('.') for part in Path(rel).parts):
                return cli.json_response(self, 404, {'error': 'Nicht gefunden.'})
            target = (root / rel).resolve()
            target.relative_to(root)
            if not target.is_file():
                return cli.json_response(self, 404, {'error': 'Datei nicht gefunden.'})
            body = target.read_bytes()
            self.send_response(200)
            self.send_header('Content-Type', {'.html': 'text/html; charset=utf-8', '.json': 'application/json; charset=utf-8', '.txt': 'text/plain; charset=utf-8'}[target.suffix])
            self.send_header('Content-Security-Policy', "default-src 'none'; script-src 'unsafe-inline'; style-src 'unsafe-inline'; connect-src 'self'; img-src 'self' data:; base-uri 'none'; frame-ancestors 'none'; form-action 'none'")
            self.send_header('Content-Length', str(len(body)))
            self.end_headers()
            self.wfile.write(body)
        except Exception as exc:
            cli.json_response(self, 400, {'error': cli.redact_log(str(exc))})


def serve(port):
    cli.load_registry()
    server = ThreadingHTTPServer(('127.0.0.1', port), Handler)
    server.daemon_threads = True
    cli.STATE.port = server.server_address[1]
    print(json.dumps({'event': 'ready', 'url': f'http://127.0.0.1:{cli.STATE.port}', 'port': cli.STATE.port, 'token': cli.STATE.token}), flush=True)
    def shutdown_listener():
        import sys
        for line in sys.stdin:
            if line.strip() == 'shutdown':
                for event in cli.STATE.events.values():
                    event.set()
                server.shutdown()
                return
    threading.Thread(target=shutdown_listener, daemon=True).start()
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        for event in cli.STATE.events.values():
            event.set()
        server.server_close()
    return 0
