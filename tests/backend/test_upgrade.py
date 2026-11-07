"""Cross-module regressions for the upgrade's data and output boundaries."""
import io
import json
from pathlib import Path
import sys
import tempfile
import threading
import unittest
from unittest.mock import patch
sys.path.insert(0, str(Path(__file__).resolve().parents[2] / 'backend'))
from exam_backend import cli, runtime, providers, generate_exams as g


class UpgradeTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.state = patch.object(cli, 'STATE', cli.State()); self.state.start()
        self.registry = patch.object(cli, 'settings_path', return_value=self.root / 'settings.json'); self.registry.start()
        self.logs = patch.object(cli, 'fallback_log_dir', return_value=self.root / 'logs'); self.logs.start()
    def tearDown(self):
        self.logs.stop(); self.registry.stop(); self.state.stop(); self.temp.cleanup()
    def source(self):
        source = self.root / 'source'; source.mkdir()
        (source / 'lecture.txt').write_text('Learning connects concepts with evidence. Repetition improves retrieval. We compare the two ideas.', encoding='utf-8')
        return source
    def fake_complete(self, *args, **kwargs):
        return providers.ProviderResult(json.dumps({'multiple_choice': [{'question': 'Welche Aussage gilt?', 'explanation': 'Die Quelle erklärt Lernen.', 'options': [{'text': str(i), 'is_correct': i == 0} for i in range(4)]}], 'open_ended': [{'question': 'Erkläre Lernen.', 'expected_answer': 'Begriffe mit Belegen verbinden.', 'key_concepts': ['Lernen']}]}), {}, 'stop')
    def run_job(self, source):
        job = cli.Job(id='job-' + str(len(cli.STATE.jobs)), kind='all')
        cli.STATE.jobs[job.id] = job
        cli.run_generation(job, {'input_path': str(source), 'output_path': str(self.root / 'out'), 'mode': 'all', 'profile_id': 'ollama', 'model': 'test'})
        return job
    def test_parent_output_alias_is_rejected(self):
        source = self.source()
        with self.assertRaises(ValueError): cli.ensure_separate_output(str(source), str(self.root))
    def test_reject_string_false(self):
        with self.assertRaises(ValueError): g.strict_boolean('false')
        self.assertFalse(g.strict_boolean(False))
    def test_complete_generate_reuse_and_changed_source(self):
        source = self.source()
        before = (source / 'lecture.txt').read_bytes()
        with patch.object(providers, 'complete', side_effect=self.fake_complete) as complete:
            first = self.run_job(source)
            self.assertEqual(first.status, 'done', first.error)
            self.assertEqual(len(first.result['generated']), 1)
            calls = complete.call_count
            second = self.run_job(source)
            self.assertEqual(second.status, 'done', second.error)
            self.assertEqual(len(second.result['reused']), 1)
            self.assertEqual(complete.call_count, calls)
            (source / 'lecture.txt').write_text('A changed lecture explains memory and learning through practice and retrieval.', encoding='utf-8')
            third = self.run_job(source)
            self.assertEqual(third.status, 'done', third.error)
            self.assertEqual(len(third.result['generated']), 1)
            self.assertEqual(len(list(g.discover_generated_exams(Path(third.result['project_root'])).values())[0]), 1)
        self.assertTrue(before)
        self.assertEqual(list(source.glob('exams')), [])
    def test_cancel_during_provider_does_not_publish(self):
        source = self.source()
        job = cli.Job(id='cancel', kind='all')
        def cancel(*args, **kwargs):
            cli.request_job_cancel(job)
            return self.fake_complete()
        with patch.object(providers, 'complete', side_effect=cancel):
            cli.run_generation(job, {'input_path': str(source), 'output_path': str(self.root / 'out'), 'mode': 'all', 'profile_id': 'ollama', 'model': 'test'})
        self.assertEqual(job.status, 'canceled')
        self.assertEqual(list((self.root / 'out').glob('*/exam_index.html')), [])
    def test_failed_directory_replacement_restores_previous_output(self):
        dest, staging = self.root / 'exam', self.root / 'staging'
        dest.mkdir(); staging.mkdir(); (dest / 'value').write_text('old'); (staging / 'value').write_text('new')
        original = runtime.os.replace
        def replace(a, b):
            if Path(a) == staging: raise OSError('disk failure')
            return original(a, b)
        with patch.object(runtime.os, 'replace', side_effect=replace):
            with self.assertRaises(OSError): runtime.replace_directory(staging, dest)
        self.assertEqual((dest / 'value').read_text(), 'old')
    def test_request_body_limit_and_object_validation(self):
        for length, body in [('-1', b''), ('1048577', b''), ('2', b'[]')]:
            handler = type('Request', (), {'headers': {'Content-Length': length}, 'rfile': io.BytesIO(body)})()
            with self.assertRaises(ValueError): cli.read_json(handler)
    def test_plain_logs_do_not_include_source_diagnostics(self):
        job = cli.Job(id='log', kind='all')
        cli.job_debug(job, 'source', 'PRIVATE SOURCE')
        cli.job_log(job, 'Authorization: Bearer secret-test')
        text = cli.job_log_text(job)
        self.assertNotIn('PRIVATE SOURCE', text)
        self.assertNotIn('secret-test', text)

if __name__ == '__main__': unittest.main()
