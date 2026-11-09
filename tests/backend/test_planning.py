import json
from pathlib import Path
import sys
import tempfile
import threading
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / 'backend'))
from exam_backend import cli, planning, providers, runtime


class PlanningTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.addCleanup(patch.stopall)
        patch.object(cli, 'STATE', cli.State()).start()
        patch.object(providers, 'settings_dir', return_value=self.root / 'settings').start()
        patch.object(cli, 'settings_path', return_value=self.root / 'registry.json').start()
        patch.object(cli, 'fallback_log_dir', return_value=self.root / 'logs').start()

    def payload(self, mode='finals'):
        source = self.root / 'source'
        for folder in ('M2', 'M2/Archiv', 'M3'):
            path = source / folder
            path.mkdir(parents=True, exist_ok=True)
            (path / 'lecture.txt').write_text('Lernen verbindet Begriffe mit Belegen. Übung fördert den Abruf.', encoding='utf-8')
        return {'input_path': str(source), 'output_path': str(self.root / 'out'), 'mode': mode, 'profile_id': 'deepseek', 'model': 'test', 'target_mode': 'manual', 'target_mc': 1, 'target_open': 1, 'profile_snapshot': {'id': 'deepseek', 'kind': 'deepseek', 'base_url': 'https://api.deepseek.com'}}

    def analyze(self, payload):
        job = cli.Job(id='analysis', kind='preflight')
        with patch.object(providers, 'complete', side_effect=AssertionError('Analysis must not perform inference')):
            cli.run_preflight(job, payload)
        self.assertEqual(job.status, 'done', job.error)
        return job.result

    def test_selection_grouping_counts_and_unknown_eta(self):
        payload = self.payload()
        plan = self.analyze(payload)
        self.assertEqual(plan['counts'], {'planned': 3, 'valid': 3, 'new': 3, 'reused': 0, 'unavailable': 0})
        self.assertEqual({e['course'] for e in plan['exams']}, {'M2', 'M2/Archiv', 'M3'})
        self.assertIsNone(plan['estimate']['low_seconds'])
        self.assertEqual(plan['recommendation']['exam_slots'], 2)
        self.assertEqual(plan['material']['totals']['documents'], 3)
        sources = cli.scan_documents(payload)['sources']
        payload['source_ids'] = [sources[0]['id'], sources[1]['id']]
        payload['course_assignments'] = {s['id']: 'Gemeinsam' for s in sources}
        self.assertEqual(self.analyze(payload)['counts']['planned'], 1)
        payload['mode'] = 'example'
        result = self.analyze(payload)
        self.assertEqual(result['material']['totals']['documents'], 1)

    def test_limits_are_strict_and_do_not_change_content_signature(self):
        payload = self.payload()
        initial = planning.content_settings(payload, cli.generation_options(payload))
        for value in (True, '2', 0, 5, -1):
            with self.subTest(value=value), self.assertRaises(ValueError):
                planning.request_limits(dict(payload, max_active_requests=value))
        updated = planning.content_settings(dict(payload, max_parallel_exams=1, max_active_requests=1), cli.generation_options(payload))
        self.assertEqual(initial, updated)

    def test_dependency_estimate_uses_request_budget_and_keeps_uneven_jobs(self):
        exams = [{'key': 'a', 'available': True, 'reused': False, 'phases': [
            {'id': 'bp', 'phase': 'blueprint', 'deps': [], 'prompt_chars': 300, 'output_tokens': 10},
            {'id': 'mc', 'phase': 'mc', 'deps': ['bp'], 'prompt_chars': 300, 'output_tokens': 10},
            {'id': 'open', 'phase': 'open', 'deps': ['bp'], 'prompt_chars': 300, 'output_tokens': 10}]}]
        history = [{'phase': phase, 'seconds': 10, 'prompt_chars': 300, 'output_chars': 30, 'status': 'response'} for phase in ('blueprint', 'mc', 'open')]
        sequential = planning.estimate(exams, history, 1, 1)
        parallel = planning.estimate(exams, history, 2, 2)
        self.assertGreater(sequential['high_seconds'], parallel['high_seconds'])
        self.assertEqual(parallel['sample_count'], 3)

    def test_thinking_default_and_overrides_isolate_results_metrics_and_calibration(self):
        payload = self.payload()
        options = cli.generation_options(payload)
        self.assertEqual(planning.content_settings(payload, options)['reasoning_effort'], 'low')
        execution = {'model_identity': 'model'}
        paths, keys, signatures = set(), set(), set()
        sources = cli.scan_documents(payload)['sources']
        for effort in ('off', 'low', 'high', 'max'):
            selected = dict(payload, reasoning_effort=effort)
            signatures.add(planning.input_signature(selected, sources, options))
            keys.add(planning.metrics_key(selected, execution, 1))
            paths.add(cli.calibration_path(selected, execution))
        self.assertEqual((len(signatures), len(keys), len(paths)), (4, 4, 4))
        for value in (None, True, 'medium', 1, []):
            with self.subTest(value=value), self.assertRaises(ValueError):
                cli.generation_options(dict(payload, reasoning_effort=value))

    def test_analysis_signature_rejects_changed_selection(self):
        payload = self.payload()
        plan = self.analyze(payload)
        changed = dict(payload, language='en', analysis_id=plan['analysis_id'])
        job = cli.Job(id='stale', kind='finals')
        with patch.object(providers, 'complete', side_effect=AssertionError('Stale plans cannot infer')):
            cli.run_generation(job, changed)
        self.assertEqual(job.status, 'error')
        self.assertIn('erneut', job.error)

    def test_request_limit_is_shared_and_cancelable(self):
        event = threading.Event()
        entered = threading.Event()
        release = threading.Event()
        peak = active = 0
        lock = threading.Lock()
        def complete(*args, **kwargs):
            nonlocal peak, active
            with lock:
                active += 1
                peak = max(active, peak)
            entered.set()
            release.wait(2)
            with lock:
                active -= 1
            return providers.ProviderResult('{}', {}, 'stop')
        import concurrent.futures
        import contextvars
        with patch.object(providers, 'complete', side_effect=complete), runtime.generation_context(profile_id='test', model='test', cancel_event=event, request_limit=1):
            with concurrent.futures.ThreadPoolExecutor(max_workers=2) as pool:
                one = pool.submit(contextvars.copy_context().run, runtime.complete, [{'role': 'system', 'content': 'Test'}], 3)
                self.assertTrue(entered.wait(2))
                two = pool.submit(contextvars.copy_context().run, runtime.complete, [{'role': 'system', 'content': 'Test'}], 3)
                event.set()
                with self.assertRaises(runtime.Canceled):
                    two.result(timeout=2)
                release.set()
                with self.assertRaises(runtime.Canceled):
                    one.result(timeout=2)
        self.assertEqual(peak, 1)

    def test_small_final_does_not_assume_a_maximum_size_blueprint(self):
        args = cli.final_args(self.root)
        nodes = planning.phases('Eine kurze Quelle über Lernen.', 'finals', 1, 1, args)
        self.assertTrue(all(n['prompt_chars'] < 6000 for n in nodes))

    def test_prompt_prefix_is_stable_and_all_evidence_is_preserved(self):
        from exam_backend import generate_final_exams as finals
        for builder in (finals.mc_prompt, finals.open_prompt):
            one = builder('Course', {'topics': ['Lernen']}, 'UNIQUE EVIDENCE', 5, 1, [])
            two = builder('Course', {'topics': ['Lernen']}, 'UNIQUE EVIDENCE', 3, 2, ['Already asked'])
            self.assertEqual(one.split('Generate')[0], two.split('Generate')[0])
            self.assertIn('UNIQUE EVIDENCE', one)
            self.assertIn('Already asked', two)
            self.assertIn('Generate 3', two)

    def test_calibration_improves_provisional_estimate_and_local_recommendation(self):
        benchmark = {'status': 'sample_passed', 'checks': [{'ok': True}], 'observations': [{'phase': 'calibration_' + phase, 'duration_seconds': 5, 'prompt_chars': 300, 'response_chars': 120, 'concurrency': 1} for phase in ('blueprint', 'mc', 'open')], 'concurrency_comparison': [{'active_requests': 1, 'valid': True, 'duration_seconds': 10}, {'active_requests': 2, 'valid': True, 'duration_seconds': 6}]}
        records = planning.calibration_measurements(benchmark, 4)
        args = cli.final_args(self.root)
        exams = [{'key': 'x', 'available': True, 'phases': planning.phases('Kurz', 'finals', 1, 1, args)}]
        result = planning.estimate(exams, records, 2, 4)
        self.assertIsNotNone(result['high_seconds'])
        self.assertIn('synthetisch', result['basis'])
        self.assertEqual(planning.recommendation({'location': 'local'}, benchmark)['request_slots'], 2)

    def test_calibration_scope_does_not_establish_english_or_challenging_quality(self):
        execution = {'model_identity': 'fixed'}
        path = cli.calibration_path({}, execution)
        runtime.atomic_json(path, {'language': 'de', 'difficulty': 'balanced', 'status': 'sample_passed', 'checks': []})
        loaded = cli.load_calibration({'language': 'en', 'difficulty': 'challenging'}, execution)
        self.assertEqual(loaded['status'], 'limited')
        self.assertFalse(loaded['timing_applicable'])
        self.assertEqual(planning.calibration_measurements(loaded, 1), [])

    def test_usage_separates_cached_input_reasoning_and_visible_output(self):
        result = runtime.usage_summary([{'prompt_tokens': 100, 'prompt_cache_hit_tokens': 70, 'completion_tokens': 80, 'completion_tokens_details': {'reasoning_tokens': 50}}])
        self.assertEqual(result, {'input_tokens': 100, 'output_tokens': 80, 'reasoning_tokens': 50, 'cached_input_tokens': 70})
        self.assertTrue(all(value is None for value in runtime.usage_summary([{}, None, []]).values()))
        self.assertEqual(runtime.usage_summary([{'input_tokens': 50, 'input_tokens_details': {'cached_tokens': 25}, 'output_tokens': 20, 'output_tokens_details': {'reasoning_tokens': 5}}]), {'input_tokens': 50, 'output_tokens': 20, 'reasoning_tokens': 5, 'cached_input_tokens': 25})


if __name__ == '__main__':
    unittest.main()
