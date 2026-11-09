"""Cross-module regressions for the upgrade's data and output boundaries."""
import io
import json
import os
from pathlib import Path
import sys
import tempfile
import threading
import unittest
from unittest.mock import patch
sys.path.insert(0, str(Path(__file__).resolve().parents[2] / 'backend'))
from exam_backend import cli, runtime, providers, generate_exams as g, generate_final_exams as finals


class StateDirectoryTests(unittest.TestCase):
    def test_isolated_app_state_preserves_home_for_keychain(self):
        home = Path.home()
        with tempfile.TemporaryDirectory() as directory, patch.dict(os.environ, {'EXAM_GENERATOR_DATA_DIR': directory}):
            self.assertEqual(providers.settings_dir(), Path(directory).resolve())
            self.assertEqual(cli.metrics_path().parent, providers.settings_dir())
            self.assertEqual(cli.fallback_log_dir().parent, providers.settings_dir())
            self.assertEqual(Path.home(), home)


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
    def run_job(self, source, **settings):
        job = cli.Job(id='job-' + str(len(cli.STATE.jobs)), kind='all')
        cli.STATE.jobs[job.id] = job
        cli.run_generation(job, {'input_path': str(source), 'output_path': str(self.root / 'out'), 'mode': 'all', 'profile_id': 'ollama', 'model': 'test', **settings})
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
    def test_fresh_final_uses_selected_extractions_without_deck_exams(self):
        source = self.source()
        (source / 'second.md').write_text('A second lecture discusses attention and memory. Retrieval is useful for learning.', encoding='utf-8')
        def complete(profile_id, model, messages, **kwargs):
            prompt = messages[-1]['content']
            if 'coverage blueprint' in prompt:
                return providers.ProviderResult(json.dumps({'major_topics': [{'topic': 'Lernen', 'exam_targets': ['Lernen erklären']}]}), {}, 'stop')
            return self.fake_complete()
        job = cli.Job(id='fresh-final', kind='finals')
        with patch.object(providers, 'complete', side_effect=complete):
            cli.run_generation(job, {'input_path': str(source), 'output_path': str(self.root / 'out'), 'mode': 'finals', 'profile_id': 'ollama', 'model': 'test', 'max_parallel_exams': 2, 'max_active_requests': 4, 'target_mode': 'manual', 'target_mc': 1, 'target_open': 1})
        self.assertEqual(job.status, 'done', job.error)
        exams = list(Path(job.result['project_root']).glob('*/exams/*/exam.json'))
        self.assertEqual(len(exams), 1)
        exam = json.loads(exams[0].read_text())
        self.assertTrue(exam['metadata']['is_final_exam'])
        self.assertEqual(len(exam['metadata']['included_sources']), 2)
        self.assertEqual(len(exam['multiple_choice']), 1)
        self.assertEqual(len(exam['open_ended']), 1)
    def test_two_finals_overlap_with_both_question_types_and_preserve_context(self):
        source = self.root / 'source'
        for name in ('Alpha', 'Beta', 'Gamma'):
            folder = source / name
            folder.mkdir(parents=True)
            (folder / 'lecture.txt').write_text(f'{name} explains learning through practice and retrieval.', encoding='utf-8')
        barrier = threading.Barrier(4, timeout=5)
        lock = threading.Lock()
        active, peak = 0, 0
        started, calls = [], []
        original = finals.write_final_exam
        def write_final(args, course_dir, **kwargs):
            nonlocal active, peak
            with lock:
                active += 1
                peak = max(peak, active)
                started.append(args.course_label)
            try:
                return original(args, course_dir, **kwargs)
            finally:
                with lock:
                    active -= 1
        def complete(profile_id, model, messages, **kwargs):
            self.assertEqual((profile_id, model), ('test-profile', 'test-model'))
            self.assertEqual(kwargs['profile_snapshot'], {'id': 'test-profile'})
            self.assertIn('English', messages[0]['content'])
            prompt = messages[-1]['content']
            course = next(name for name in ('Alpha', 'Beta', 'Gamma') if f'Course: {name}' in prompt)
            self.assertIn(f'{course} explains learning', prompt)
            if 'coverage blueprint' not in prompt:
                with lock:
                    calls.append(course)
                if course != 'Gamma':
                    barrier.wait()
            return self.fake_complete()
        job = cli.Job(id='parallel-finals', kind='finals')
        payload = {'input_path': str(source), 'output_path': str(self.root / 'out'), 'mode': 'finals', 'profile_id': 'test-profile', 'model': 'test-model', 'profile_snapshot': {'id': 'test-profile'}, 'max_parallel_exams': 2, 'max_active_requests': 4, 'language': 'en', 'target_mode': 'manual', 'target_mc': 1, 'target_open': 1}
        with patch.object(finals, 'write_final_exam', side_effect=write_final), patch.object(providers, 'complete', side_effect=complete):
            cli.run_generation(job, payload)
        self.assertEqual(job.status, 'done', job.error)
        self.assertEqual(peak, 2)
        self.assertEqual(sorted(started[:2]), ['Alpha', 'Beta'])
        self.assertEqual(sorted(calls), ['Alpha', 'Alpha', 'Beta', 'Beta', 'Gamma', 'Gamma'])
        self.assertEqual(len(job.result['usage']), 9)
        exams = list(Path(job.result['project_root']).glob('*/exams/*/exam.json'))
        self.assertEqual(len(exams), 3)
        for path in exams:
            exam = json.loads(path.read_text())
            self.assertEqual(len(exam['multiple_choice']), 1)
            self.assertEqual(len(exam['open_ended']), 1)
            self.assertEqual(exam['metadata']['provider_id'], 'test-profile')
            self.assertIn(exam['metadata']['course'], (path.parent / 'source.txt').read_text())
        reused = cli.Job(id='reuse-finals', kind='finals')
        with patch.object(providers, 'complete', side_effect=AssertionError('Unchanged finals must not call the provider')) as provider:
            cli.run_generation(reused, payload)
        self.assertEqual(reused.status, 'done', reused.error)
        self.assertEqual(len(reused.result['reused']), 3)
        provider.assert_not_called()
    def test_final_provider_failure_cancels_other_question_type(self):
        source = self.source()
        opened = threading.Event()
        canceled = threading.Event()
        def complete(profile_id, model, messages, **kwargs):
            prompt = messages[-1]['content']
            if 'coverage blueprint' in prompt:
                return self.fake_complete()
            if 'open-ended questions' in prompt:
                opened.set()
                if kwargs['cancel_event'].wait(5):
                    canceled.set()
                    raise providers.GenerationCanceled()
                self.fail('Other question type was not canceled after provider failure')
            self.assertTrue(opened.wait(5), 'Question types did not overlap')
            raise providers.ProviderError('balance', 'Limit erreicht')
        job = cli.Job(id='failed-final', kind='finals')
        with patch.object(providers, 'complete', side_effect=complete):
            cli.run_generation(job, {'input_path': str(source), 'output_path': str(self.root / 'out'), 'mode': 'finals', 'profile_id': 'ollama', 'model': 'test', 'max_parallel_exams': 2, 'max_active_requests': 4, 'target_mode': 'manual', 'target_mc': 1, 'target_open': 1})
        self.assertTrue(canceled.is_set())
        self.assertEqual(job.status, 'error', job.error)
        self.assertIn('Limit erreicht', cli.job_log_text(job))
        self.assertEqual(list((self.root / 'out').glob('*/exam_index.html')), [])
    def test_cancel_stops_both_finals_and_all_question_requests_without_publishing(self):
        source = self.root / 'source'
        for name in ('Alpha', 'Beta', 'Gamma'):
            folder = source / name
            folder.mkdir(parents=True)
            (folder / 'lecture.txt').write_text(f'{name} explains learning through practice.', encoding='utf-8')
        job = cli.Job(id='cancel-finals', kind='finals')
        barrier = threading.Barrier(4, action=lambda: cli.request_job_cancel(job), timeout=5)
        canceled = []
        def complete(profile_id, model, messages, **kwargs):
            prompt = messages[-1]['content']
            self.assertNotIn('Course: Gamma', prompt)
            if 'coverage blueprint' in prompt:
                return self.fake_complete()
            barrier.wait()
            self.assertTrue(kwargs['cancel_event'].wait(5), 'Active request did not receive cancellation')
            canceled.append(True)
            raise providers.GenerationCanceled()
        with patch.object(providers, 'complete', side_effect=complete):
            cli.run_generation(job, {'input_path': str(source), 'output_path': str(self.root / 'out'), 'mode': 'finals', 'profile_id': 'ollama', 'model': 'test', 'max_parallel_exams': 2, 'max_active_requests': 4, 'target_mode': 'manual', 'target_mc': 1, 'target_open': 1})
        self.assertEqual(job.status, 'canceled', job.error)
        self.assertEqual(len(canceled), 4)
        self.assertEqual(list((self.root / 'out').glob('*/exam_index.html')), [])
        self.assertEqual(list((self.root / 'out').glob('.exam-run-*')), [])
    def test_manual_open_only_and_mc_only(self):
        source = self.source()
        for mode in ('all', 'finals'):
            for mc, opened in [(0, 2), (2, 0)]:
                with self.subTest(mode=mode, mc=mc, opened=opened), patch.object(providers, 'complete', side_effect=self.fake_complete):
                    job = cli.Job(id=f'only-{mode}-{mc}', kind=mode)
                    cli.run_generation(job, {'input_path': str(source), 'output_path': str(self.root / f'out-{mode}-{mc}'), 'mode': mode, 'profile_id': 'ollama', 'model': 'test', 'target_mode': 'manual', 'target_mc': mc, 'target_open': opened})
                    self.assertEqual(job.status, 'done', job.error)
                    exam = json.loads(next(Path(job.result['project_root']).glob('*/exams/*/exam.json')).read_text())
                    self.assertEqual(bool(exam['multiple_choice']), bool(mc))
                    self.assertEqual(bool(exam['open_ended']), bool(opened))
    def test_terminal_provider_failure_keeps_completed_exam_as_partial(self):
        source = self.source()
        for name in ['second.txt', 'third.txt']:
            (source / name).write_text('Another lecture has useful evidence and explains practice and learning.', encoding='utf-8')
        failure_seen = threading.Event()
        original_log = cli.job_log
        def log(job, message):
            original_log(job, message)
            if 'KI-Verbindung fehlgeschlagen' in message:
                failure_seen.set()
        def complete(*args, **kwargs):
            # The two documents start together; keep the successful one in flight
            # until the coordinator has stopped scheduling after the other fails.
            prompt = args[2][-1]['content']
            if 'Another lecture' in prompt:
                raise providers.ProviderError('balance', 'Limit erreicht')
            self.assertTrue(failure_seen.wait(5))
            return self.fake_complete()
        with patch.object(providers, 'complete', side_effect=complete), patch.object(cli, 'job_log', side_effect=log):
            job = self.run_job(source, max_parallel_exams=2, max_active_requests=2)
        self.assertEqual(job.status, 'partial', job.error)
        self.assertEqual(len(job.result['generated']), 1)
        self.assertEqual(len(job.result['failed']), 1)
        self.assertEqual(len(job.result['skipped']), 1)
        self.assertEqual(len(list(Path(job.result['project_root']).glob('*/exams/*/exam.json'))), 1)
    def test_output_roots_do_not_repoint_existing_workspace(self):
        source = self.source()
        with patch.object(providers, 'complete', side_effect=self.fake_complete):
            first = self.run_job(source)
            second = cli.Job(id='other-output', kind='all')
            cli.run_generation(second, {'input_path': str(source), 'output_path': str(self.root / 'other-output'), 'mode': 'all', 'profile_id': 'ollama', 'model': 'test'})
        self.assertEqual(first.status, 'done', first.error)
        self.assertEqual(second.status, 'done', second.error)
        self.assertNotEqual(first.result['workspace_id'], second.result['workspace_id'])
        self.assertEqual(cli.STATE.workspaces[first.result['workspace_id']]['root'], first.result['project_root'])
        self.assertEqual(cli.STATE.workspaces[second.result['workspace_id']]['root'], second.result['project_root'])
    def test_registry_count_updates_and_corruption_is_not_overwritten(self):
        source = self.source()
        with patch.object(providers, 'complete', side_effect=self.fake_complete):
            first = self.run_job(source)
            (source / 'more.md').write_text('Another lecture explains useful concepts and practice.', encoding='utf-8')
            second = self.run_job(source)
        self.assertEqual(second.status, 'done', second.error)
        self.assertEqual(cli.load_registry()['workspaces'][first.result['workspace_id']]['exam_count'], 2)
        cli.settings_path().write_text('{broken', encoding='utf-8')
        with self.assertRaises(ValueError): cli.save_registry()
        self.assertEqual(cli.settings_path().read_text(), '{broken')
    def test_course_reassignment_has_one_current_exam_and_preserves_history(self):
        source = self.source()
        with patch.object(providers, 'complete', side_effect=self.fake_complete):
            first = self.run_job(source)
            source_id = cli.scan_documents({'input_path': str(source)})['sources'][0]['id']
            job = cli.Job(id='reassigned', kind='all')
            cli.run_generation(job, {'input_path': str(source), 'output_path': str(self.root / 'out'), 'mode': 'all', 'profile_id': 'ollama', 'model': 'test', 'course_assignments': {source_id: 'Neuer Kurs'}})
        self.assertEqual(job.status, 'done', job.error)
        workspace = Path(job.result['project_root'])
        exams = g.discover_generated_exams(workspace)
        self.assertEqual(sum(len(items) for items in exams.values()), 1)
        self.assertEqual(next(iter(exams.values()))[0]['course'], 'Neuer Kurs')
        self.assertEqual(len(list((workspace / '.history').glob('*/exam.json'))), 1)
    def test_generated_ids_are_unique_and_oversized_option_lists_are_not_truncated(self):
        raw = json.loads(self.fake_complete().text)
        raw['open_ended'] *= 2
        for question in raw['open_ended']: question['id'] = 'duplicate'
        exam = g.normalize_exam(raw, 'Course', 'lecture.txt', None, 100)
        self.assertEqual([q['id'] for q in exam['open_ended']], ['open-001', 'open-002'])
        raw['multiple_choice'][0]['options'].extend([{'text': f'extra-{i}', 'is_correct': True} for i in range(3)])
        self.assertEqual(g.normalize_mc_items(raw['multiple_choice'], set()), [])
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
