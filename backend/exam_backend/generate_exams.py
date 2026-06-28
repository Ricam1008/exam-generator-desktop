#!/usr/bin/env python3
"""Generate local static exam apps from university lecture PDFs."""

from __future__ import annotations

import argparse
import datetime as dt
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import textwrap
import unicodedata
import urllib.error
import urllib.request
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable


DEFAULT_ENDPOINT = "http://localhost:11434/api/chat"
DEFAULT_MODEL = "gemma4:31b-cloud"
MIN_WORDS_FOR_FULL_EXAM = 3000
LOW_TEXT_WORD_THRESHOLD = 800
MAX_PROMPT_TEXT_CHARS = 52000
FULL_COVERAGE_TEXT_CHARS = 22000
MARKER_REQUIRED = True
USE_MARKER_FIRST = True
MARKER_TIMEOUT_SECONDS = 1800
MARKER_MIN_WORDS_FOR_SUCCESS = 80


@dataclass
class ParserResult:
    text: str
    warning: str | None
    page_count: int | None
    parser: str
    parser_warning: str | None = None
    marker_used: bool = False
    fallback_used: bool = False
    diagnostics: str | None = None


EXAM_JSON_SCHEMA: dict[str, Any] = {
    "type": "object",
    "required": ["metadata", "multiple_choice", "open_ended"],
    "properties": {
        "metadata": {
            "type": "object",
            "required": [
                "course",
                "source_pdf",
                "generated_date",
                "text_extraction_warning",
            ],
            "properties": {
                "title": {"type": "string"},
                "course": {"type": "string"},
                "source_pdf": {"type": "string"},
                "generated_date": {"type": "string"},
                "text_extraction_warning": {"type": ["string", "null"]},
                "generator": {"type": "string"},
                "source_word_count": {"type": "integer"},
                "parser": {"type": "string"},
                "parser_warning": {"type": ["string", "null"]},
                "marker_used": {"type": "boolean"},
                "fallback_used": {"type": "boolean"},
                "coverage_mode": {"type": "string"},
                "source_chunk_count": {"type": "integer"},
                "processed_chunk_count": {"type": "integer"},
                "failed_chunk_count": {"type": "integer"},
                "coverage_warning": {"type": ["string", "null"]},
                "question_count": {"type": "object"},
            },
        },
        "multiple_choice": {
            "type": "array",
            "items": {
                "type": "object",
                "required": ["id", "question", "options", "explanation"],
                "properties": {
                    "id": {"type": "string"},
                    "topic": {"type": "string"},
                    "question": {"type": "string"},
                    "options": {
                        "type": "array",
                        "minItems": 4,
                        "maxItems": 6,
                        "items": {
                            "type": "object",
                            "required": ["text", "is_correct"],
                            "properties": {
                                "text": {"type": "string"},
                                "is_correct": {"type": "boolean"},
                            },
                        },
                    },
                    "explanation": {"type": "string"},
                },
            },
        },
        "open_ended": {
            "type": "array",
            "items": {
                "type": "object",
                "required": [
                    "id",
                    "question",
                    "expected_answer",
                    "key_concepts",
                    "grading_rubric",
                    "max_score",
                ],
                "properties": {
                    "id": {"type": "string"},
                    "question": {"type": "string"},
                    "expected_answer": {"type": "string"},
                    "key_concepts": {"type": "array", "items": {"type": "string"}},
                    "grading_rubric": {"type": "object"},
                    "max_score": {"type": "integer", "const": 100},
                },
            },
        },
    },
}


GENERATION_SYSTEM_PROMPT = """You are an expert university exam writer.

Create difficult but fair exam-preparation questions from lecture slide text.
Use only the source material provided by the user. Do not invent facts.
Write all generated exam content in German, even when the source text is partly or fully English.
Keep established technical terms, theory names, study names, author names, formulas, and quoted source phrases in their original language when that is clearer or source-faithful.
Focus on definitions, models, theories, central findings, distinctions between similar concepts, examples, applications, and likely exam-relevant lecture material.
Avoid duplicate, trivial, or overly easy questions.
Distractors must be plausible and based on common misconceptions or nearby concepts from the source material.

Return valid JSON only. No markdown. No prose outside JSON."""


def build_generation_prompt(
    course: str,
    source_pdf: str,
    text: str,
    target_mc: int,
    target_open: int,
    extraction_warning: str | None,
) -> str:
    schema = json.dumps(EXAM_JSON_SCHEMA, ensure_ascii=False, indent=2)
    warning_note = extraction_warning or "None"
    return f"""Create one standalone exam JSON object for this lecture PDF.

Course: {course}
Source PDF: {source_pdf}
Text extraction warning: {warning_note}

Question counts:
- Create exactly {target_mc} multiple-choice questions if the source text supports it.
- Create exactly {target_open} open-ended questions if the source text supports it.
- If the source text is too thin or repetitive, create fewer high-quality questions and rely on the metadata warning.

Multiple-choice requirements:
- Write question text, options, explanations, topics, expected answers, key concepts, and rubrics in German.
- Preserve established technical terms, model names, formulas, and source quotations in the original language where appropriate.
- True multiple-choice, not single-choice.
- Each question has 4 to 6 options.
- Each option has a hardcoded boolean is_correct.
- There may be one correct answer, several correct answers, all correct, or none correct, but only when justified by the source.
- Include a short explanation for each MC question.
- Do not make every question have the same number of correct options.

Open-ended requirements:
- Each open question has max_score 100.
- Include expected_answer, key_concepts, and a grading_rubric object.
- Rubrics must be strict and useful for grading from 0 to 100 points.

Required JSON schema:
{schema}

Source slide text:
<<<SOURCE_TEXT
{text}
SOURCE_TEXT>>>"""


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Generate local static exam apps from PDF slide decks.")
    parser.add_argument("--root", required=True, help="Root folder containing course folders.")
    parser.add_argument("--overwrite", action="store_true", help="Overwrite existing exam folders.")
    parser.add_argument("--only-folder", help="Generate only PDFs inside a folder with this name.")
    parser.add_argument("--limit", type=int, help="Generate at most this many exams. Useful for the first inspection pass.")
    parser.add_argument("--min-mc", type=int, default=40)
    parser.add_argument("--max-mc", type=int, default=60)
    parser.add_argument("--min-open", type=int, default=10)
    parser.add_argument("--max-open", type=int, default=20)
    parser.add_argument("--endpoint", default=DEFAULT_ENDPOINT)
    parser.add_argument("--model", default=DEFAULT_MODEL)
    parser.add_argument("--timeout", type=int, default=600, help="LLM request timeout in seconds.")
    parser.add_argument("--retries", type=int, default=2, help="Retries per PDF when the model returns malformed JSON.")
    parser.add_argument(
        "--coverage-mode",
        choices=["representative", "full_coverage", "auto"],
        default="auto",
        help="Use representative excerpts, full multi-chunk coverage, or automatic full coverage for long PDFs.",
    )
    parser.add_argument(
        "--allow-heuristic-fallback",
        action="store_true",
        help="Create a small inspection-only exam if the LLM is unavailable.",
    )
    return parser.parse_args()


