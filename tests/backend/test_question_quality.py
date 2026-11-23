"""Regression cases from the November BGF exam feedback; no provider needed."""
import copy
import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / 'backend'))
from exam_backend import question_quality as quality, runtime, generate_exams as g, generate_final_exams as finals, planning

SOURCE = '[SECTION 12 TEXT]\nBelastung bezeichnet äußere Einflüsse. Beanspruchung ist deren individuelle Auswirkung.\n\n[SECTION 13 TEXT]\nEine Kennzahl zeigt einen Befund. Die Diagnostik klärt Einflussfaktoren und Ansatzpunkte.'


def approved_response(prompt):
    catalogue = json.loads(prompt.split('SOURCE CATALOGUE:\n')[1].split('\nEXISTING ACCEPTED QUESTIONS:')[0])
    drafts = json.loads(prompt.split('DRAFT QUESTIONS:\n')[1])
    return {'reviews': [dict(index=q['index'], relevant=True, supported=True, self_contained=True,
                             unambiguous=True, non_duplicate=True, learning_objective='Belastung und Beanspruchung unterscheiden',
                             reason='Conceptual distinction supported by source',
                             evidence=[{'id': catalogue[0]['id'], 'quote': catalogue[0]['text'][:150]}]) for q in drafts]}


class QuestionQualityTests(unittest.TestCase):
    def question(self, stem='Wie unterscheiden sich Belastung und Beanspruchung?'):
        return {'question': stem, 'expected_answer': 'Äußere Einflüsse und individuelle Auswirkung unterscheiden.'}

    def review(self, questions, ask=approved_response, source=SOURCE):
        warnings = []
        quality.review_questions(questions, source, 'Vorlesung.docx', ask, warnings)
        return warnings

    def test_screenshot_detail_questions_are_excluded_even_if_reviewer_would_approve(self):
        stems = [
            'Welcher durchschnittliche Wert für die Veränderung des Absentismus ist in der Tabelle zur Meta-Evaluation angegeben?',
            'Welche Studie zeigt in der Tabelle die stärkste prozentuale Reduktion des Absentismus?',
            'Welche Zuordnungen von Studie und Veränderung des Absentismus stimmen mit der Tabelle überein?',
            'Welcher ROI-Bereich wird nach Uhle & Treier (2015) im Zusammenhang mit BGF/BGM genannt?',
            'Welche Aussage beschreibt das Studiendesign von Lang et al. (2026) korrekt?',
            'Welche Zuordnung von BGF-Phase und typischer Dauer ist korrekt?',
            'Wie viele Qualitätskriterien umfasst das QEP und wie werden sie zugeordnet?',
            'Was bedeutet die Reihenfolge der Studien von oben nach unten?',
        ]
        for stem in stems:
            with self.subTest(stem=stem):
                questions = [self.question(stem)]
                self.assertIn('incidental_detail_recall', self.review(questions)[0])
                self.assertEqual(questions, [])

    def test_good_concepts_calculation_and_interpretation_survive(self):
        for stem in ('Wie unterscheiden sich Belastung und Beanspruchung?',
                     '100 Beschäftigte haben zusammen 200 AU-Tage. Wie viele AU-Tage entfallen auf eine Person?',
                     'Ein ROI von 1:3 wird angegeben. Was bedeutet dieser Wert?',
                     'Warum folgt die Maßnahmenplanung auf die Analyse?',
                     'Welche Formel beschreibt AU-Tage je Fall?',
                     'Welche Bedeutung haben Struktur-, Prozess- und Ergebnisqualität?'):
            with self.subTest(stem=stem):
                self.assertIsNone(quality.detail_recall_reason(self.question(stem)))
        questions = [self.question()]
        self.review(questions)
        self.assertEqual(len(questions), 1)
        ref = questions[0]['_meta']['source_refs'][0]
        self.assertEqual(ref['locator'], {'kind': 'section', 'index': 12})
        self.assertEqual(ref['source_name'], 'Vorlesung.docx')
        self.assertIn(ref['quote'], SOURCE)

    def test_missing_rejected_or_malformed_review_never_passes(self):
        def alter(prompt, key, value):
            result = approved_response(prompt)
            result['reviews'][0][key] = value
            return result
        responses = [lambda p: {}, lambda p: {'reviews': None}]
        for key, value in [('supported', False), ('relevant', False), ('unambiguous', False),
                           ('self_contained', False), ('non_duplicate', False), ('supported', 'true'),
                           ('learning_objective', ''), ('evidence', []), ('index', True),
                           ('evidence', [{'id': 'invented', 'quote': 'A fabricated supporting quote.'}])]:
            responses.append(lambda p, k=key, v=value: alter(p, k, v))
        for ask in responses:
            questions = [self.question()]
            self.review(questions, ask)
            self.assertEqual(questions, [])

    def test_semantic_reviewer_rejects_absent_diagram_and_ambiguous_formula(self):
        for stem in ('Welche Aussagen zum Diagramm auf Folie 42 sind korrekt?',
                     'Welche Formel ist korrekt: (x/n)/365 oder (x/365)/n?'):
            questions = [self.question(stem)]
            def reject(prompt):
                result = approved_response(prompt)
                result['reviews'][0]['self_contained'] = False
                result['reviews'][0]['unambiguous'] = False
                return result
            self.review(questions, reject)
            self.assertEqual(questions, [])

    def test_verbatim_evidence_allows_only_unicode_and_whitespace_normalization(self):
        for quote, retained in [('Belastung bezeichnet äußere Einflüsse.', True),
                                ('Belastung  bezeichnet\n äußere Einflüsse.', True),
                                ('Belastung ist ein äußerer Einfluss.', False)]:
            def ask(prompt):
                result = approved_response(prompt)
                result['reviews'][0]['evidence'][0]['quote'] = quote
                return result
            questions = [self.question()]
            self.review(questions, ask)
            self.assertEqual(bool(questions), retained)

    def test_document_block_ids_disambiguate_duplicate_text_and_preserve_multiple_references(self):
        original = 'Belastung bezeichnet äußere Einflüsse.'
        docs = {name: {'id': name, 'name': name + '.docx', 'blocks': [{'text': original, 'locator': {'kind': 'section', 'index': 7}}]} for name in ('one', 'two')}
        with runtime.generation_context(documents=docs):
            catalogue = quality.source_catalogue(original, 'Course')
            self.assertEqual(len({row['id'] for row in catalogue}), 2)
            def ask(prompt):
                result = approved_response(prompt)
                result['reviews'][0]['evidence'].append({'id': catalogue[1]['id'], 'quote': original})
                return result
            questions = [self.question()]
            self.review(questions, ask, original)
            self.assertEqual({ref['source_id'] for ref in questions[0]['_meta']['source_refs']}, {'one', 'two'})
            unknown = [self.question()]
            self.review(unknown, source='This text cannot be located in the selected documents.')
            self.assertEqual(unknown, [])

    def test_review_failure_propagates_and_cannot_publish_unchecked_drafts(self):
        args = SimpleNamespace(endpoint='local', model='test', timeout=1, retries=0)
        with patch.object(g, 'call_json_with_retries', side_effect=RuntimeError('review unavailable')):
            with self.assertRaisesRegex(RuntimeError, 'review unavailable'):
                g.review_generated_questions([], [self.question()], SOURCE, 'lecture', args)

    def test_all_prompt_paths_share_policy_and_cache_signature_changes(self):
        prompts = [g.GENERATION_SYSTEM_PROMPT, finals.FINAL_SYSTEM_PROMPT,
                   finals.mc_prompt('C', {}, SOURCE, 1, 1, []), finals.open_prompt('C', {}, SOURCE, 1, 1, [])]
        self.assertTrue(all(quality.POLICY in p for p in prompts))
        self.assertNotIn('Generate exactly', g.build_merged_chunk_prompt('C', 'lecture', SOURCE, 1, 1, 1, 1))
        self.assertEqual(planning.content_settings({}, {})['quality_policy'], quality.VERSION)

    def test_generation_paths_use_review_transport_and_keep_previous_exam_on_rejection(self):
        from exam_backend import cli
        draft = {'multiple_choice': [{'question': 'Wie unterscheiden sich Belastung und Beanspruchung?',
                 'explanation': 'Belastung ist außen, Beanspruchung individuell.',
                 'options': [{'text': text, 'is_correct': index == 0} for index, text in enumerate(
                     ['Außen und individuell', 'Beides nur außen', 'Beides nur individuell', 'Beides identisch'])]}],
                 'open_ended': [{'question': 'Erläutere den Unterschied.', 'expected_answer': 'Äußere Einflüsse und individuelle Auswirkung.'}],
                 'coverage_notes': [{'topic': 'Belastung', 'exam_targets': ['Unterschied erklären']}], 'audit_hints': {}}
        for coverage in ('representative', 'full_coverage'):
            with self.subTest(coverage=coverage), tempfile.TemporaryDirectory() as directory:
                root = Path(directory)
                source = root / 'Course' / 'lecture.pdf'
                source.parent.mkdir()
                source.write_bytes(b'%PDF-test')
                args = cli.generator_args(root, example=True)
                args.overwrite, args.retries, args.allow_heuristic_fallback = True, 0, False
                args.coverage_mode, args.target_mode = coverage, 'manual'
                args.target_mc, args.target_open = 1, 1
                calls = []
                def complete(endpoint, model, prompt, timeout):
                    calls.append(prompt)
                    return json.dumps(approved_response(prompt) if prompt.startswith('QUALITY_REVIEW:') else draft)
                with patch.object(g, 'extract_pdf_text', return_value=(SOURCE, None, None)), patch.object(g, 'post_ollama', side_effect=complete):
                    result = g.write_exam_folder(source, root, args, Path('backend/exam_backend/templates'))
                path = Path(result['exam_dir']) / 'exam.json'
                published = path.read_bytes()
                exam = json.loads(published)
                self.assertTrue(any(p.startswith('QUALITY_REVIEW:') for p in calls))
                self.assertEqual(len(exam['multiple_choice']) + len(exam['open_ended']), 2)
                self.assertTrue(all(q['_meta']['quality_review']['approved'] and q['_meta']['source_refs'] for q in exam['multiple_choice'] + exam['open_ended']))
                def reject(endpoint, model, prompt, timeout):
                    return json.dumps({'reviews': []} if prompt.startswith('QUALITY_REVIEW:') else draft)
                with patch.object(g, 'extract_pdf_text', return_value=(SOURCE, None, None)), patch.object(g, 'post_ollama', side_effect=reject):
                    with self.assertRaises((RuntimeError, ValueError)):
                        g.write_exam_folder(source, root, args, Path('backend/exam_backend/templates'))
                self.assertEqual(path.read_bytes(), published)

    def test_final_mc_and_open_both_use_review_and_stop_at_source_limit(self):
        draft = {'multiple_choice': [{'question': 'Wie unterscheiden sich Belastung und Beanspruchung?',
                 'explanation': 'Äußere Einflüsse und individuelle Auswirkung.',
                 'options': [{'text': str(i), 'is_correct': i == 0} for i in range(4)]}],
                 'open_ended': [{'question': 'Erläutere den Unterschied.', 'expected_answer': 'Äußere Einflüsse und individuelle Auswirkung.'}]}
        args = SimpleNamespace(endpoint='local', model='test', timeout=1, retries=0, mc_batch_size=5, open_batch_size=5)
        for generate in (finals.generate_mc, finals.generate_open):
            calls = []
            def review(endpoint, model, prompt, timeout):
                calls.append(prompt)
                return json.dumps(approved_response(prompt))
            with patch.object(finals, 'post_ollama', return_value=json.dumps(draft)), patch.object(g, 'post_ollama', side_effect=review):
                questions = generate(args, 'Course', {}, [SOURCE], 5)
            self.assertEqual(len(questions), 1)
            self.assertEqual(len(calls), 1)
            self.assertTrue(questions[0]['_meta']['source_refs'])

    def test_duplicate_reviews_and_quote_mismatch_fail_closed(self):
        for mode in ('duplicate', 'wrong_quote'):
            def ask(prompt):
                result = approved_response(prompt)
                if mode == 'duplicate':
                    result['reviews'].append(copy.deepcopy(result['reviews'][0]))
                else:
                    result['reviews'][0]['evidence'][0]['quote'] = 'Invented material that is not in this source.'
                return result
            questions = [self.question()]
            self.review(questions, ask)
            self.assertEqual(questions, [])


if __name__ == '__main__':
    unittest.main()
