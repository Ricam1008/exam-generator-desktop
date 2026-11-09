#!/usr/bin/env python3
"""Verify a packaged backend against a local AI stand-in and a new dummy Keychain item.

No application source modules are imported. All documents/settings use a temporary
directory; HOME stays intact for Keychain. Only a fresh test credential is touched.
The stand-in does not contact another service or measure real model quality.
"""
from __future__ import annotations

import argparse
from datetime import datetime, timezone
import hashlib
from http.cookiejar import CookieJar
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import os
from pathlib import Path
import queue
import secrets
from socketserver import TCPServer
import subprocess
import sys
import tempfile
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid


SERVICE = 'app.exam-generator.desktop'
MODEL = 'local-smoke-model'
SOURCE = ('Retrieval practice improves memory. Spaced retrieval supports long-term retention.\n\n'
          'Working memory holds information for current cognitive tasks. Comparing concepts supports learning.')


class Blocked(RuntimeError):
    """A required protected platform capability could not be exercised."""


class ApiError(RuntimeError):
    def __init__(self, path, status, payload):
        super().__init__(f'{path}: HTTP {status}: {payload.get("error", "request failed") if isinstance(payload, dict) else "request failed"}')
        self.status, self.payload = status, payload


class LocalServer(ThreadingHTTPServer):
    daemon_threads = True

    def server_bind(self):
        TCPServer.server_bind(self)
        self.server_name = 'localhost'
        self.server_port = self.server_address[1]


def exam_answer():
    return {
        'multiple_choice': [
            {'id': 'smoke-mc-1', 'question': 'Was verbessert laut Quelle das Gedächtnis?', 'topic': 'Gedächtnis', 'explanation': 'Die Quelle nennt Retrieval practice.', 'options': [{'text': 'Abrufübungen', 'is_correct': True}, {'text': 'Nur Ablenkung', 'is_correct': False}, {'text': 'Keine Wiederholung', 'is_correct': False}, {'text': 'Ausschließlich Raten', 'is_correct': False}]},
            {'id': 'smoke-mc-2', 'question': 'Welche Aufgabe hat das Arbeitsgedächtnis laut Quelle?', 'topic': 'Arbeitsgedächtnis', 'explanation': 'Es hält Informationen für aktuelle Aufgaben bereit.', 'options': [{'text': 'Informationen für aktuelle Aufgaben halten', 'is_correct': True}, {'text': 'Jede Erinnerung dauerhaft speichern', 'is_correct': False}, {'text': 'Jeden Lernvorgang verhindern', 'is_correct': False}, {'text': 'Wiederholung ausschließen', 'is_correct': False}]},
        ],
        'open_ended': [{'id': 'smoke-open-1', 'question': 'Erkläre den Nutzen von Abrufübungen.', 'expected_answer': 'Abrufübungen verbessern laut Quelle das Gedächtnis.', 'key_concepts': ['Abrufübungen', 'Gedächtnis'], 'rubric_template': 'standard_concept_explanation', 'max_score': 100}],
    }


def response_for(messages):
    user = '\n'.join(str(item.get('content', '')) for item in messages if item.get('role') == 'user')
    if 'Return exactly this JSON object:' in user:
        return {'ok': True}, 'connection-test'
    if 'Antwort der lernenden Person:' in user:
        if 'Retrieval helps memory.' not in user or 'Abrufübungen verbessern laut Quelle das Gedächtnis.' not in user:
            raise AssertionError('Grading did not use the real answer and stored expected answer.')
        return {'score': 88, 'max_score': 100, 'verdict': 'Die Kernidee stimmt.', 'feedback': 'Abrufübungen verbessern das Gedächtnis; die Antwort trifft diese Kernidee.', 'model_answer': 'Abrufübungen verbessern laut Quelle das Gedächtnis.'}, 'grading'
    if 'Create a compact final-exam coverage blueprint' in user:
        return {'chunk': 1, 'major_topics': [{'topic': 'Gedächtnis', 'exam_targets': ['Abrufübungen erklären'], 'common_traps': [], 'applications_or_transfer': ['Lernplan'], 'source_area': 'lecture.txt'}]}, 'final-blueprint'
    if 'final-exam multiple-choice questions' in user:
        return {'multiple_choice': exam_answer()['multiple_choice']}, 'final-mc'
    if 'final-exam open-ended questions' in user:
        return {'open_ended': exam_answer()['open_ended']}, 'final-open'
    if 'standalone exam JSON object' in user:
        return exam_answer(), 'generation'
    raise AssertionError('The local stand-in received an unexpected request kind.')