def slugify(value: str, max_length: int = 80) -> str:
    normalized = unicodedata.normalize("NFKD", value).encode("ascii", "ignore").decode("ascii")
    lowered = normalized.lower()
    slug = re.sub(r"[^a-z0-9]+", "-", lowered).strip("-")
    slug = re.sub(r"-{2,}", "-", slug)
    return (slug[:max_length].strip("-") or "exam")


def is_inside_exams(path: Path) -> bool:
    return any(part.lower() == "exams" for part in path.parts)


def matches_only_folder(pdf_path: Path, root: Path, only_folder: str | None) -> bool:
    if not only_folder:
        return True
    wanted = only_folder.casefold()
    try:
        relative_parts = pdf_path.relative_to(root).parts
    except ValueError:
        relative_parts = pdf_path.parts
    return any(part.casefold() == wanted for part in relative_parts[:-1])


def find_pdfs(root: Path, only_folder: str | None) -> list[Path]:
    pdfs = []
    for pdf in sorted(root.rglob("*.pdf")):
        if is_inside_exams(pdf):
            continue
        if matches_only_folder(pdf, root, only_folder):
            pdfs.append(pdf)
    return pdfs


def run_command(args: list[str], timeout: int = 120) -> subprocess.CompletedProcess[str]:
    return subprocess.run(args, check=False, capture_output=True, text=True, timeout=timeout)


def marker_cli_path() -> str | None:
    return shutil.which("marker_single")


def marker_install_hint() -> str:
    return "Marker is required. Install it with: python3 -m pip install marker-pdf"


def clean_marker_markdown(text: str) -> str:
    text = text.replace("\x00", " ")
    text = re.sub(r"\n{6,}", "\n\n\n", text)
    return text.strip()


def marker_markdown_candidates(output_dir: Path) -> list[Path]:
    return sorted(output_dir.rglob("*.md")) + sorted(output_dir.rglob("*.markdown"))


def extract_with_marker(pdf_path: Path) -> ParserResult:
    marker = marker_cli_path()
    if not marker:
        raise RuntimeError(marker_install_hint())

    with tempfile.TemporaryDirectory(prefix="exam-marker-") as temp:
        output_dir = Path(temp)
        result = run_command(
            [
                marker,
                str(pdf_path),
                "--output_format",
                "markdown",
                "--output_dir",
                str(output_dir),
                "--disable_tqdm",
            ],
            timeout=MARKER_TIMEOUT_SECONDS,
        )
        if result.returncode != 0:
            detail = (result.stderr or result.stdout or "Marker exited with an error.").strip()
            raise RuntimeError(detail[:1200])

        candidates = marker_markdown_candidates(output_dir)
        if not candidates:
            raise RuntimeError("Marker completed but did not produce a Markdown file.")
        output_file = max(candidates, key=lambda path: path.stat().st_size)
        markdown = clean_marker_markdown(output_file.read_text(encoding="utf-8", errors="replace"))
        if count_words(markdown) < MARKER_MIN_WORDS_FOR_SUCCESS:
            raise RuntimeError(f"Marker produced only {count_words(markdown)} words.")
        warning = None
        if count_words(markdown) < LOW_TEXT_WORD_THRESHOLD:
            warning = f"Only {count_words(markdown)} words were extracted; this PDF may contain scanned slides, images, or little text."

        return ParserResult(
            text=markdown,
            warning=warning,
            page_count=None,
            parser="marker",
            marker_used=True,
            fallback_used=False,
            diagnostics=f"Marker output: {output_file.name}",
        )


def extract_with_pypdf(pdf_path: Path) -> tuple[str, int | None] | None:
    try:
        from pypdf import PdfReader  # type: ignore
    except Exception:
        return None

    reader = PdfReader(str(pdf_path))
    page_text = []
    for page in reader.pages:
        page_text.append(page.extract_text() or "")
    return "\n\n".join(page_text), len(reader.pages)


def extract_with_pdfplumber(pdf_path: Path) -> tuple[str, int | None] | None:
    try:
        import pdfplumber  # type: ignore
    except Exception:
        return None

    page_text = []
    with pdfplumber.open(str(pdf_path)) as pdf:
        for page in pdf.pages:
            page_text.append(page.extract_text() or "")
        return "\n\n".join(page_text), len(pdf.pages)


def extract_with_pdftotext(pdf_path: Path) -> tuple[str, int | None] | None:
    if not shutil.which("pdftotext"):
        return None
    result = run_command(["pdftotext", "-layout", str(pdf_path), "-"], timeout=180)
    if result.returncode != 0:
        return None
    return result.stdout, None


def extract_with_strings(pdf_path: Path) -> tuple[str, int | None] | None:
    if not shutil.which("strings"):
        return None
    result = run_command(["strings", str(pdf_path)], timeout=180)
    if result.returncode != 0:
        return None
    lines = []
    for line in result.stdout.splitlines():
        clean = line.strip()
        if len(clean) >= 4 and re.search(r"[A-Za-z0-9]", clean):
            lines.append(clean)
    return "\n".join(lines), None


