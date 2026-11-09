import json
from pathlib import Path
import sys
import threading
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / 'backend'))
from exam_backend import model_checks as checks, providers, runtime


class ModelChecksTests(unittest.TestCase):
    profile = {'id': 'ollama', 'kind': 'ollama', 'base_url': 'http://localhost:11434'}

    def metadata(self, base, path, payload=None):
        return {
            '/api/show': {'model_info': {'test.context_length': 4096}, 'details': {'quantization_level': 'Q4'}},
            '/api/ps': {'models': [{'name': 'test', 'digest': 'abc', 'context_length': 2048}]},
            '/api/tags': {'models': [{'name': 'test', 'digest': 'abc'}]},
            '/api/version': {'version': 'test'},
        }[path]

    def test_local_identity_includes_model_digest_context_and_hardware(self):
        with patch.object(checks, '_metadata', side_effect=self.metadata), patch.object(checks, '_hardware', return_value={'cpu': 'test', 'memory_bytes': 123}):
            result = checks.probe(self.profile, 'test')
        self.assertEqual(result['location'], 'local')
        self.assertEqual(result['context_length'], 2048)
        self.assertEqual(result['maximum_context_length'], 4096)
        self.assertEqual(result['hardware']['cpu'], 'test')
        self.assertEqual(result['quantization'], 'Q4')

    def test_loopback_is_not_proof_of_local_inference(self):
        with patch.object(checks, '_metadata', side_effect=OSError()), patch.object(checks, '_hardware') as hardware:
            result = checks.probe(self.profile, 'test-cloud')
        self.assertEqual(result['location'], 'unknown')
        self.assertIsNone(result['context_length'])
        hardware.assert_not_called()

    def test_cloud_relay_overrides_explicit_local_claim(self):
        def metadata(base, path, payload=None):
            value = self.metadata(base, path, payload)
            return {**value, 'remote_host': 'https://ollama.com'} if path == '/api/show' else value
        with patch.object(checks, '_metadata', side_effect=metadata), patch.object(checks, '_hardware') as hardware:
            result = checks.probe(self.profile, 'test', 'local')
        self.assertEqual(result['location'], 'cloud')
        self.assertTrue(result['warnings'])
        hardware.assert_not_called()

    def test_remote_runtime_does_not_measure_this_macs_hardware(self):
        profile = {**self.profile, 'base_url': 'https://other-machine.example'}
        with patch.object(checks, '_metadata', side_effect=self.metadata), patch.object(checks, '_hardware') as hardware:
            result = checks.probe(profile, 'test', 'local')
        self.assertEqual(result['location'], 'remote')
        hardware.assert_not_called()

    def test_context_checks_include_output_and_unknown_metadata(self):
        self.assertFalse(checks.check_context({'context_length': 4096}, 12000, 1000)['ok'])
        self.assertTrue(checks.check_context({'context_length': 4096}, 3000, 1000)['ok'])
        self.assertIsNone(checks.check_context({}, 3000, 1000)['ok'])

    def response(self, kind):
        if kind == 'blueprint':
            return {'topics': ['Dauer', 'Vergleich', 'Grenzen']}
        if kind == 'mc':
            return {'multiple_choice': [
                {'id': key, 'question': f'Welche Angabe gilt bei Aufgabe {key}?', 'option_ids': [*values, next(option for option in checks.MC_OPTIONS if option not in values)], 'correct_ids': values, 'explanation': 'Die Quelle belegt diese Antwort.'}
                for key, values in checks.MC_KEYS.items()
            ]}
        return {'open_ended': [
            {'id': key, 'question': f'Erkläre die Aufgabe {key}.', 'answer_value': value, 'expected_answer': 'Das Ergebnis ergibt sich aus den angegebenen Quelldaten.'}
            for key, value in checks.OPEN_KEYS.items()
        ]}

    def provider(self, profile_id, model, messages, **kwargs):
        self.assertEqual(profile_id, 'ollama')
        self.assertEqual(model, 'test')
        self.assertIn('Deutsch', messages[0]['content'])
        self.assertEqual(kwargs['profile_snapshot'], self.profile)
        prompt = messages[-1]['content']
        kind = 'blueprint' if 'topics (Liste)' in prompt else ('mc' if 'multiple_choice' in prompt else 'open')
        self.assertEqual(kwargs['max_output_tokens'], 300 if kind == 'blueprint' else 2000)
        return providers.ProviderResult(json.dumps(self.response(kind)), {}, 'stop')

    def test_synthetic_sample_checks_known_answers_without_repair(self):
        with patch.object(providers, 'complete', side_effect=self.provider) as provider:
            result = checks.calibrate(self.profile, 'test', threading.Event(), {'model_identity': 'a', 'context_length': 8192})
        self.assertEqual(result['status'], 'sample_passed')
        self.assertEqual(result['sample_count'], 10)
        self.assertEqual(result['request_count'], 3)
        self.assertEqual(provider.call_count, 3)
        self.assertIsNone(next(c for c in result['checks'] if c['id'] == 'language_difficulty')['ok'])
        self.assertEqual(result['sample']['source'], checks.FIXTURE)
        self.assertTrue(result['sample']['mc']['multiple_choice'][0]['options'])

    def test_valid_json_does_not_establish_task_quality(self):
        with patch.object(providers, 'complete', return_value=providers.ProviderResult('{}', {}, 'stop')):
            result = checks.calibrate(self.profile, 'test', threading.Event(), {'model_identity': 'a'})
        self.assertEqual(result['status'], 'limited')
        self.assertFalse(next(c for c in result['checks'] if c['id'] == 'mc_keys')['ok'])
        self.assertEqual(result['sample_count'], 0)

    def test_incorrect_answer_and_hallucination_are_detected(self):
        mc, opened = self.response('mc'), self.response('open')
        mc['multiple_choice'][2]['correct_ids'] = ['20']
        opened['open_ended'][3]['answer_value'] = 100
        result = checks._evaluate(self.response('blueprint'), mc, opened)
        self.assertFalse(next(c for c in result if c['id'] == 'mc_keys')['ok'])
        self.assertFalse(next(c for c in result if c['id'] == 'open_facts')['ok'])

    def test_unknown_response_fields_and_bad_shapes_fail_safely(self):
        with patch.object(providers, 'complete', return_value=providers.ProviderResult('[]', {}, 'stop')):
            result = checks.calibrate(self.profile, 'test', threading.Event(), {'model_identity': 'a'})
        self.assertEqual(result['status'], 'unsuitable')
        self.assertEqual(result['request_count'], 1)

    def test_too_small_context_never_starts_inference(self):
        with patch.object(providers, 'complete') as provider:
            result = checks.calibrate(self.profile, 'test', threading.Event(), {'model_identity': 'a', 'context_length': 400})
        self.assertEqual(result['status'], 'unsuitable')
        provider.assert_not_called()

    def test_pre_canceled_sample_never_starts_inference(self):
        cancel = threading.Event(); cancel.set()
        with patch.object(providers, 'complete') as provider, self.assertRaises(runtime.Canceled):
            checks.calibrate(self.profile, 'test', cancel, {'model_identity': 'a'})
        provider.assert_not_called()

    def test_optional_comparison_runs_equal_work(self):
        with patch.object(providers, 'complete', side_effect=self.provider) as provider:
            result = checks.calibrate(self.profile, 'test', threading.Event(), {'model_identity': 'a'}, compare_concurrency=True)
        self.assertEqual(provider.call_count, 7)
        self.assertEqual([item['active_requests'] for item in result['concurrency_comparison']], [1, 2])
        self.assertTrue(all(item['valid'] for item in result['concurrency_comparison']))
    def test_local_comparison_stops_when_memory_reserve_is_very_low(self):
        with patch.object(providers, 'complete', side_effect=self.provider) as provider, patch.object(checks, '_available_memory_percent', return_value=3):
            result = checks.calibrate(self.profile, 'test', threading.Event(), {'model_identity': 'a', 'location': 'local'}, compare_concurrency=True)
        self.assertEqual(provider.call_count, 3)
        self.assertEqual(result['concurrency_comparison'], [])
        self.assertTrue(any(c['id'] == 'memory' for c in result['checks']))

    def test_overall_deadline_is_visible_to_active_provider(self):
        def provider(*args, **kwargs):
            self.assertIsInstance(kwargs['cancel_event'], checks._SampleCancellation)
            kwargs['cancel_event'].deadline = 0
            self.assertTrue(kwargs['cancel_event'].is_set())
            raise providers.GenerationCanceled()
        with patch.object(providers, 'complete', side_effect=provider):
            result = checks.calibrate(self.profile, 'test', threading.Event(), {'model_identity': 'a'})
        self.assertEqual(result['status'], 'limited')
        self.assertEqual(result['checks'][0]['id'], 'timeout')


if __name__ == '__main__':
    unittest.main()
