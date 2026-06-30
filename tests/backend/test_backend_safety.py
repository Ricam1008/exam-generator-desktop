from pathlib import Path
from types import SimpleNamespace
import io
import json
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
            original_metrics_path = cli.metrics_path
            cli.metrics_path = lambda: Path(temp) / "metrics.json"  # type: ignore[assignment]
            root = Path(temp) / "root"
            (root / "Course" / "exams").mkdir(parents=True)
            (root / "Course" / "a.pdf").write_bytes(b"%PDF-1.4\n")
            (root / "Course" / "exams" / "b.pdf").write_bytes(b"%PDF-1.4\n")

            try:
                result = scan_folder(str(root))
            finally:
                cli.metrics_path = original_metrics_path  # type: ignore[assignment]

            self.assertEqual(result["pdf_count"], 1)
            self.assertEqual(result["courses"], {"Course": 1})
            self.assertEqual(result["estimate"]["size_buckets"]["small"], 1)
            self.assertGreater(result["estimate"]["generate_all_minutes_high"], 0)
            self.assertIn("file size", result["estimate"]["note"])
            self.assertEqual(result["estimate"]["basis"]["generate_all"], "file-size heuristic")

    def test_scan_estimate_uses_history_for_selected_model(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            original_metrics_path = cli.metrics_path
            cli.metrics_path = lambda: Path(temp) / "metrics.json"  # type: ignore[assignment]
            root = Path(temp) / "root"
            (root / "Course").mkdir(parents=True)
            (root / "Course" / "a.pdf").write_bytes(b"x" * 1_000_000)

            try:
                cli.record_generation_metric("fast-model", "all", 500_000, 80_000, 600)
                result = scan_folder(str(root), "fast-model")
            finally:
                cli.metrics_path = original_metrics_path  # type: ignore[assignment]

            estimate = result["estimate"]
            self.assertEqual(estimate["basis"]["generate_all"], "previous runs with fast-model")
            self.assertEqual(estimate["history_runs_used"], 1)
            self.assertGreaterEqual(estimate["generate_all_minutes_high"], estimate["generate_all_minutes_low"])

    def test_scan_estimate_ignores_other_model_history(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            original_metrics_path = cli.metrics_path
            cli.metrics_path = lambda: Path(temp) / "metrics.json"  # type: ignore[assignment]
            root = Path(temp) / "root"
            (root / "Course").mkdir(parents=True)
            (root / "Course" / "a.pdf").write_bytes(b"x" * 1_000_000)

            try:
                cli.record_generation_metric("other-model", "all", 500_000, 80_000, 600)
                result = scan_folder(str(root), "selected-model")
            finally:
                cli.metrics_path = original_metrics_path  # type: ignore[assignment]

            self.assertEqual(result["estimate"]["basis"]["generate_all"], "file-size heuristic")

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

    def test_model_json_accepts_unescaped_formula_backslashes(self) -> None:
        raw = r'{"coverage_notes":[{"topic":"Kostenfunktion \Delta K","exam_targets":["Gewinn \pi = R-C"],"common_traps":["Zeile\nbleibt Escape"],"source_area":"S. 3"}]}'

        parsed = generate_exams.load_json_from_model(raw)

        note = parsed["coverage_notes"][0]
        self.assertEqual(note["topic"], "Kostenfunktion \\Delta K")
        self.assertEqual(note["exam_targets"], ["Gewinn \\pi = R-C"])
        self.assertEqual(note["common_traps"], ["Zeile\nbleibt Escape"])

    def test_model_json_repair_adds_audit_warning(self) -> None:
        warnings: list[str] = []

        parsed = generate_exams.load_json_from_model(
            r'{"coverage_notes":[{"topic":"Kostenfunktion \Delta K"}]}',
            context="repair test",
            audit_warnings=warnings,
        )

        self.assertEqual(parsed["coverage_notes"][0]["topic"], "Kostenfunktion \\Delta K")
        self.assertTrue(any("Parser repair applied for repair test" in warning for warning in warnings))

    def test_pdf_tables_are_formatted_as_structured_source_blocks(self) -> None:
        formatted = generate_exams.format_pdf_table(
            7,
            2,
            [
                ["Preis", "Nachfrage", "Angebot"],
                ["10", "80", "40"],
                ["20", "50", "70"],
            ],
        )

        self.assertIn("[TABLE page=7 index=2]", formatted)
        self.assertIn("Columns: Preis | Nachfrage | Angebot", formatted)
        self.assertIn("Row 1: Preis=10; Nachfrage=80; Angebot=40", formatted)
        self.assertIn("[/TABLE]", formatted)

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

    def test_audit_tracks_pages_chunks_question_metadata_and_summary(self) -> None:
        source_text = "\n\n".join(
            [
                "--- Page 1 ---\n[PAGE 1 TEXT]\nNachfragekurve und Tabelle.\n[TABLE page=1 index=1]\nColumns: Preis | Menge\nRow 1: Preis=10; Menge=20\n[/TABLE]",
                "--- Page 2 ---\n[PAGE 2 TEXT]\nNur Kontext ohne Frage.",
                "--- Page 3 ---\n[PAGE 3 TEXT]\nFormel: P = MC.",
            ]
        )
        exam = generate_exams.normalize_exam(
            {
                "multiple_choice": [
                    {
                        "topic": "Nachfrage",
                        "question": "Welche Aussage zur Tabelle stimmt?",
                        "options": [
                            {"text": "A", "is_correct": True},
                            {"text": "B", "is_correct": False},
                            {"text": "C", "is_correct": False},
                            {"text": "D", "is_correct": True},
                        ],
                        "explanation": "Die Tabelle stützt A und D.",
                    }
                ],
                "open_ended": [
                    {
                        "question": "Erkläre die Formel.",
                        "expected_answer": "P = MC wird erklärt.",
                        "key_concepts": ["Grenzkosten"],
                        "grading_rubric": {"90-100": "Vollständig."},
                    }
                ],
            },
            "Course",
            "source.pdf",
            None,
            120,
        )
        generate_exams.attach_question_metadata(exam["multiple_choice"], source_text.split("\n\n")[0], "chunk-001")
        generate_exams.attach_question_metadata(exam["open_ended"], source_text.split("\n\n")[2], "chunk-002")

        generate_exams.apply_exam_audit(exam, "source.pdf", source_text, 3, 2, 2, 0, ["validation warning"])

        audit = exam["audit"]
        self.assertEqual(audit["generator_version"], generate_exams.GENERATOR_VERSION)
        self.assertEqual(audit["pages_total"], 3)
        self.assertEqual(audit["pages_used"], [1, 3])
        self.assertEqual(audit["pages_without_questions"], [2])
        self.assertAlmostEqual(audit["coverage_ratio"], 2 / 3, places=3)
        self.assertEqual(audit["chunks_total"], 2)
        self.assertEqual(audit["chunks_processed"], 2)
        self.assertEqual(audit["question_distribution"]["multiple_choice"], 1)
        self.assertEqual(audit["question_distribution"]["open_ended"], 1)
        self.assertIn("validation warning", audit["warnings"])
        self.assertEqual(exam["multiple_choice"][0]["_meta"]["source_pages"], [1])
        self.assertEqual(exam["multiple_choice"][0]["_meta"]["chunk_id"], "chunk-001")

        summary = generate_exams.format_audit_summary(audit)
        self.assertIn("AUDIT SUMMARY", summary)
        self.assertIn("Pages total: 3", summary)
        self.assertIn("Pages used: 2", summary)
        self.assertIn("validation warning", summary)

    def test_write_exam_folder_writes_audit_json_and_log_summary(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            course = root / "Course"
            course.mkdir()
            pdf = course / "source.pdf"
            pdf.write_bytes(b"%PDF-1.4\n")
            args = SimpleNamespace(
                overwrite=True,
                min_mc=1,
                max_mc=1,
                min_open=1,
                max_open=1,
                endpoint="http://example.invalid",
                model="fake",
                timeout=1,
                retries=0,
                coverage_mode="representative",
                allow_heuristic_fallback=False,
            )
            source_text = "\n\n".join(
                [
                    "--- Page 1 ---\n[PAGE 1 TEXT]\nNachfragekurve und Tabelle.\n[TABLE page=1 index=1]\nColumns: Preis | Menge\nRow 1: Preis=10; Menge=20\n[/TABLE]",
                    "--- Page 2 ---\n[PAGE 2 TEXT]\nKontext ohne Frage.",
                ]
            )
            events: list[tuple[str, str]] = []
            original_extract_pdf_text = generate_exams.extract_pdf_text
            original_post_ollama = generate_exams.post_ollama

            def fake_extract_pdf_text(pdf_path: Path, debug=None):
                return source_text, None, 2

            def fake_post_ollama(endpoint: str, model: str, prompt: str, timeout: int) -> str:
                return """{
                  "multiple_choice": [{
                    "topic": "Nachfrage",
                    "question": "Welche Aussage zur Tabelle stimmt?",
                    "options": [
                      {"text": "A", "is_correct": true},
                      {"text": "B", "is_correct": false},
                      {"text": "C", "is_correct": false},
                      {"text": "D", "is_correct": true}
                    ],
                    "explanation": "A und D sind korrekt."
                  }],
                  "open_ended": [{
                    "question": "Erkläre die Nachfragekurve.",
                    "expected_answer": "Die Nachfragekurve wird erklärt.",
                    "key_concepts": ["Nachfrage"],
                    "grading_rubric": {"90-100": "Vollständig."},
                    "max_score": 100
                  }]
                }"""

            try:
                generate_exams.extract_pdf_text = fake_extract_pdf_text  # type: ignore[assignment]
                generate_exams.post_ollama = fake_post_ollama
                result = generate_exams.write_exam_folder(
                    pdf,
                    root,
                    args,
                    ROOT / "backend" / "exam_backend" / "templates",
                    debug=lambda title, content: events.append((title, content)),
                )
            finally:
                generate_exams.extract_pdf_text = original_extract_pdf_text  # type: ignore[assignment]
                generate_exams.post_ollama = original_post_ollama

            assert result is not None
            exam = json.loads((Path(result["exam_dir"]) / "exam.json").read_text(encoding="utf-8"))
            self.assertEqual(exam["audit"]["pages_total"], 2)
            self.assertEqual(exam["audit"]["pages_used"], [1, 2])
            self.assertEqual(exam["audit"]["chunks_processed"], 1)
            self.assertEqual(exam["multiple_choice"][0]["_meta"]["chunk_id"], "representative-001")
            self.assertTrue(any(title == "AUDIT SUMMARY: source.pdf" and "AUDIT SUMMARY" in content for title, content in events))

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
        text = "--- Page 1 ---\n[PAGE 1 TEXT]\n" + "alpha " * 3000 + "\n\n--- Page 2 ---\n[PAGE 2 TEXT]\n" + "beta " * 3000
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
                pages_total=2,
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
        self.assertEqual(exam["audit"]["pages_total"], 2)
        self.assertEqual(exam["audit"]["pages_used"], [1, 2])
        self.assertEqual(exam["audit"]["chunks_processed"], 2)
        self.assertEqual(exam["multiple_choice"][0]["_meta"]["chunk_id"], "chunk-001")

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
        self.assertEqual(exam["audit"]["chunks_failed"], 1)
        self.assertTrue(any("chunk-001 failed" in warning for warning in exam["audit"]["warnings"]))

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

    def test_call_json_with_retries_emits_prompt_and_raw_response_debug(self) -> None:
        args = SimpleNamespace(endpoint="http://example.invalid", model="fake", timeout=1, retries=0)
        events: list[tuple[str, str]] = []
        original_post_ollama = generate_exams.post_ollama

        def fake_post_ollama(endpoint: str, model: str, prompt: str, timeout: int) -> str:
            return '{"coverage_notes":[]}'

        try:
            generate_exams.post_ollama = fake_post_ollama
            data = generate_exams.call_json_with_retries(
                args,
                "Prompt body",
                "debug context",
                debug=lambda title, content: events.append((title, content)),
            )
        finally:
            generate_exams.post_ollama = original_post_ollama

        self.assertEqual(data, {"coverage_notes": []})
        self.assertTrue(any("Ollama request: debug context attempt 1" in title and "Prompt body" in content for title, content in events))
        self.assertTrue(any("Ollama raw response: debug context attempt 1" in title and '{"coverage_notes":[]}' in content for title, content in events))

    def test_job_log_writes_to_configured_log_file(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            job = Job(id="test", kind="example")
            log_path = Path(temp) / "generation-test.log"
            cli.attach_job_log_path(job, log_path)

            job_log(job, "Waiting for Ollama")
            cli.job_debug(job, "Raw response", '{"ok": true}')

            text = log_path.read_text(encoding="utf-8")
            self.assertIn("Waiting for Ollama", text)
            self.assertIn("---", text)
            self.assertIn("Raw response", text)
            self.assertIn('{"ok": true}', text)
            self.assertEqual(job.log_path, str(log_path))
            self.assertEqual(job.log_url, "/api/jobs/test/log")

    def test_run_generation_failure_writes_fallback_log_with_traceback(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            original_fallback_log_dir = cli.fallback_log_dir
            cli.fallback_log_dir = lambda: Path(temp)  # type: ignore[assignment]
            job = Job(id="failed", kind="example")

            try:
                cli.run_generation(job, {"mode": "example"})
            finally:
                cli.fallback_log_dir = original_fallback_log_dir  # type: ignore[assignment]

            log_path = Path(temp) / "generation-failed.log"
            text = log_path.read_text(encoding="utf-8")
            self.assertEqual(job.status, "error")
            self.assertIn("Input folder is required.", text)
            self.assertIn("Traceback", text)

    def test_job_log_endpoint_returns_complete_text(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            class FakeHandler:
                def __init__(self, path: str) -> None:
                    self.path = path
                    self.status: int | None = None
                    self.headers: list[tuple[str, str]] = []
                    self.wfile = io.BytesIO()

                def send_response(self, status: int) -> None:
                    self.status = status

                def send_header(self, key: str, value: str) -> None:
                    self.headers.append((key, value))

                def end_headers(self) -> None:
                    return None

            job = Job(id="endpoint", kind="example")
            cli.attach_job_log_path(job, Path(temp) / "generation-endpoint.log")
            job_log(job, "Queued example generation")
            cli.STATE.jobs[job.id] = job
            try:
                handler = FakeHandler(f"/api/jobs/{job.id}/log")
                cli.Handler.do_GET(handler)  # type: ignore[arg-type]
                body = handler.wfile.getvalue().decode("utf-8")
                self.assertEqual(handler.status, 200)
                self.assertIn("Queued example generation", body)

                missing = FakeHandler("/api/jobs/missing/log")
                cli.Handler.do_GET(missing)  # type: ignore[arg-type]
                self.assertEqual(missing.status, 404)
                self.assertIn("Job not found", missing.wfile.getvalue().decode("utf-8"))
            finally:
                cli.STATE.jobs.pop(job.id, None)


if __name__ == "__main__":
    unittest.main()
