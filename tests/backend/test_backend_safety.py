from pathlib import Path
from types import SimpleNamespace
import sys
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "backend"))

from exam_backend import cli, local_server  # noqa: E402
from exam_backend import generate_exams  # noqa: E402
from exam_backend.cli import Job, backup_existing_project, ensure_separate_output, final_args, generator_args, job_log, materialize_input, scan_folder  # noqa: E402


class BackendSafetyTests(unittest.TestCase):
    def test_materialize_copies_pdfs_without_touching_input(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            tmp_path = Path(temp)
            source = tmp_path / "source"
            course = source / "Course A"
            course.mkdir(parents=True)
            pdf = course / "lecture.pdf"
            pdf.write_bytes(b"%PDF-1.4\n")
            (course / "exams").mkdir()
            ignored = course / "exams" / "old.pdf"
            ignored.write_bytes(b"%PDF-1.4\n")

            out = tmp_path / "out"
            project = materialize_input(str(source), str(out))

            self.assertTrue(pdf.exists())
            self.assertTrue((project / "Course A" / "lecture.pdf").exists())
            self.assertFalse((project / "Course A" / "exams" / "old.pdf").exists())

    def test_scan_ignores_existing_exams(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp) / "root"
            (root / "Course" / "exams").mkdir(parents=True)
            (root / "Course" / "a.pdf").write_bytes(b"%PDF-1.4\n")
            (root / "Course" / "exams" / "b.pdf").write_bytes(b"%PDF-1.4\n")

            result = scan_folder(str(root))

            self.assertEqual(result["pdf_count"], 1)
            self.assertEqual(result["courses"], {"Course": 1})

    def test_output_inside_input_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp) / "input"
            output = root / "generated"
            root.mkdir()

            with self.assertRaises(ValueError):
                ensure_separate_output(str(root), str(output))

    def test_overwrite_backup_copies_existing_project(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            tmp_path = Path(temp)
            source = tmp_path / "source"
            source.mkdir()
            output = tmp_path / "output"
            existing_project = output / "source"
            existing_project.mkdir(parents=True)
            (existing_project / "exam_index.html").write_text("old", encoding="utf-8")

            backup = backup_existing_project(str(source), str(output), overwrite=True)

            self.assertIsNotNone(backup)
            assert backup is not None
            self.assertEqual((backup / "exam_index.html").read_text(encoding="utf-8"), "old")

    def test_retry_counts_can_shrink_below_initial_minimum(self) -> None:
        args = SimpleNamespace(min_mc=40, max_mc=60, min_open=10, max_open=20)

        mc, open_count = generate_exams.scaled_retry_counts(40, 10, 2, args)

        self.assertLess(mc, 40)
        self.assertLess(open_count, 10)

    def test_normalize_accepts_common_question_key_aliases(self) -> None:
        model_exam = {
            "mc_questions": [
                {
                    "question": "Welche Aussagen stimmen?",
                    "options": [
                        {"text": "A", "is_correct": True},
                        {"text": "B", "is_correct": False},
                        {"text": "C", "is_correct": False},
                        {"text": "D", "is_correct": True},
                    ],
                    "explanation": "A und D sind durch die Quelle gestützt.",
                }
            ],
            "open_questions": [
                {
                    "question": "Erkläre den zentralen Befund.",
                    "expected_answer": "Der zentrale Befund wird erklärt.",
                    "key_concepts": ["Befund"],
                    "grading_rubric": {"90-100": "Vollständig."},
                }
            ],
        }

        exam = generate_exams.normalize_exam(model_exam, "Course", "source.pdf", None, 2000)

        self.assertEqual(len(exam["multiple_choice"]), 1)
        self.assertEqual(len(exam["open_ended"]), 1)

    def test_desktop_example_uses_smaller_ai_request_and_fallback(self) -> None:
        args = generator_args(Path("/tmp/example"), example=True, model="llama3.1:8b")

        self.assertEqual((args.min_mc, args.max_mc), (12, 20))
        self.assertEqual((args.min_open, args.max_open), (4, 8))
        self.assertTrue(args.allow_heuristic_fallback)
        self.assertEqual(args.coverage_mode, "representative")
        self.assertEqual(args.model, "llama3.1:8b")

    def test_desktop_all_uses_auto_coverage(self) -> None:
        args = generator_args(Path("/tmp/example"), example=False, model="qwen2.5:14b")

        self.assertEqual(args.coverage_mode, "auto")
        self.assertEqual(args.model, "qwen2.5:14b")

    def test_final_args_uses_selected_model(self) -> None:
        args = final_args(Path("/tmp/example"), model="mistral-small:latest")

        self.assertEqual(args.model, "mistral-small:latest")

    def test_check_dependencies_returns_available_models(self) -> None:
        class FakeResponse:
            def __enter__(self) -> "FakeResponse":
                return self

            def __exit__(self, *args: object) -> None:
                return None

            def read(self) -> bytes:
                return b"Ollama is running"

        original_urlopen = cli.request.urlopen
        original_ollama_json = cli.ollama_json

        try:
            cli.request.urlopen = lambda *args, **kwargs: FakeResponse()  # type: ignore[assignment]
            cli.ollama_json = lambda *args, **kwargs: {"models": [{"name": "gemma4:31b-cloud"}, {"name": "qwen2.5:14b"}]}  # type: ignore[assignment]
            with tempfile.TemporaryDirectory() as temp:
                result = cli.check_dependencies(temp, "qwen2.5:14b")
        finally:
            cli.request.urlopen = original_urlopen  # type: ignore[assignment]
            cli.ollama_json = original_ollama_json  # type: ignore[assignment]

        self.assertEqual(result["available_models"], ["gemma4:31b-cloud", "qwen2.5:14b"])
        self.assertEqual(result["default_model"], "gemma4:31b-cloud")
        self.assertTrue(next(item for item in result["checks"] if item["id"] == "model")["ok"])

    def test_check_dependencies_reports_missing_model_guidance(self) -> None:
        class FakeResponse:
            def __enter__(self) -> "FakeResponse":
                return self

            def __exit__(self, *args: object) -> None:
                return None

            def read(self) -> bytes:
                return b"Ollama is running"

        original_urlopen = cli.request.urlopen
        original_ollama_json = cli.ollama_json

        try:
            cli.request.urlopen = lambda *args, **kwargs: FakeResponse()  # type: ignore[assignment]
            cli.ollama_json = lambda *args, **kwargs: {"models": [{"name": "gemma4:31b-cloud"}]}  # type: ignore[assignment]
            with tempfile.TemporaryDirectory() as temp:
                result = cli.check_dependencies(temp, "missing:model")
        finally:
            cli.request.urlopen = original_urlopen  # type: ignore[assignment]
            cli.ollama_json = original_ollama_json  # type: ignore[assignment]

        model_check = next(item for item in result["checks"] if item["id"] == "model")
        self.assertFalse(model_check["ok"])
        self.assertIn("ollama pull missing:model", model_check["detail"])

    def test_test_model_success_updates_selected_model(self) -> None:
        original_ollama_json = cli.ollama_json
        original_model = cli.STATE.selected_model

        try:
            cli.ollama_json = lambda *args, **kwargs: {"message": {"content": "OK"}}  # type: ignore[assignment]
            result = cli.test_model("qwen2.5:14b")
        finally:
            cli.ollama_json = original_ollama_json  # type: ignore[assignment]
            cli.STATE.selected_model = original_model

        self.assertTrue(result["ok"])
        self.assertEqual(result["model"], "qwen2.5:14b")

    def test_test_model_handles_malformed_response(self) -> None:
        original_ollama_json = cli.ollama_json

        try:
            cli.ollama_json = lambda *args, **kwargs: {"message": {"content": ""}}  # type: ignore[assignment]
            result = cli.test_model("qwen2.5:14b")
        finally:
            cli.ollama_json = original_ollama_json  # type: ignore[assignment]

        self.assertFalse(result["ok"])
        self.assertIn("no message", result["detail"])

    def test_grading_prompt_is_austrian_tutor_not_flirty(self) -> None:
        prompt = local_server.TUTOR_EVALUATOR_SYSTEM_PROMPT

        self.assertIn("österreich", prompt.casefold())
        self.assertIn("Schmäh", prompt)
        self.assertIn("Schimpfwörter", prompt)
        forbidden = ["flirtend", "anzüglich", "sexualisiert", "romantisch", "lusterregend"]
        for word in forbidden:
            self.assertNotIn(word, prompt.casefold())

    def test_chunk_text_keeps_all_content(self) -> None:
        text = "alpha " * 900 + "\n\n" + "beta " * 900 + "\n\n" + "gamma " * 900

        chunks = generate_exams.chunk_text(text, max_chars=3000)

        self.assertGreater(len(chunks), 1)
        self.assertIn("alpha", "\n".join(chunks))
        self.assertIn("beta", "\n".join(chunks))
        self.assertIn("gamma", "\n".join(chunks))

    def test_auto_coverage_mode_uses_full_coverage_for_long_text(self) -> None:
        args = SimpleNamespace(coverage_mode="auto")

        self.assertEqual(generate_exams.resolve_coverage_mode("short", args), "representative")
        self.assertEqual(generate_exams.resolve_coverage_mode("x" * (generate_exams.MAX_PROMPT_TEXT_CHARS + 1), args), "full_coverage")

    def test_full_coverage_generation_processes_every_chunk(self) -> None:
        args = SimpleNamespace(
            min_mc=2,
            max_mc=2,
            min_open=2,
            max_open=2,
            endpoint="http://example.invalid",
            model="fake",
            timeout=1,
            retries=0,
            coverage_mode="full_coverage",
            allow_heuristic_fallback=False,
        )
        text = "alpha " * 3000 + "\n\n" + "beta " * 3000
        calls = {"questions": 0}
        original_post_ollama = generate_exams.post_ollama

        def fake_post_ollama(endpoint: str, model: str, prompt: str, timeout: int) -> str:
            if "Create compact coverage notes" in prompt:
                return '{"coverage_notes":[{"topic":"Thema","exam_targets":["Ziel"],"common_traps":["Falle"],"source_area":"Chunk"}]}'
            calls["questions"] += 1
            number = calls["questions"]
            return f"""{{
              "multiple_choice": [{{
                "topic": "Thema {number}",
                "question": "Welche Aussagen stimmen zu Chunk {number}?",
                "options": [
                  {{"text": "A {number}", "is_correct": true}},
                  {{"text": "B {number}", "is_correct": false}},
                  {{"text": "C {number}", "is_correct": false}},
                  {{"text": "D {number}", "is_correct": true}}
                ],
                "explanation": "A und D sind für Chunk {number} richtig."
              }}],
              "open_ended": [{{
                "question": "Erkläre Chunk {number}.",
                "expected_answer": "Chunk {number} wird konzeptuell erklärt.",
                "key_concepts": ["Konzept {number}"],
                "grading_rubric": {{"90-100": "Vollständig."}},
                "max_score": 100
              }}]
            }}"""

        try:
            generate_exams.post_ollama = fake_post_ollama
            exam = generate_exams.generate_full_coverage_exam(
                "Course",
                "source.pdf",
                text,
                None,
                generate_exams.count_words(text),
                args,
            )
        finally:
            generate_exams.post_ollama = original_post_ollama

        metadata = exam["metadata"]
        self.assertEqual(metadata["coverage_mode"], "full_coverage")
        self.assertEqual(metadata["source_chunk_count"], 2)
        self.assertEqual(metadata["processed_chunk_count"], 2)
        self.assertEqual(metadata["failed_chunk_count"], 0)
        self.assertIsNone(metadata["coverage_warning"])
        self.assertEqual(len(exam["multiple_choice"]), 2)
        self.assertEqual(len(exam["open_ended"]), 2)

    def test_full_coverage_records_partial_chunk_failure(self) -> None:
        args = SimpleNamespace(
            min_mc=1,
            max_mc=3,
            min_open=1,
            max_open=3,
            endpoint="http://example.invalid",
            model="fake",
            timeout=1,
            retries=0,
            coverage_mode="full_coverage",
            allow_heuristic_fallback=False,
        )
        text = "alpha " * 3000 + "\n\n" + "beta " * 3000 + "\n\n" + "gamma " * 3000
        calls = {"coverage": 0, "questions": 0}
        original_post_ollama = generate_exams.post_ollama

        def fake_post_ollama(endpoint: str, model: str, prompt: str, timeout: int) -> str:
            if "Create compact coverage notes" in prompt:
                calls["coverage"] += 1
                if calls["coverage"] == 1:
                    return "not json"
                return '{"coverage_notes":[{"topic":"Thema","exam_targets":["Ziel"],"common_traps":["Falle"],"source_area":"Chunk"}]}'
            calls["questions"] += 1
            number = calls["questions"]
            return f"""{{
              "multiple_choice": [{{
                "topic": "Thema {number}",
                "question": "Welche Aussagen stimmen nach Ausfalltest {number}?",
                "options": [
                  {{"text": "A {number}", "is_correct": true}},
                  {{"text": "B {number}", "is_correct": false}},
                  {{"text": "C {number}", "is_correct": false}},
                  {{"text": "D {number}", "is_correct": true}}
                ],
                "explanation": "A und D sind richtig."
              }}],
              "open_ended": [{{
                "question": "Erkläre Ausfalltest {number}.",
                "expected_answer": "Der Chunk wird erklärt.",
                "key_concepts": ["Konzept"],
                "grading_rubric": {{"90-100": "Vollständig."}},
                "max_score": 100
              }}]
            }}"""

        try:
            generate_exams.post_ollama = fake_post_ollama
            exam = generate_exams.generate_full_coverage_exam(
                "Course",
                "source.pdf",
                text,
                None,
                generate_exams.count_words(text),
                args,
            )
        finally:
            generate_exams.post_ollama = original_post_ollama

        metadata = exam["metadata"]
        self.assertEqual(metadata["source_chunk_count"], 3)
        self.assertEqual(metadata["processed_chunk_count"], 2)
        self.assertEqual(metadata["failed_chunk_count"], 1)
        self.assertIn("Full coverage processed 2/3 chunks", metadata["coverage_warning"])

    def test_normalize_mc_items_deduplicates_question_text(self) -> None:
        raw = [
            {
                "question": "Welche Aussage stimmt?",
                "options": [
                    {"text": "A", "is_correct": True},
                    {"text": "B", "is_correct": False},
                    {"text": "C", "is_correct": False},
                    {"text": "D", "is_correct": True},
                ],
                "explanation": "A und D.",
            },
            {
                "question": "Welche Aussage stimmt?",
                "options": [
                    {"text": "A", "is_correct": True},
                    {"text": "B", "is_correct": False},
                    {"text": "C", "is_correct": False},
                    {"text": "D", "is_correct": True},
                ],
                "explanation": "A und D.",
            },
        ]

        questions = generate_exams.normalize_mc_items(raw, set())

        self.assertEqual(len(questions), 1)

    def test_job_log_updates_activity_timestamp(self) -> None:
        job = Job(id="test", kind="example")
        before = job.updated_at

        job_log(job, "Waiting for Ollama")

        self.assertGreaterEqual(job.updated_at, before)
        self.assertTrue(job.logs[-1].endswith("Waiting for Ollama"))


if __name__ == "__main__":
    unittest.main()
