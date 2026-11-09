"""Local UI fixture with a deterministic Ollama-protocol server; never calls real AI."""
import json
import os
from pathlib import Path
import sys
import tempfile
import threading
from http.server import BaseHTTPRequestHandler

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / 'backend'))
from exam_backend import cli, providers
from exam_backend.http_server import LoopbackServer

class FixtureAI(BaseHTTPRequestHandler):
    def log_message(self, *args): pass
    def do_GET(self):
        data = json.dumps({'models': [{'name': 'fixture-model'}]}).encode()
        self.send_response(200); self.send_header('Content-Length', str(len(data))); self.end_headers(); self.wfile.write(data)
    def do_POST(self):
        body = json.loads(self.rfile.read(int(self.headers['Content-Length'])))
        prompt = body['messages'][-1]['content']
        if 'Prüfungsfrage:' in prompt:
            result = {'score': 75, 'feedback': 'Die Kernidee stimmt. Ergänze den Zusammenhang mit dem langfristigen Behalten.', 'verdict': 'Gut verstanden', 'what_was_good': ['Aktives Abrufen erkannt'], 'missing_key_points': ['Langfristiges Behalten'], 'model_answer': 'Aktives Abrufen unterstützt das langfristige Behalten.'}
        elif 'coverage blueprint' in prompt:
            result = {'major_topics': [{'topic': 'Lernen', 'exam_targets': ['Aktives Abrufen erklären']}]}
        elif 'Return exactly this JSON object' in prompt:
            result = {'ok': True}
        else:
            result = {'multiple_choice': [
                {'question': 'Was unterstützt das langfristige Behalten?', 'explanation': 'Die Quelle nennt aktives Abrufen.', 'topic': 'Lernen', 'options': [{'text': 'Aktives Abrufen', 'is_correct': True}, {'text': 'Nur passives Lesen', 'is_correct': False}, {'text': 'Unterlagen nie schließen', 'is_correct': False}, {'text': 'Auf Wiederholung verzichten', 'is_correct': False}], '_meta': {'source_excerpt': 'Aktives Abrufen unterstützt das langfristige Behalten.'}},
                {'question': 'Welche Aussagen widersprechen der Quelle?', 'explanation': 'Keine der Aussagen widerspricht der Quelle.', 'options': [{'text': 'Aktives Abrufen unterstützt Lernen', 'is_correct': False}, {'text': 'Wiederholung ist hilfreich', 'is_correct': False}, {'text': 'Feedback hilft bei der Korrektur', 'is_correct': False}, {'text': 'Lernende können selbst prüfen', 'is_correct': False}]}],
                'open_ended': [{'id': 'open-1', 'question': 'Wie unterstützt aktives Abrufen das Lernen?', 'expected_answer': 'Aktives Abrufen unterstützt das langfristige Behalten.', 'key_concepts': ['Aktives Abrufen', 'Behalten']}]}
        data = (json.dumps({'message': {'content': json.dumps(result, ensure_ascii=False)}, 'done': True, 'done_reason': 'stop'}) + '\n').encode()
        self.send_response(200); self.send_header('Content-Type', 'application/x-ndjson'); self.send_header('Content-Length', str(len(data))); self.end_headers(); self.wfile.write(data)

with tempfile.TemporaryDirectory(prefix='exam-ui-fixture-') as temp:
    root = Path(temp)
    os.environ['HOME'] = str(root)
    source = root / 'Material'; source.mkdir()
    (source / 'Lernen.md').write_text('# Lernen\n\nAktives Abrufen unterstützt das langfristige Behalten. Wiederholung ist hilfreich. Feedback hilft bei der Korrektur. Lernende können selbst prüfen.', encoding='utf-8')
    ai = LoopbackServer(('127.0.0.1', 0), FixtureAI)
    threading.Thread(target=ai.serve_forever, daemon=True).start()
    providers.save_profile({'id': 'ollama', 'kind': 'ollama', 'name': 'Lokale Test-KI', 'base_url': f'http://127.0.0.1:{ai.server_port}', 'model': 'fixture-model'})
    from exam_backend import service
    server = LoopbackServer(('127.0.0.1', 0), service.Handler)
    cli.STATE.port = server.server_port
    info = {'url': f'http://127.0.0.1:{server.server_port}', 'token': cli.STATE.token, 'input_path': str(source), 'output_path': str(root / 'Output')}
    connection = Path('/private/tmp/exam-ui-fixture-connection.json')
    connection.write_text(json.dumps(info))
    print('UI fixture ready; connection stored in temporary file.', flush=True)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    try:
        sys.stdin.readline()
    finally:
        for event in cli.STATE.events.values(): event.set()
        server.shutdown(); server.server_close(); ai.shutdown(); ai.server_close()
        connection.unlink(missing_ok=True)