def standin(dummy_key):
    calls, errors = [], []
    lock = threading.Lock()
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass

        def authorized(self):
            if self.headers.get('Authorization') != 'Bearer ' + dummy_key:
                with lock:
                    errors.append('The local AI request did not carry the saved dummy credential.')
                self.send_response(401)
                self.send_header('Content-Length', '0')
                self.end_headers()
                return False
            return True

        def do_GET(self):
            if not self.authorized():
                return
            if self.path != '/v1/models':
                self.send_error(404)
                return
            data = json.dumps({'data': [{'id': MODEL}]}).encode()
            with lock:
                calls.append('models')
            self.send_response(200)
            self.send_header('Content-Type', 'application/json')
            self.send_header('Content-Length', str(len(data)))
            self.end_headers()
            self.wfile.write(data)

        def do_POST(self):
            if not self.authorized():
                return
            try:
                if self.path != '/v1/chat/completions':
                    raise AssertionError('Unexpected inference URL.')
                size = int(self.headers.get('Content-Length', '0'))
                if not 0 < size <= 1_048_576:
                    raise AssertionError('Invalid inference body size.')
                raw = self.rfile.read(size)
                if dummy_key.encode() in raw:
                    raise AssertionError('Credential leaked into an inference prompt.')
                payload = json.loads(raw)
                if payload.get('model') != MODEL or payload.get('stream') is not True:
                    raise AssertionError('Wrong model or missing streaming request.')
                answer, kind = response_for(payload.get('messages', []))
                with lock:
                    calls.append(kind)
                content = json.dumps(answer, ensure_ascii=False)
                midpoint = max(1, len(content) // 2)
                events = [{'choices': [{'index': 0, 'delta': {'content': piece}, 'finish_reason': None}]} for piece in (content[:midpoint], content[midpoint:])]
                events.append({'choices': [{'index': 0, 'delta': {}, 'finish_reason': 'stop'}], 'usage': {'prompt_tokens': 20, 'completion_tokens': 30, 'total_tokens': 50}})
                body = ''.join('data: ' + json.dumps(event, ensure_ascii=False) + '\n\n' for event in events) + 'data: [DONE]\n\n'
                data = body.encode()
                self.send_response(200)
                self.send_header('Content-Type', 'text/event-stream')
                self.send_header('Content-Length', str(len(data)))
                self.end_headers()
                self.wfile.write(data)
            except Exception as exc:
                with lock:
                    errors.append(str(exc))
                self.send_error(400, 'Local stand-in rejected the request.')
    server = LocalServer(('127.0.0.1', 0), Handler)
    thread = threading.Thread(target=server.serve_forever, kwargs={'poll_interval': 0.05}, daemon=True)
    thread.start()
    return server, thread, calls, errors


class PackedBackend:
    def __init__(self, binary, root):
        self.binary, self.root = binary, root
        self.process = None
        self.url, self.token = '', ''
        self.stdout_thread = None
        self.stderr = None

    def start(self):
        home, cwd = self.root / 'home', self.root / 'empty-cwd'
        home.mkdir(exist_ok=True)
        cwd.mkdir(exist_ok=True)
        env = os.environ.copy()
        for name in ('PYTHONPATH', 'PYTHONHOME', 'VIRTUAL_ENV', 'CONDA_PREFIX', 'DYLD_LIBRARY_PATH', 'DYLD_FRAMEWORK_PATH'):
            env.pop(name, None)
        env.update({'EXAM_GENERATOR_DATA_DIR': str(home / '.exam-generator-desktop'), 'PATH': '/usr/bin:/bin:/usr/sbin:/sbin', 'HF_HOME': str(home / '.cache' / 'huggingface'), 'XDG_CACHE_HOME': str(home / '.cache'), 'HF_HUB_OFFLINE': '1', 'TRANSFORMERS_OFFLINE': '1', 'PYINSTALLER_RESET_ENVIRONMENT': '1', 'HTTP_PROXY': 'http://127.0.0.1:9', 'HTTPS_PROXY': 'http://127.0.0.1:9', 'NO_PROXY': '127.0.0.1,localhost'})
        self.stderr = (self.root / 'backend.stderr').open('a+')
        self.process = subprocess.Popen([str(self.binary), 'serve', '--port', '0'], stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=self.stderr, text=True, cwd=cwd, env=env)
        process = self.process
        ready = queue.Queue()
        def drain():
            try:
                ready.put(process.stdout.readline())
                for _ in process.stdout:
                    pass
            except Exception:
                ready.put('')
        self.stdout_thread = threading.Thread(target=drain, daemon=True)
        self.stdout_thread.start()
        try:
            event = json.loads(ready.get(timeout=45))
            parsed = urllib.parse.urlsplit(event.get('url', ''))
            if event.get('event') != 'ready' or parsed.hostname != '127.0.0.1' or not parsed.port or not event.get('token'):
                raise ValueError('Invalid packed backend handshake.')
            self.url, self.token = event['url'], event['token']
        except (queue.Empty, ValueError, TypeError) as exc:
            self.stop()
            raise RuntimeError('The packaged backend did not produce a valid ready handshake within45 seconds.') from exc

    def stop(self):
        if self.process is not None and self.process.poll() is None:
            try:
                self.process.stdin.write('shutdown\n')
                self.process.stdin.flush()
                self.process.wait(timeout=10)
            except (OSError, subprocess.TimeoutExpired):
                self.process.kill()
                self.process.wait(timeout=3)
        if self.process is not None:
            for stream in (self.process.stdin, self.process.stdout):
                if stream is not None:
                    stream.close()
        if self.stdout_thread is not None:
            self.stdout_thread.join(timeout=2)
        if self.stderr is not None:
            self.stderr.close()
        self.process = self.stderr = self.stdout_thread = None

    def call(self, path, payload=None, *, auth=True, opener=None, expected=200):
        headers = {'Authorization': 'Bearer ' + self.token} if auth else {}
        body = None
        if payload is not None:
            body = json.dumps(payload).encode()
            headers['Content-Type'] = 'application/json'
        request = urllib.request.Request(self.url + path, data=body, headers=headers, method='POST' if payload is not None else 'GET')
        client = opener or urllib.request.build_opener(urllib.request.ProxyHandler({}))
        try:
            with client.open(request, timeout=60) as response:
                data = response.read()
                status = response.status
                content_type = response.headers.get('Content-Type', '')
        except urllib.error.HTTPError as exc:
            data, status, content_type = exc.read(), exc.code, exc.headers.get('Content-Type', '')
        payload = json.loads(data) if 'application/json' in content_type else data.decode('utf-8', errors='replace')
        if status != expected:
            raise ApiError(path, status, payload)
        return payload

    def job(self, payload):
        job_id = self.call('/api/generate', payload)['job_id']
        deadline = time.monotonic() + 45
        while time.monotonic() < deadline:
            job = self.call('/api/jobs/' + job_id)
            if job['status'] not in {'running', 'stopping'}:
                if job['status'] != 'done':
                    raise RuntimeError('Packaged generation failed: ' + str(job.get('error') or job.get('result') or job['status']))
                return job['result']
            time.sleep(0.05)
        self.call('/api/jobs/' + job_id + '/cancel', {})
        raise RuntimeError('Packaged generation did not finish within45 seconds.')


def active_exams(root):
    return [path for path in root.rglob('exams/*/exam.json') if not any(part.startswith('.') for part in path.relative_to(root).parts)]


def check_counts(path):
    exam = json.loads(path.read_text(encoding='utf-8'))
    if len(exam.get('multiple_choice', [])) != 2 or len(exam.get('open_ended', [])) != 1:
        raise AssertionError('The custom2 MC/1 open targets were not preserved.')
    if any(type(option.get('is_correct')) is not bool for question in exam['multiple_choice'] for option in question['options']):
        raise AssertionError('Generated answer keys are not strict booleans.')
    return exam


def delete_dummy_key(profile_id):
    """Remove only this run's fresh credential ID, including after backend failure."""
    if os.uname().sysname != 'Darwin':
        raise Blocked('The packaged Keychain smoke is intended for macOS.')
    result = subprocess.run(['/usr/bin/security', 'delete-generic-password', '-s', SERVICE, '-a', profile_id], capture_output=True, text=True, timeout=20)
    # 44 means the item is absent, including after successful API cleanup.
    if result.returncode not in {0, 44}:
        raise Blocked('The dummy Keychain entry could not be removed. Profile ID: ' + profile_id)


def run(binary, report):
    dummy_key, profile_id = 'smoke-key-' + secrets.token_urlsafe(32), 'smoke-' + uuid.uuid4().hex
    report['profile_id'] = profile_id
    server = server_thread = backend = None
    profile_attempted = False
    try:
        with tempfile.TemporaryDirectory(prefix='exam-packaged-generation-') as temporary:
            root = Path(temporary).resolve()
            source = root / 'input'
            source.mkdir()
            lecture = source / 'lecture.txt'
            lecture.write_text(SOURCE, encoding='utf-8')
            initial_hash = hashlib.sha256(lecture.read_bytes()).hexdigest()
            server, server_thread, calls, errors = standin(dummy_key)
            backend = PackedBackend(binary, root)
            backend.start()
            report['checks'].append('packed backend starts with isolated app state and empty cwd; macOS HOME is preserved')
            profile = {'id': profile_id, 'kind': 'compatible', 'name': 'Local packaged smoke', 'base_url': f'http://127.0.0.1:{server.server_port}/v1', 'model': MODEL, 'api_key': dummy_key}
            profile_attempted = True
            try:
                saved = backend.call('/api/profiles', profile)
            except ApiError as exc:
                message = str(exc).casefold()
                if 'schlüssel' in message or 'zugangsdaten' in message or 'credential' in message or 'keychain' in message:
                    report['keychain_api_error'] = str(exc)
                    raise Blocked('Protected Keychain storage is inaccessible to the packaged backend; no plaintext fallback was used.') from exc
                raise
            except (TimeoutError, urllib.error.URLError) as exc:
                report['keychain_api_error'] = 'The initial protected credential save did not return within the request timeout.'
                raise Blocked('The protected credential save could not be completed. Check whether macOS requires Keychain interaction; no plaintext fallback was used.') from exc
            if not saved.get('credential_saved'):
                raise AssertionError('The profile was not saved with protected credentials.')
            profiles = backend.call('/api/profiles')
            if dummy_key in json.dumps(saved) or dummy_key in json.dumps(profiles) or dummy_key in (root / 'home' / '.exam-generator-desktop' / 'profiles.json').read_text():
                raise AssertionError('The API key leaked into an API response or plaintext settings.')
            report['checks'].append('new dummy credential is protected and absent from API/settings output')
            backend.stop()
            backend.start()
            models = backend.call('/api/models', {'profile_id': profile_id})
            if MODEL not in models.get('models', []):
                raise AssertionError('The restarted package did not load the local provider model list.')
            tested = backend.call('/api/test-model', {'profile_id': profile_id, 'model': MODEL})
            if not tested.get('ok'):
                raise AssertionError('The restarted package could not complete an authenticated model test: ' + str(tested.get('detail')))
            report['checks'].append('same packed binary reads dummy Keychain credential after restart; model list/test succeed')
            scanned = backend.call('/api/scan', {'input_path': str(source)})
            if len(scanned.get('sources', [])) != 1:
                raise AssertionError('The synthetic source was not discovered.')
            payload = {'input_path': str(source), 'output_path': str(root / 'output'), 'source_ids': [scanned['sources'][0]['id']], 'profile_id': profile_id, 'model': MODEL, 'mode': 'all', 'language': 'de', 'difficulty': 'balanced', 'coverage_mode': 'representative', 'target_mode': 'manual', 'target_mc': 2, 'target_open': 1}
            first = backend.job(payload)
            workspace = Path(first['project_root']).resolve()
            if not workspace.is_relative_to(root) or len(first.get('generated', [])) != 1:
                raise AssertionError('The generated workspace/result is invalid.')
            exams = active_exams(workspace)
            if len(exams) != 1:
                raise AssertionError('Generation did not publish exactly one active exam.')
            exam = check_counts(exams[0])
            if hashlib.sha256(lecture.read_bytes()).hexdigest() != initial_hash or (source / 'exams').exists():
                raise AssertionError('Generation changed the original material.')
            report['checks'].append('real packed generation publishes2 MC/1 open; originals stay unchanged')
            before_reuse = len(calls)
            reused = backend.job(payload)
            if len(reused.get('reused', [])) != 1 or reused.get('generated') or len(calls) != before_reuse:
                raise AssertionError('An unchanged run did not reuse its exam without another AI request.')
            lecture.write_text(SOURCE + '\n\nRepeated retrieval can be scheduled over multiple days.', encoding='utf-8')
            changed_scan = backend.call('/api/scan', {'input_path': str(source)})
            if changed_scan['sources'][0]['fingerprint'] == scanned['sources'][0]['fingerprint']:
                raise AssertionError('The changed source fingerprint was not detected.')
            regenerated = backend.job(payload)
            if len(regenerated.get('generated', [])) != 1 or len(calls) <= before_reuse:
                raise AssertionError('Changed material did not trigger fresh generation.')
            exams = active_exams(workspace)
            if len(exams) != 1:
                raise AssertionError('Regeneration exposed an old backup as another exam.')
            exam = check_counts(exams[0])
            report['checks'].append('unchanged source reuses output; changed source regenerates without duplicate backup exams')
            final = backend.job({**payload, 'output_path': str(root / 'fresh-finals'), 'mode': 'finals'})
            final_root = Path(final['project_root']).resolve()
            final_exams = active_exams(final_root)
            if len(final_exams) != 1 or not check_counts(final_exams[0])['metadata'].get('is_final_exam'):
                raise AssertionError('Fresh finals still require pre-existing deck exams.')
            report['checks'].append('fresh course final generates from source without deck-exam prerequisite')
            opener = urllib.request.build_opener(urllib.request.ProxyHandler({}), urllib.request.HTTPCookieProcessor(CookieJar()))
            launch = backend.call('/api/open-preview', {'workspace_id': regenerated['workspace_id']})['url']
            parsed = urllib.parse.urlsplit(launch)
            if parsed.hostname != '127.0.0.1' or parsed.port != urllib.parse.urlsplit(backend.url).port:
                raise AssertionError('The preview launch escaped the local backend.')
            with opener.open(launch, timeout=10) as response:
                response.read()
            exam_path = exams[0].relative_to(workspace).as_posix()
            preview = '/preview/' + regenerated['workspace_id']
            attempt = backend.call(preview + '/attempt?' + urllib.parse.urlencode({'exam_path': exam_path}), auth=False, opener=opener)
            if attempt['fingerprint'] != hashlib.sha256(exams[0].read_bytes()).hexdigest():
                raise AssertionError('Preview fingerprint does not match the published exam.')
            saved_attempt = {'answers': {exam['open_ended'][0]['id']: 'Retrieval helps memory.'}}
            backend.call(preview + '/attempt', {'exam_path': exam_path, 'fingerprint': attempt['fingerprint'], 'attempt': saved_attempt}, auth=False, opener=opener)
            restored = backend.call(preview + '/attempt?' + urllib.parse.urlencode({'exam_path': exam_path}), auth=False, opener=opener)
            if restored['attempt'] != saved_attempt:
                raise AssertionError('The saved attempt was not restored.')
            backend.call('/api/health', auth=False, opener=opener, expected=401)
            grade_payload = {'exam_path': exam_path, 'fingerprint': attempt['fingerprint'], 'question_id': exam['open_ended'][0]['id'], 'student_answer': 'Retrieval helps memory.'}
            grade = backend.call(preview + '/grade-open-answer', grade_payload, auth=False, opener=opener)
            if grade.get('score') != 88 or not grade.get('feedback'):
                raise AssertionError('The packed grading stream was not normalized correctly.')
            before_stale = len(calls)
            backend.call(preview + '/grade-open-answer', {**grade_payload, 'fingerprint': 'stale'}, auth=False, opener=opener, expected=409)
            if len(calls) != before_stale:
                raise AssertionError('A stale exam triggered a provider call.')
            report['checks'].append('scoped preview restores attempts and grades through saved profile; stale grading is blocked')
            if errors:
                raise AssertionError('; '.join(errors))
            expected_calls = {'models', 'connection-test', 'generation', 'final-blueprint', 'final-mc', 'final-open', 'grading'}
            if not expected_calls <= set(calls):
                raise AssertionError('Not all real provider flows reached the authenticated local stand-in.')
            report['authorized_local_requests'] = len(calls)
            report['provider_flows'] = sorted(set(calls))
            report['remote_ai_requests'] = 0
            backend.call('/api/profiles/delete', {'id': profile_id})
            remaining = backend.call('/api/profiles')['profiles']
            if any(profile['id'] == profile_id for profile in remaining):
                raise AssertionError('The dummy profile was not deleted.')
            report['checks'].append('dummy profile removed through packed API')
            report['status'] = 'pass'
    finally:
        if backend is not None:
            backend.stop()
        if server is not None:
            server.shutdown()
            server.server_close()
            server_thread.join(timeout=2)
        if profile_attempted:
            previous_error = sys.exc_info()[0] is not None
            try:
                delete_dummy_key(profile_id)
                report['keychain_entry_removed'] = True
            except (Blocked, OSError, subprocess.TimeoutExpired) as exc:
                report['keychain_entry_removed'] = False
                report['cleanup_error'] = str(exc)
                if not previous_error:
                    raise Blocked('The smoke finished, but removal of its own dummy Keychain entry could not be confirmed. Profile ID: ' + profile_id) from exc


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--backend', required=True, type=Path, help='Packaged backend executable, not a Python entry point.')
    parser.add_argument('--report', required=True, type=Path, help='JSON result path.')
    args = parser.parse_args()
    binary = args.backend.expanduser().resolve()
    report = {'timestamp': datetime.now(timezone.utc).isoformat(), 'backend': str(binary), 'status': 'running', 'checks': [], 'uses_synthetic_sources': True, 'uses_dummy_credentials_only': True}
    try:
        if not binary.is_file() or not os.access(binary, os.X_OK):
            raise RuntimeError('The packaged backend executable does not exist or is not executable.')
        if os.uname().sysname != 'Darwin':
            raise Blocked('This protected Keychain smoke requires macOS.')
        run(binary, report)
    except Blocked as exc:
        report.update(status='blocked', error=str(exc))
    except Exception as exc:
        report.update(status='fail', error=str(exc))
    output = args.report.expanduser().resolve()
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(report, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')
    print(json.dumps({'status': report['status'], 'checks_passed': len(report['checks']), 'report': str(output)}, ensure_ascii=False))
    return {'pass': 0, 'blocked': 2}.get(report['status'], 1)


if __name__ == '__main__':
    raise SystemExit(main())
