from pathlib import Path
from types import SimpleNamespace
import sys
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "backend"))

from exam_backend import generate_exams  # noqa: E402
from exam_backend.cli import backup_existing_project, ensure_separate_output, generator_args, materialize_input, scan_folder  # noqa: E402


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
        args = generator_args(Path("/tmp/example"), example=True)

        self.assertEqual((args.min_mc, args.max_mc), (12, 20))
        self.assertEqual((args.min_open, args.max_open), (4, 8))
        self.assertTrue(args.allow_heuristic_fallback)


if __name__ == "__main__":
    unittest.main()