def clean_extracted_text(text: str) -> str:
    text = text.replace("\x00", " ")
    text = re.sub(r"[ \t]+", " ", text)
    text = re.sub(r"\n{4,}", "\n\n\n", text)
    return text.strip()


def extract_with_legacy_parsers(pdf_path: Path, fallback_reason: str | None = None) -> ParserResult:
    attempts = [
        ("pypdf", extract_with_pypdf),
        ("pdfplumber", extract_with_pdfplumber),
        ("pdftotext", extract_with_pdftotext),
        ("strings fallback", extract_with_strings),
    ]
    last_error = None
    for name, extractor in attempts:
        try:
            result = extractor(pdf_path)
        except Exception as exc:
            last_error = f"{name} failed: {exc}"
            continue
        if not result:
            continue
        text, page_count = result
        text = clean_extracted_text(text)
        if text:
            words = count_words(text)
            warning = None
            if name == "strings fallback":
                warning = "Text extraction used a rough fallback; question quality may be weak. Install pypdf or Poppler/pdftotext for better extraction."
            elif words < LOW_TEXT_WORD_THRESHOLD:
                warning = f"Only {words} words were extracted; this PDF may contain scanned slides, images, or little text."
            parser_warning = f"Marker failed; used {name}. Reason: {fallback_reason}" if fallback_reason else None
            combined_warning = " ".join(part for part in [warning, parser_warning] if part).strip() or None
            return ParserResult(
                text=text,
                warning=combined_warning,
                page_count=page_count,
                parser=name,
                parser_warning=parser_warning,
                marker_used=False,
                fallback_used=bool(fallback_reason),
                diagnostics=last_error,
            )

    message = last_error or "No PDF text extractor was available."
    parser_warning = f"Marker failed and legacy extraction failed. Marker reason: {fallback_reason}. Legacy reason: {message}" if fallback_reason else message
    return ParserResult(
        text="",
        warning=f"Could not extract usable text. {parser_warning}",
        page_count=None,
        parser="none",
        parser_warning=parser_warning,
        marker_used=False,
        fallback_used=bool(fallback_reason),
        diagnostics=message,
    )


def extract_pdf_text(pdf_path: Path) -> ParserResult:
    if USE_MARKER_FIRST:
        if not marker_cli_path():
            if MARKER_REQUIRED:
                raise RuntimeError(marker_install_hint())
            return extract_with_legacy_parsers(pdf_path, "Marker is not installed.")
        try:
            return extract_with_marker(pdf_path)
        except Exception as exc:
            return extract_with_legacy_parsers(pdf_path, str(exc))
    return extract_with_legacy_parsers(pdf_path)


def count_words(text: str) -> int:
    return len(re.findall(r"\b[\w'-]+\b", text, flags=re.UNICODE))


def chunk_for_prompt(text: str, max_chars: int = MAX_PROMPT_TEXT_CHARS) -> str:
    if len(text) <= max_chars:
        return text

    chunk_size = 6500
    chunks = [text[i : i + chunk_size] for i in range(0, len(text), chunk_size)]
    keep_count = max(3, max_chars // chunk_size)
    if len(chunks) <= keep_count:
        selected = chunks
    else:
        selected_indexes = sorted({round(i * (len(chunks) - 1) / (keep_count - 1)) for i in range(keep_count)})
        selected = [chunks[index] for index in selected_indexes]
    labeled = [f"[Excerpt {i + 1} of {len(selected)}]\n{chunk}" for i, chunk in enumerate(selected)]
    return "\n\n".join(labeled)


def chunk_text(text: str, max_chars: int = FULL_COVERAGE_TEXT_CHARS) -> list[str]:
    if len(text) <= max_chars:
        return [text]
    chunks = []
    current: list[str] = []
    current_len = 0
    for block in text.split("\n\n"):
        block_len = len(block) + 2
        if current and current_len + block_len > max_chars:
            chunks.append("\n\n".join(current))
            current = []
            current_len = 0
        if block_len > max_chars:
            for index in range(0, len(block), max_chars):
                chunks.append(block[index : index + max_chars])
            continue
        current.append(block)
        current_len += block_len
    if current:
        chunks.append("\n\n".join(current))
    return chunks


def resolve_coverage_mode(text: str, args: argparse.Namespace) -> str:
    mode = getattr(args, "coverage_mode", "representative")
    if mode == "auto":
        return "full_coverage" if len(text) > MAX_PROMPT_TEXT_CHARS else "representative"
    return mode


def distribute_counts(total: int, buckets: int) -> list[int]:
    if buckets <= 0:
        return []
    base, remainder = divmod(total, buckets)
    return [base + (1 if index < remainder else 0) for index in range(buckets)]


def target_counts(words: int, args: argparse.Namespace) -> tuple[int, int]:
    if words < LOW_TEXT_WORD_THRESHOLD:
        return min(12, args.min_mc), min(4, args.min_open)
    if words < MIN_WORDS_FOR_FULL_EXAM:
        return min(25, args.min_mc), min(8, args.min_open)
    if words < 6000:
        mc = args.min_mc
        open_count = args.min_open
    elif words < 12000:
        mc = round((args.min_mc + args.max_mc) / 2)
        open_count = round((args.min_open + args.max_open) / 2)
    else:
        mc = args.max_mc
        open_count = args.max_open
    mc = max(args.min_mc, min(args.max_mc, mc))
    open_count = max(args.min_open, min(args.max_open, open_count))
    return mc, open_count


def scaled_retry_counts(mc: int, open_count: int, attempt: int, args: argparse.Namespace) -> tuple[int, int]:
    if attempt <= 0:
        return mc, open_count
    factor = 0.85 if attempt == 1 else 0.7
    retry_mc = max(8, int(round(mc * factor)))
    retry_open = max(3, int(round(open_count * factor)))
    return min(args.max_mc, retry_mc), min(args.max_open, retry_open)


def post_ollama(endpoint: str, model: str, prompt: str, timeout: int) -> str:
    payload = {
        "model": model,
        "stream": False,
        "format": "json",
        "messages": [
            {"role": "system", "content": GENERATION_SYSTEM_PROMPT},
            {"role": "user", "content": prompt},
        ],
        "options": {"temperature": 0.25},
    }
    data = json.dumps(payload).encode("utf-8")
    request = urllib.request.Request(endpoint, data=data, headers={"Content-Type": "application/json"}, method="POST")
    with urllib.request.urlopen(request, timeout=timeout) as response:
        body = response.read().decode("utf-8")
    parsed = json.loads(body)
    if "message" in parsed and isinstance(parsed["message"], dict):
        return parsed["message"].get("content", "")
    return parsed.get("response", "")


def load_json_from_model(raw: str) -> dict[str, Any]:
    raw = raw.strip()
    try:
        return json.loads(raw)
    except json.JSONDecodeError:
        start = raw.find("{")
        end = raw.rfind("}")
        if start == -1 or end == -1 or end <= start:
            raise
        return json.loads(raw[start : end + 1])


def call_json_with_retries(args: argparse.Namespace, prompt: str, context: str, progress: Callable[[str], None] | None = None) -> dict[str, Any]:
    last_error: Exception | None = None
    for attempt in range(max(0, args.retries) + 1):
        retry_prompt = prompt
        if attempt:
            retry_prompt += (
                "\n\nSTRICT RETRY INSTRUCTION:\n"
                "Return one complete valid JSON object only. No markdown, no comments, no prose outside JSON."
            )
        try:
            if progress:
                progress(f"Waiting for Ollama: {context} (attempt {attempt + 1}/{max(0, args.retries) + 1})")
            return load_json_from_model(post_ollama(args.endpoint, args.model, retry_prompt, args.timeout))
        except (urllib.error.URLError, TimeoutError, json.JSONDecodeError, ValueError, OSError) as exc:
            last_error = exc
            if attempt < max(0, args.retries):
                if progress:
                    progress(f"RETRY {attempt + 1}/{args.retries} for {context}: {exc}")
                continue
    raise RuntimeError(f"Could not get valid JSON for {context}: {last_error}") from last_error


def ensure_list(value: Any) -> list[Any]:
    return value if isinstance(value, list) else []


def first_list(model_exam: dict[str, Any], keys: list[str]) -> list[Any]:
    for key in keys:
        value = model_exam.get(key)
        if isinstance(value, list):
            return value
    questions = model_exam.get("questions")
    if isinstance(questions, dict):
        for key in keys:
            value = questions.get(key)
            if isinstance(value, list):
                return value
    return []


def question_signature(text: str) -> str:
    text = re.sub(r"\s+", " ", text.casefold()).strip()
    text = re.sub(r"[^a-z0-9äöüß ]", "", text)
    return text[:180]


def normalize_mc_items(raw_questions: Any, existing: set[str]) -> list[dict[str, Any]]:
    questions = []
    if not isinstance(raw_questions, list):
        return questions
    for raw in raw_questions:
        if not isinstance(raw, dict):
            continue
        question_text = str(raw.get("question") or "").strip()
        explanation = str(raw.get("explanation") or "").strip()
        if not question_text or not explanation:
            continue
        signature = question_signature(question_text)
        if signature in existing:
            continue
        options = []
        for option in ensure_list(raw.get("options"))[:6]:
            if not isinstance(option, dict):
                continue
            option_text = str(option.get("text") or "").strip()
            if option_text:
                options.append({"text": option_text, "is_correct": bool(option.get("is_correct", False))})
        if len(options) < 4:
            continue
        questions.append(
            {
                "id": "",
                "topic": str(raw.get("topic") or "").strip(),
                "question": question_text,
                "options": options,
                "explanation": explanation,
            }
        )
        existing.add(signature)
    return questions


def normalize_open_items(raw_questions: Any, existing: set[str]) -> list[dict[str, Any]]:
    questions = []
    if not isinstance(raw_questions, list):
        return questions
    for raw in raw_questions:
        if not isinstance(raw, dict):
            continue
        question_text = str(raw.get("question") or "").strip()
        expected = str(raw.get("expected_answer") or "").strip()
        if not question_text or not expected:
            continue
        signature = question_signature(question_text)
        if signature in existing:
            continue
        rubric = raw.get("grading_rubric")
        if not isinstance(rubric, dict):
            rubric = {
                "90-100": "Präzise, vollständige Antwort.",
                "76-89": "Gute Antwort mit kleineren Lücken.",
                "61-75": "Solide Antwort mit merklichen Lücken.",
                "41-60": "Grundsätzlich thematisch, aber unvollständig.",
                "21-40": "Teilweise relevant mit großen Lücken.",
                "0-20": "Falsch, leer oder sehr allgemein.",
            }
        questions.append(
            {
                "id": "",
                "question": question_text,
                "expected_answer": expected,
                "key_concepts": [str(item) for item in ensure_list(raw.get("key_concepts")) if str(item).strip()],
                "grading_rubric": rubric,
                "max_score": 100,
            }
        )
        existing.add(signature)
    return questions


def normalize_exam(
    model_exam: dict[str, Any],
    course: str,
    source_pdf: str,
    extraction_warning: str | None,
    source_word_count: int,
    parser_result: ParserResult | None = None,
) -> dict[str, Any]:
    mc_questions = []
    mc_source = first_list(model_exam, ["multiple_choice", "multiple_choice_questions", "mc_questions", "mcq", "mc"])
    for index, question in enumerate(mc_source, start=1):
        if not isinstance(question, dict):
            continue
        options = []
        for option in ensure_list(question.get("options"))[:6]:
            if not isinstance(option, dict):
                continue
            text = str(option.get("text", "")).strip()
            if not text:
                continue
            options.append({"text": text, "is_correct": bool(option.get("is_correct", False))})
        if len(options) < 4:
            continue
        mc_questions.append(
            {
                "id": str(question.get("id") or f"mc-{index:03d}"),
                "topic": str(question.get("topic") or ""),
                "question": str(question.get("question") or "").strip(),
                "options": options,
                "explanation": str(question.get("explanation") or "").strip(),
            }
        )

    open_questions = []
    open_source = first_list(model_exam, ["open_ended", "open_ended_questions", "open_questions", "short_answer", "essay_questions"])
    for index, question in enumerate(open_source, start=1):
        if not isinstance(question, dict):
            continue
        rubric = question.get("grading_rubric")
        if not isinstance(rubric, dict):
            rubric = {
                "90-100": "Precise, complete answer covering all key concepts.",
                "61-89": "Mostly correct answer with minor to noticeable gaps.",
                "41-60": "On topic but incomplete or imprecise.",
                "21-40": "Partially relevant with major conceptual gaps.",
                "0-20": "Mostly wrong, vague, or empty.",
            }
        open_questions.append(
            {
                "id": str(question.get("id") or f"open-{index:03d}"),
                "question": str(question.get("question") or "").strip(),
                "expected_answer": str(question.get("expected_answer") or "").strip(),
                "key_concepts": [str(item) for item in ensure_list(question.get("key_concepts")) if str(item).strip()],
                "grading_rubric": rubric,
                "max_score": 100,
            }
        )

    today = dt.date.today().isoformat()
    title = Path(source_pdf).stem
    if not mc_questions or not open_questions:
        raise ValueError("The model response did not contain usable MC and open-ended questions.")
    for question in mc_questions:
        if not question["question"] or not question["explanation"]:
            raise ValueError("At least one MC question is missing a question text or explanation.")
    for question in open_questions:
        if not question["question"] or not question["expected_answer"]:
            raise ValueError("At least one open-ended question is missing a question text or expected answer.")

    return {
        "metadata": {
            "title": title,
            "course": course,
            "source_pdf": source_pdf,
            "generated_date": today,
            "text_extraction_warning": extraction_warning,
            "generator": "generate_exams.py",
            "source_word_count": source_word_count,
            "parser": parser_result.parser if parser_result else "unknown",
            "parser_warning": parser_result.parser_warning if parser_result else None,
            "marker_used": parser_result.marker_used if parser_result else False,
            "fallback_used": parser_result.fallback_used if parser_result else False,
            "question_count": {
                "multiple_choice": len(mc_questions),
                "open_ended": len(open_questions),
            },
        },
        "multiple_choice": mc_questions,
        "open_ended": open_questions,
    }


def apply_coverage_metadata(
    exam: dict[str, Any],
    coverage_mode: str,
    source_chunk_count: int,
    processed_chunk_count: int,
    failed_chunk_count: int,
    coverage_warning: str | None,
) -> dict[str, Any]:
    metadata = exam.setdefault("metadata", {})
    metadata["coverage_mode"] = coverage_mode
    metadata["source_chunk_count"] = source_chunk_count
    metadata["processed_chunk_count"] = processed_chunk_count
    metadata["failed_chunk_count"] = failed_chunk_count
    metadata["coverage_warning"] = coverage_warning
    if coverage_warning:
        existing = metadata.get("text_extraction_warning")
        metadata["text_extraction_warning"] = f"{existing} {coverage_warning}".strip() if existing else coverage_warning
    return exam


def build_chunk_coverage_prompt(course: str, source_pdf: str, chunk: str, chunk_index: int, chunk_count: int) -> str:
    return f"""Create compact coverage notes for one source chunk from a university lecture PDF.

Course: {course}
Source PDF: {source_pdf}
Chunk: {chunk_index} of {chunk_count}

Write notes in German, preserving established technical terms, study names, formulas, and author names where useful.
Focus on exam-relevant definitions, theories, models, findings, distinctions, examples, applications, and conceptual traps.

Return JSON only:
{{
  "coverage_notes": [
    {{
      "topic": "string",
      "exam_targets": ["string"],
      "common_traps": ["string"],
      "source_area": "string"
    }}
  ]
}}

SOURCE CHUNK:
<<<SOURCE
{chunk}
SOURCE>>>"""


def build_chunk_questions_prompt(
    course: str,
    source_pdf: str,
    chunk: str,
    coverage_notes: dict[str, Any],
    target_mc: int,
    target_open: int,
    existing_stems: list[str],
    chunk_index: int,
    chunk_count: int,
) -> str:
    return f"""Generate exam questions from this source chunk.

Course: {course}
Source PDF: {source_pdf}
Chunk: {chunk_index} of {chunk_count}

Question counts:
- Generate exactly {target_mc} multiple-choice questions.
- Generate exactly {target_open} open-ended questions.
- If a count is 0, return an empty array for that section.

Language requirements:
- Write questions, options, explanations, expected answers, key concepts, and rubrics in German.
- Preserve established technical terms, model names, formulas, author names, and source quotations in the original language where appropriate.

Multiple-choice requirements:
- True multiple-choice, not single-choice.
- Each question has 4 to 6 options.
- Every option has a hardcoded boolean is_correct.
- Distractors must be plausible and source-adjacent.

Open-ended requirements:
- Each open question has expected_answer, key_concepts, grading_rubric, and max_score 100.
- Make questions precise, difficult, and gradeable.

Avoid duplicating these already generated stems:
{json.dumps(existing_stems[-80:], ensure_ascii=False, indent=2)}

Return JSON only:
{{
  "multiple_choice": [
    {{
      "topic": "string",
      "question": "string",
      "options": [
        {{"text": "string", "is_correct": true}},
        {{"text": "string", "is_correct": false}},
        {{"text": "string", "is_correct": false}},
        {{"text": "string", "is_correct": true}}
      ],
      "explanation": "string"
    }}
  ],
  "open_ended": [
    {{
      "question": "string",
      "expected_answer": "string",
      "key_concepts": ["string"],
      "grading_rubric": {{
        "90-100": "string",
        "76-89": "string",
        "61-75": "string",
        "41-60": "string",
        "21-40": "string",
        "0-20": "string"
      }},
      "max_score": 100
    }}
  ]
}}

COVERAGE NOTES:
<<<COVERAGE
{json.dumps(coverage_notes, ensure_ascii=False, indent=2)[:18000]}
COVERAGE>>>

SOURCE CHUNK:
<<<SOURCE
{chunk}
SOURCE>>>"""


def generate_full_coverage_exam(
    course: str,
    source_pdf: str,
    text: str,
    extraction_warning: str | None,
    word_count: int,
    args: argparse.Namespace,
    parser_result: ParserResult | None = None,
    progress: Callable[[str], None] | None = None,
) -> dict[str, Any]:
    chunks = chunk_text(text)
    target_mc, target_open = target_counts(word_count, args)
    mc_targets = distribute_counts(target_mc, len(chunks))
    open_targets = distribute_counts(target_open, len(chunks))
    mc_questions: list[dict[str, Any]] = []
    open_questions: list[dict[str, Any]] = []
    seen_mc: set[str] = set()
    seen_open: set[str] = set()
    failed_chunks = 0
    processed_chunks = 0

    for index, chunk in enumerate(chunks, start=1):
        if progress:
            progress(f"Processing chunk {index}/{len(chunks)}")
        try:
            coverage = call_json_with_retries(args, build_chunk_coverage_prompt(course, source_pdf, chunk, index, len(chunks)), f"{source_pdf} coverage chunk {index}", progress)
            if progress:
                progress(f"Generating questions batch {index}/{len(chunks)}")
            data = call_json_with_retries(
                args,
                build_chunk_questions_prompt(
                    course,
                    source_pdf,
                    chunk,
                    coverage,
                    mc_targets[index - 1],
                    open_targets[index - 1],
                    [question["question"] for question in mc_questions + open_questions],
                    index,
                    len(chunks),
                ),
                f"{source_pdf} questions chunk {index}",
                progress,
            )
            mc_questions.extend(normalize_mc_items(first_list(data, ["multiple_choice", "multiple_choice_questions", "mc_questions", "mcq", "mc"]), seen_mc))
            open_questions.extend(normalize_open_items(first_list(data, ["open_ended", "open_ended_questions", "open_questions", "short_answer", "essay_questions"]), seen_open))
            processed_chunks += 1
            if progress:
                progress(f"Merged {len(mc_questions)} MC / {len(open_questions)} open")
        except RuntimeError as exc:
            failed_chunks += 1
            if progress:
                progress(f"Chunk {index}/{len(chunks)} failed: {exc}")

    too_many_failed = failed_chunks > len(chunks) // 2
    if processed_chunks == 0 or too_many_failed or len(mc_questions) < args.min_mc or len(open_questions) < args.min_open:
        raise RuntimeError(
            f"Full-coverage generation failed for {source_pdf}: processed {processed_chunks}/{len(chunks)} chunks, "
            f"generated {len(mc_questions)} MC and {len(open_questions)} open questions."
        )

    warnings = []
    if failed_chunks:
        warnings.append(f"Full coverage processed {processed_chunks}/{len(chunks)} chunks; {failed_chunks} chunk(s) failed.")
    if len(mc_questions) < target_mc or len(open_questions) < target_open:
        warnings.append(f"Generated {len(mc_questions)} MC and {len(open_questions)} open questions instead of target {target_mc}/{target_open}.")

    exam = normalize_exam(
        {"multiple_choice": mc_questions[:target_mc], "open_ended": open_questions[:target_open]},
        course,
        source_pdf,
        extraction_warning,
        word_count,
        parser_result,
    )
    return apply_coverage_metadata(exam, "full_coverage", len(chunks), processed_chunks, failed_chunks, " ".join(warnings) if warnings else None)


def heuristic_exam(
    course: str,
    source_pdf: str,
    text: str,
    extraction_warning: str | None,
    source_word_count: int,
    parser_result: ParserResult | None = None,
) -> dict[str, Any]:
    sentences = [s.strip() for s in re.split(r"(?<=[.!?])\s+|\n+", text) if len(s.strip()) > 55]
    seeds = sentences[:16] or [Path(source_pdf).stem]
    mc = []
    for index, sentence in enumerate(seeds[:8], start=1):
        topic = sentence[:70]
        mc.append(
            {
                "id": f"mc-{index:03d}",
                "topic": topic,
                "question": f"Welche Aussagen werden durch den Quellentext zu folgendem Punkt gestützt: {topic}?",
                "options": [
                    {"text": sentence[:220], "is_correct": True},
                    {"text": "Die Quelle stellt diesen Punkt als unabhängig vom Thema der Lehrveranstaltung dar.", "is_correct": False},
                    {"text": "Die Quelle behandelt dieses Konzept nur als historische Randnotiz ohne Prüfungsrelevanz.", "is_correct": False},
                    {"text": "Der Punkt sollte im Zusammenhang mit den umliegenden Konzepten aus der Quelle verstanden werden.", "is_correct": True},
                ],
                "explanation": "Diese Notfallfrage nutzt direkt extrahierten Quellentext, weil die KI kein vollständig verwertbares Prüfungs-JSON geliefert hat.",
            }
        )
    open_questions = []
    for index, sentence in enumerate(seeds[8:12] or seeds[:4], start=1):
        open_questions.append(
            {
                "id": f"open-{index:03d}",
                "question": f"Erkläre die Prüfungsrelevanz dieses Quellpunkts: {sentence[:180]}",
                "expected_answer": sentence,
                "key_concepts": [word for word in re.findall(r"\b[A-Za-zÄÖÜäöüß][\wÄÖÜäöüß-]{5,}\b", sentence)[:6]],
                "grading_rubric": {
                    "90-100": "Präzise, vollständige Erklärung, die klar am zitierten Quellpunkt verankert ist.",
                    "61-89": "Überwiegend korrekte Erklärung mit kleineren Lücken.",
                    "41-60": "Relevant, aber unvollständig oder unpräzise.",
                    "21-40": "Nur locker mit dem Quellpunkt verbunden.",
                    "0-20": "Fehlende, falsche oder sehr allgemeine Antwort.",
                },
                "max_score": 100,
            }
        )
    warning = extraction_warning or ""
    warning = (warning + " " if warning else "") + "Die KI konnte kein vollständig verwertbares Prüfungs-JSON liefern; diese Datei ist eine kleinere Notfall-Inspektionsprüfung aus direkt extrahiertem Quellentext."
    return normalize_exam(
        {"multiple_choice": mc, "open_ended": open_questions},
        course,
        source_pdf,
        warning.strip(),
        source_word_count,
        parser_result,
    )


def escape_script_json(data: dict[str, Any]) -> str:
    return json.dumps(data, ensure_ascii=False, indent=2).replace("</", "<\\/")


def render_exam_html(template_dir: Path, exam: dict[str, Any]) -> str:
    template = (template_dir / "index_template.html").read_text(encoding="utf-8")
    styles = textwrap.indent((template_dir / "styles.css").read_text(encoding="utf-8"), "    ")
    script = textwrap.indent((template_dir / "app.js").read_text(encoding="utf-8"), "    ")
    exam_json = textwrap.indent(escape_script_json(exam), "    ")
    return template.replace("{{ styles }}", styles).replace("{{ script }}", script).replace("{{ exam_json }}", exam_json)


def write_exam_folder(
    pdf_path: Path,
    root: Path,
    args: argparse.Namespace,
    template_dir: Path,
    progress: Callable[[str], None] | None = None,
) -> dict[str, Any] | None:
    course_dir = pdf_path.parent
    course = course_dir.name
    exams_dir = course_dir / "exams"
    exam_dir = exams_dir / slugify(pdf_path.stem)

    if exam_dir.exists() and not args.overwrite:
        print(f"SKIP existing: {exam_dir}", flush=True)
        return None

    parser_result = extract_pdf_text(pdf_path)
    text = parser_result.text
    extraction_warning = parser_result.warning
    page_count = parser_result.page_count
    word_count = count_words(text)
    if progress:
        pages = page_count if page_count is not None else "unknown"
        progress(f"Extracted {word_count:,} words from {pages} pages using {parser_result.parser}")
        if parser_result.fallback_used and parser_result.parser_warning:
            progress(parser_result.parser_warning)

    base_mc, base_open = target_counts(word_count, args)
    last_error: Exception | None = None
    exam: dict[str, Any] | None = None
    max_attempts = max(0, args.retries) + 1
    coverage_mode = resolve_coverage_mode(text, args)

    if coverage_mode == "full_coverage":
        if progress:
            progress("Using full-coverage generation")
        exam = generate_full_coverage_exam(course, pdf_path.name, text, extraction_warning, word_count, args, parser_result, progress)
    else:
        if progress:
            progress("Using representative generation")
        prompt_text = chunk_for_prompt(text)
        if len(text) > len(prompt_text):
            chunk_note = "Long PDF text was chunked into representative excerpts for LLM generation."
            extraction_warning = f"{extraction_warning} {chunk_note}".strip() if extraction_warning else chunk_note

        for attempt in range(max_attempts):
            target_mc, target_open = scaled_retry_counts(base_mc, base_open, attempt, args)
            prompt = build_generation_prompt(course, pdf_path.name, prompt_text, target_mc, target_open, extraction_warning)
            if attempt:
                prompt += (
                    "\n\nIMPORTANT RETRY INSTRUCTION:\n"
                    "Your previous response for this PDF was invalid, incomplete, or failed schema validation. Return one complete, syntactically valid JSON object only. "
                    "Do not include markdown, comments, trailing commas, undefined values, or text outside the JSON object. "
                    f"Include both a multiple_choice array with {target_mc} usable questions and an open_ended array with {target_open} usable questions. "
                    "Use shorter but still substantive question and explanation text if needed."
                )
            try:
                if progress:
                    progress(f"Waiting for Ollama: {pdf_path.name} (attempt {attempt + 1}/{max_attempts}, {target_mc} MC / {target_open} open)")
                raw = post_ollama(args.endpoint, args.model, prompt, args.timeout)
                model_exam = load_json_from_model(raw)
                exam = normalize_exam(model_exam, course, pdf_path.name, extraction_warning, word_count, parser_result)
                exam = apply_coverage_metadata(exam, "representative", 1, 1, 0, None)
                if progress:
                    progress(f"Ollama returned usable exam JSON for {pdf_path.name}")
                break
            except (urllib.error.URLError, TimeoutError, json.JSONDecodeError, ValueError, OSError) as exc:
                last_error = exc
                if attempt < max_attempts - 1:
                    message = f"RETRY {attempt + 1}/{args.retries} for {pdf_path.name}: {exc}"
                    print(message, flush=True)
                    if progress:
                        progress(message)
                    continue

    if exam is None:
        if not args.allow_heuristic_fallback:
            raise RuntimeError(f"Could not generate questions for {pdf_path.name}: {last_error}") from last_error
        print(f"LLM unavailable for {pdf_path.name}; writing heuristic inspection exam.", flush=True)
        if progress:
            progress(f"Using fallback inspection exam for {pdf_path.name}")
        exam = heuristic_exam(course, pdf_path.name, text, extraction_warning, word_count, parser_result)

    if exam_dir.exists() and args.overwrite:
        shutil.rmtree(exam_dir)
    exam_dir.mkdir(parents=True, exist_ok=True)
    (exam_dir / "index.html").write_text(render_exam_html(template_dir, exam), encoding="utf-8")
    (exam_dir / "exam.json").write_text(json.dumps(exam, ensure_ascii=False, indent=2), encoding="utf-8")
    (exam_dir / "source.txt").write_text(text, encoding="utf-8")
    print(f"WROTE {exam_dir}", flush=True)

    return {
        "course": course,
        "course_dir": str(course_dir),
        "exam_dir": str(exam_dir),
        "source_pdf": pdf_path.name,
        "warning": exam["metadata"].get("text_extraction_warning"),
        "mc_count": len(exam["multiple_choice"]),
        "open_count": len(exam["open_ended"]),
        "page_count": page_count,
        "word_count": word_count,
    }


def discover_generated_exams(root: Path) -> dict[Path, list[dict[str, Any]]]:
    courses: dict[Path, list[dict[str, Any]]] = {}
    for exam_json in sorted(root.rglob("exams/*/exam.json")):
        try:
            data = json.loads(exam_json.read_text(encoding="utf-8"))
        except Exception:
            continue
        metadata = data.get("metadata", {})
        course_dir = exam_json.parent.parent.parent
        courses.setdefault(course_dir, []).append(
            {
                "exam_dir": exam_json.parent,
                "title": metadata.get("title") or exam_json.parent.name,
                "course": metadata.get("course") or course_dir.name,
                "source_pdf": metadata.get("source_pdf") or "",
                "warning": metadata.get("text_extraction_warning"),
                "mc_count": len(data.get("multiple_choice", [])),
                "open_count": len(data.get("open_ended", [])),
            }
        )
    return courses


def html_page(title: str, body: str) -> str:
    return f"""<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>{escape_html(title)}</title>
  <style>
    body {{ margin: 0; font-family: system-ui, -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif; background: #f7f5f0; color: #17211d; }}
    main {{ width: min(1000px, calc(100% - 32px)); margin: 0 auto; padding: 32px 0 56px; }}
    h1 {{ margin: 0 0 8px; font-size: clamp(2rem, 5vw, 3.2rem); }}
    p {{ color: #65726d; }}
    ul {{ list-style: none; padding: 0; display: grid; gap: 10px; }}
    li {{ background: white; border: 1px solid #d9ded8; border-radius: 8px; padding: 14px 16px; }}
    a {{ color: #0f766e; font-weight: 750; text-decoration: none; }}
    a:hover {{ text-decoration: underline; }}
    .warning {{ display: inline-block; margin-top: 8px; color: #995c00; }}
    .meta {{ display: block; margin-top: 4px; color: #65726d; }}
  </style>
</head>
<body>
  <main>
{body}
  </main>
</body>
</html>
"""


def escape_html(value: Any) -> str:
    return (
        str(value)
        .replace("&", "&amp;")
        .replace("<", "&lt;")
        .replace(">", "&gt;")
        .replace('"', "&quot;")
    )


def rel_href(from_dir: Path, target: Path) -> str:
    return Path(os.path.relpath(target, start=from_dir)).as_posix()


def write_index_pages(root: Path) -> None:
    courses = discover_generated_exams(root)
    course_links = []
    for course_dir, exams in sorted(courses.items(), key=lambda item: item[0].name.casefold()):
        exams_dir = course_dir / "exams"
        items = []
        for exam in sorted(exams, key=lambda item: item["source_pdf"].casefold()):
            href = rel_href(exams_dir, exam["exam_dir"] / "index.html")
            warning = f'<span class="warning">Warning: {escape_html(exam["warning"])}</span>' if exam.get("warning") else ""
            items.append(
                f'<li><a href="{escape_html(href)}">{escape_html(exam["title"])}</a>'
                f'<span class="meta">Source: {escape_html(exam["source_pdf"])} · '
                f'{exam["mc_count"]} MC · {exam["open_count"]} open</span>{warning}</li>'
            )
        body = (
            f"    <h1>{escape_html(course_dir.name)}</h1>\n"
            f"    <p>{len(exams)} generated exam{'s' if len(exams) != 1 else ''}</p>\n"
            f"    <ul>\n      " + "\n      ".join(items) + "\n    </ul>\n"
        )
        exams_dir.mkdir(exist_ok=True)
        (exams_dir / "index.html").write_text(html_page(f"{course_dir.name} exams", body), encoding="utf-8")
        root_href = rel_href(root, exams_dir / "index.html")
        course_links.append(
            f'<li><a href="{escape_html(root_href)}">{escape_html(course_dir.name)}</a>'
            f'<span class="meta">{len(exams)} generated exam{"s" if len(exams) != 1 else ""}</span></li>'
        )

    root_body = (
        "    <h1>Generated exam index</h1>\n"
        f"    <p>{sum(len(exams) for exams in courses.values())} generated exams across {len(courses)} course folders.</p>\n"
        "    <ul>\n      " + "\n      ".join(course_links) + "\n    </ul>\n"
    )
    (root / "exam_index.html").write_text(html_page("Generated exam index", root_body), encoding="utf-8")


def main() -> int:
    args = parse_args()
    root = Path(args.root).expanduser().resolve()
    template_dir = Path(__file__).resolve().parent / "templates"
    if not root.exists():
        print(f"Root does not exist: {root}", file=sys.stderr, flush=True)
        return 2
    for required in ["index_template.html", "styles.css", "app.js"]:
        if not (template_dir / required).exists():
            print(f"Missing template file: {template_dir / required}", file=sys.stderr, flush=True)
            return 2

    pdfs = find_pdfs(root, args.only_folder)
    if args.limit is not None:
        pdfs = pdfs[: max(0, args.limit)]

    if not pdfs:
        print("No PDFs found.", flush=True)
        write_index_pages(root)
        return 0

    generated = []
    failures = []
    for pdf in pdfs:
        try:
            result = write_exam_folder(pdf, root, args, template_dir)
            if result:
                generated.append(result)
        except Exception as exc:
            failures.append((pdf.name, str(exc)))
            print(f"FAILED {pdf.name}: {exc}", file=sys.stderr, flush=True)
            continue

    write_index_pages(root)
    if failures:
        print(f"Completed with {len(failures)} failed PDF(s).", file=sys.stderr, flush=True)
        if len(failures) == len(pdfs):
            return 1
    print(f"Done. Generated {len(generated)} exam(s). Index: {root / 'exam_index.html'}", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
